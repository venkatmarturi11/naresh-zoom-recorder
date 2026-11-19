#!/usr/bin/env python3
"""
Autonomous zero-device 1080p Zoom class recorder + Telegram archiver.

Pipeline:  Xvfb + PulseAudio -> Chromium (Playwright) -> portal login ->
           poll Join button -> Zoom web client (muted, no camera) ->
           FFmpeg x11grab/pulse crash-safe MKV segments -> MP4 remux ->
           Pyrogram MTProto upload (auto-split above ~1.95 GB).

All configuration comes from environment variables (GitHub Secrets).
"""
import asyncio
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is only a convenience for local runs
    pass

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")


def _env(name: str, default: str = "") -> str:
    """Return env var *name*, falling back to *default* when unset OR blank."""
    val = os.environ.get(name, "").strip()
    return val if val else default.strip()


PORTAL_URL = _env("PORTAL_URL")
if PORTAL_URL and not PORTAL_URL.startswith(("http://", "https://")):
    PORTAL_URL = "https://" + PORTAL_URL
PORTAL_USER = _env("PORTAL_USER")
PORTAL_PASS = _env("PORTAL_PASS")
TG_API_ID = _env("TG_API_ID")
TG_API_HASH = _env("TG_API_HASH")
TG_BOT_TOKEN = _env("TG_BOT_TOKEN")
TG_CHAT_ID = _env("TG_CHAT_ID")

CLASS_KEYWORD = _env("CLASS_KEYWORD", "React JS")
CLASS_START = _env("CLASS_START", "19:30")  # HH:MM, IST
CLASS_END = _env("CLASS_END", "")  # HH:MM, IST (optional hard stop)
CLASS_DURATION_MIN = int(_env("CLASS_DURATION_MIN", "90"))
MAX_WAIT_MIN = int(_env("MAX_WAIT_MIN", "45"))  # wait for Join to activate
MAX_MEETING_SEC = int(_env("MAX_MEETING_SEC", str(CLASS_DURATION_MIN * 60)))
if CLASS_END and not os.environ.get("MAX_MEETING_SEC", "").strip():
    try:
        sh, sm = (int(x) for x in CLASS_START.split(":"))
        eh, em = (int(x) for x in CLASS_END.split(":"))
        diff_sec = ((eh * 60 + em) - (sh * 60 + sm)) * 60
        if diff_sec < 0:
            diff_sec += 24 * 3600
        if diff_sec > 0:
            MAX_MEETING_SEC = diff_sec + 600  # 10 min buffer past class end
    except Exception:
        pass
PARTICIPANT_NAME = _env("PARTICIPANT_NAME", "") or (
    PORTAL_USER.split("@")[0] if "@" in PORTAL_USER else "Student"
)

RECORD_PATH = _env("RECORD_PATH", "/tmp/class_recording.mp4")
STEP_SUMMARY_PATH = _env("STEP_SUMMARY_PATH", "")
FFMPEG_LOG = "/tmp/ffmpeg.log"
DISPLAY = ":99"
SINK = "VirtualSpeaker"

# Optional selector overrides for the portal login form
USER_SEL = _env(
    "USER_SELECTOR",
    'input[placeholder*="Email or Phone" i], input[placeholder*="Phone" i], '
    'input[type="tel"], input[type="email"], input[name*="phone" i], '
    'input[name*="mobile" i], input[name*="login" i], input[name*="user" i], '
    'input[name*="email" i], input[id*="user" i], input[id*="email" i], '
    'input[type="text"]',
)
PASS_SEL = _env("PASS_SELECTOR", 'input[type="password"]')

# Popup / tab guard configuration
_ALLOWED_RAW = _env("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _ALLOWED_RAW.split(",") if h.strip()]
BLOCK_TRACKERS = _env("BLOCK_TRACKERS", "1") != "0"

TRACKER_DOMAINS = [
    "google-analytics.com", "analytics.google.com", "googletagmanager.com",
    "doubleclick.net", "stats.g.doubleclick.net", "cm.g.doubleclick.net",
    "googlesyndication.com", "pagead2.googlesyndication.com",
    "googleadservices.com", "adservice.google.com",
    "hotjar.com", "hotjar.io", "static.hotjar.com",
    "facebook.net", "connect.facebook.net",
    "sentry.io", "amplitude.com", "mixpanel.com",
    "segment.io", "segment.com", "cdn.segment.com",
    "optimizely.com", "crazyegg.com", "mouseflow.com",
    "fullstory.com", "clarity.ms", "newrelic.com", "nr-data.net",
    "intercom.io", "intercomcdn.com",
]

TRACKER_RE = re.compile(
    r"^https?://(?:[^/]+\.)?(?:" + "|".join(re.escape(d) for d in TRACKER_DOMAINS) + r")(?:[:/?#]|$)",
    re.I,
)

TG_SIZE_LIMIT = 1950 * 1024 * 1024  # ~1.90 GB, safe headroom below Telegram 2000 MB limit

REQUIRED = [
    "PORTAL_URL", "PORTAL_USER", "PORTAL_PASS",
    "TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN", "TG_CHAT_ID",
]

END_RE = re.compile(
    r"(this meeting has been ended by host|the host has ended this meeting|"
    r"you have been removed|meeting has ended)",
    re.I,
)
WAITING_ROOM_RE = re.compile(
    r"(host will let you in|you are in the waiting room|waiting for the host to start)",
    re.I,
)
POST_MEETING_URL_RE = re.compile(r"(postattendee|/wc/leave|/leave\b|/meeting/end)", re.I)

HIDE_CSS = """
* { cursor: none !important; }
#wc-footer, #foot-bar, .footer-bar, .footer__inner, .meeting-info-container,
.zm-notification, [class*='toast'], [class*='Toast'], [class*='notification'],
[class*='join-leave'], [class*='tooltip'], [role='alert'], [role='tooltip'] {
  display: none !important; visibility: hidden !important; opacity: 0 !important;
}
"""

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1920,1080",
    "--window-position=0,0",
    "--kiosk",
    "--start-fullscreen",
    "--disable-infobars",
    "--disable-notifications",
    "--autoplay-policy=no-user-gesture-required",
    "--no-first-run",
    "--disable-features=Translate",
]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] {msg}", flush=True)


class StepTracker:
    """Logs milestones with timestamps and outputs GitHub Step Summary markdown."""

    def __init__(self, path: str = ""):
        self.path = path
        self._init_summary()

    def _init_summary(self):
        if not self.path:
            return
        header = (
            f"## 🎥 Zoom Class Recording Summary\n\n"
            f"- **Class Keyword:** `{CLASS_KEYWORD}`\n"
            f"- **Class Time:** `{CLASS_START} IST`" + (f" - `{CLASS_END} IST`" if CLASS_END else "") + "\n"
            f"- **Date:** `{datetime.now(IST).strftime('%Y-%m-%d')}`\n\n"
            f"| Step | Status | Time (IST) | Details |\n"
            f"| :--- | :---: | :---: | :--- |\n"
        )
        try:
            p = Path(self.path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(header, encoding="utf-8")
        except Exception:
            pass

    def record(self, step_name: str, status: str = "✅", details: str = "") -> None:
        now_str = datetime.now(IST).strftime("%H:%M:%S")
        log(f"{status} [{step_name}] {details}".strip())
        if not self.path:
            return
        safe_details = details.replace("|", "/")
        row = f"| {step_name} | {status} | {now_str} | {safe_details} |\n"
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(row)
        except Exception:
            pass


step = StepTracker(STEP_SUMMARY_PATH)


def redact(text: str) -> str:
    """Strip secrets from anything that might be logged or sent to Telegram."""
    for secret in (PORTAL_PASS, TG_BOT_TOKEN, TG_API_HASH):
        if secret:
            text = text.replace(secret, "***")
    return text


def mask_url(url: str) -> str:
    return url.split("?")[0]


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def class_start_dt() -> datetime:
    now = datetime.now(IST)
    if not CLASS_START or CLASS_START.lower() in ("now", "test", "immediate"):
        return now
    try:
        parts = [int(x) for x in CLASS_START.split(":")]
        if len(parts) >= 2:
            return now.replace(hour=parts[0], minute=parts[1], second=0, microsecond=0)
    except Exception:
        pass
    return now


def keyword_pattern(kw: str) -> re.Pattern:
    """Supports single keywords or pipe-separated variants like 'React JS|React'."""
    parts = [p.strip() for p in kw.split("|") if p.strip()]
    if len(parts) > 1:
        pat = "|".join(re.escape(p) for p in parts)
    elif parts:
        pat = re.escape(parts[0])
    else:
        pat = ".*"
    return re.compile(pat, re.I)


def _domain_base(host: str) -> str:
    parts = host.split(".")
    if len(parts) > 2 and parts[-2] in ("edu", "ac", "co", "gov", "org", "res", "net", "com"):
        return ".".join(parts[-3:])
    elif len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _host_matches(host: str, pattern: str) -> bool:
    """True when *host* equals *pattern* or is a subdomain of it."""
    if not pattern:
        return False
    return host == pattern or host.endswith("." + pattern)


def _is_zoom_host(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return bool(re.search(r"(^|\.)zoom(gov)?\.(us|com|cn)$", host, re.I))
    except Exception:
        return False


# --------------------------------------------------------------------------
# Telegram notifications (plain Bot API, independent of Pyrogram)
# --------------------------------------------------------------------------
def _bot_api(method: str, **params) -> None:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20):
        pass


async def notify(text: str) -> None:
    try:
        await asyncio.to_thread(
            _bot_api, "sendMessage", chat_id=TG_CHAT_ID, text=redact(text)[:3900]
        )
    except Exception as e:  # never let a notification break the run
        log(f"Telegram notification failed ({type(e).__name__})")


def _bot_photo(path: str, caption: str) -> None:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", TG_CHAT_ID), ("caption", redact(caption)[:1000])):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        )
    img_bytes = Path(path).read_bytes()
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"debug.png\"\r\n"
        f"Content-Type: image/png\r\n\r\n".encode()
        + img_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


async def notify_photo(zp, caption: str) -> None:
    try:
        shot = "/tmp/zoom_debug.png"
        await zp.screenshot(path=shot)
        await asyncio.to_thread(_bot_photo, shot, caption)
    except Exception as e:
        log(f"Telegram screenshot failed ({type(e).__name__})")


# --------------------------------------------------------------------------
# Virtual display / audio stack
# --------------------------------------------------------------------------
class MediaStack:
    def __init__(self):
        self.xvfb = None
        self.unclutter = None

    def start(self) -> None:
        os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/xdg-runtime")
        os.environ.setdefault("PULSE_RUNTIME_PATH", "/tmp/pulse")
        for key in ("XDG_RUNTIME_DIR", "PULSE_RUNTIME_PATH"):
            os.makedirs(os.environ[key], mode=0o700, exist_ok=True)
        os.environ["DISPLAY"] = DISPLAY

        # PulseAudio with a null sink that Chromium plays into
        sh(["pulseaudio", "-D", "--exit-idle-time=-1"])
        for _ in range(30):
            if sh(["pactl", "info"]).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("PulseAudio did not start")
        sh([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={SINK}", f"sink_properties=device.description={SINK}",
        ])
        sh(["pactl", "set-default-sink", SINK])
        if SINK not in sh(["pactl", "list", "short", "sinks"]).stdout:
            raise RuntimeError("Virtual audio sink was not created")

        # Virtual microphone source so Chromium / Zoom WebRTC audio engine
        # always detects an available audio input device
        try:
            sh([
                "pactl", "load-module", "module-null-sink",
                "sink_name=VirtualMic", "sink_properties=device.description=VirtualMic",
            ])
            sh(["pactl", "set-default-source", "VirtualMic.monitor"])
        except Exception:
            pass

        # Virtual 1080p display
        self.xvfb = subprocess.Popen(
            ["Xvfb", DISPLAY, "-screen", "0", "1920x1080x24", "-ac",
             "+extension", "GLX", "+render", "-noreset"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(40):
            if sh(["xdpyinfo", "-display", DISPLAY]).returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Xvfb did not start")

        # Hide the mouse cursor (non-fatal if unavailable)
        try:
            self.unclutter = subprocess.Popen(
                ["unclutter", "-idle", "0.2", "-root"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self.unclutter = None

    def stop(self) -> None:
        for proc in (self.unclutter, self.xvfb):
            if proc and proc.poll() is None:
                proc.terminate()


# --------------------------------------------------------------------------
# FFmpeg recorder — crash-safe MKV segments with auto-restart + MP4 remux
# --------------------------------------------------------------------------
class FFmpegRecorder:
    """Records to MKV segments (immediately seekable on crash).  If FFmpeg
    dies mid-class it is restarted automatically.  On stop, segments are
    concatenated and remuxed to a playable MP4 with faststart."""

    def __init__(self, out_path: str):
        self.out_path = out_path
        self.proc: subprocess.Popen | None = None
        self._stop_evt = threading.Event()
        self._seg_dir = Path(out_path).parent / "segments"
        self._segments: list[str] = []
        self._seg_idx = 0
        self._thread: threading.Thread | None = None

    def _seg_path(self) -> str:
        return str(self._seg_dir / f"seg_{self._seg_idx:04d}.mkv")

    def _launch(self) -> subprocess.Popen:
        path = self._seg_path()
        cmd = [
            "ffmpeg", "-y",
            "-thread_queue_size", "1024", "-f", "x11grab",
            "-video_size", "1920x1080", "-framerate", "30", "-i", f"{DISPLAY}.0",
            "-thread_queue_size", "1024", "-f", "pulse", "-i", f"{SINK}.monitor",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            path,
        ]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=open(FFMPEG_LOG, "ab"),
        )
        self._segments.append(path)
        return proc

    def start(self) -> None:
        self._seg_dir.mkdir(parents=True, exist_ok=True)
        self.proc = self._launch()
        time.sleep(2)
        if self.proc.poll() is not None:
            tail = Path(FFMPEG_LOG).read_text(errors="ignore")[-600:]
            raise RuntimeError(f"FFmpeg exited immediately: {tail}")
        self._thread = threading.Thread(target=self._watchdog, daemon=True)
        self._thread.start()

    def _watchdog(self) -> None:
        """Restart FFmpeg automatically if it crashes."""
        while not self._stop_evt.wait(2):
            if self.proc and self.proc.poll() is not None:
                self._seg_idx += 1
                log(f"FFmpeg crashed (exit {self.proc.returncode}); restarting as segment {self._seg_idx}")
                try:
                    self.proc = self._launch()
                    time.sleep(1)
                except Exception as e:
                    log(f"FFmpeg restart failed: {e}")

    @staticmethod
    def _graceful_stop(p: subprocess.Popen) -> None:
        """Send 'q' so FFmpeg finalises the container, then escalate."""
        if not p or p.poll() is not None:
            return
        try:
            p.stdin.write(b"q")
            p.stdin.flush()
            p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=180)
            return
        except subprocess.TimeoutExpired:
            pass
        p.send_signal(signal.SIGINT)
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()

    def stop(self) -> None:
        self._stop_evt.set()
        self._graceful_stop(self.proc)
        if self._thread:
            self._thread.join(timeout=5)
        self._finalize()

    def _finalize(self) -> None:
        """Concatenate MKV segments and remux to a single MP4."""
        valid = [s for s in self._segments
                 if Path(s).exists() and Path(s).stat().st_size > 0]
        if not valid:
            return
        if len(valid) == 1:
            self._remux(valid[0], self.out_path)
        else:
            log(f"Joining {len(valid)} recording segments")
            self._concat_remux(valid, self.out_path)

    def _remux(self, src: str, dst: str) -> None:
        r = sh(["ffmpeg", "-y", "-i", src, "-c", "copy",
                "-movflags", "+faststart", dst])
        if r.returncode != 0:
            log("Remux to MP4 failed; copying MKV as fallback")
            shutil.copy2(src, dst)

    def _concat_remux(self, segs: list[str], dst: str) -> None:
        lst = str(self._seg_dir / "concat.txt")
        with open(lst, "w") as f:
            for s in segs:
                f.write(f"file '{Path(s).as_posix()}'\n")
        r = sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", lst, "-c", "copy", "-movflags", "+faststart", dst])
        if r.returncode != 0:
            log("Segment concat failed; using largest segment as fallback")
            biggest = max(segs, key=lambda s: Path(s).stat().st_size)
            self._remux(biggest, dst)


# --------------------------------------------------------------------------
# Generic page helpers
# --------------------------------------------------------------------------
async def click_any(page, patterns, role="button", timeout=800):
    """Click the first visible element of `role` whose name matches a pattern.
    Searches the main page and every frame. Returns the matched pattern."""
    for frame in page.frames:
        for pat in patterns:
            try:
                loc = frame.get_by_role(role, name=re.compile(pat, re.I)).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=timeout)
                    return pat
            except Exception:
                continue
    return None


async def page_text(page) -> str:
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(await frame.evaluate("document.body ? document.body.innerText : ''"))
        except Exception:
            continue
    return "\n".join(chunks)


async def inject_hide_css(page) -> None:
    js = (
        "(css)=>{if(!document.getElementById('__rec_hide')){"
        "const s=document.createElement('style');s.id='__rec_hide';"
        "s.textContent=css;(document.head||document.documentElement).appendChild(s);}}"
    )
    for frame in page.frames:
        try:
            await frame.evaluate(js, HIDE_CSS)
        except Exception:
            continue


# --------------------------------------------------------------------------
# Tab closer — background task
# --------------------------------------------------------------------------
async def _tab_closer(context, portal_url: str, allowed_extra: list[str], zoom_active: asyncio.Event):
    """Closes tabs that are not the portal (before Zoom) or Zoom (always).
    Once Zoom opens (zoom_active set), the portal tab is closed too."""
    portal_host = urllib.parse.urlparse(portal_url).hostname or ""
    portal_base = _domain_base(portal_host)
    first_seen: dict[int, float] = {}

    while True:
        try:
            now = time.monotonic()
            pages = list(context.pages)
            if len(pages) <= 1:
                await asyncio.sleep(1)
                continue

            live = {id(p) for p in pages}
            for p in pages:
                first_seen.setdefault(id(p), now)
            for k in list(first_seen):
                if k not in live:
                    del first_seen[k]

            zoom_is_on = zoom_active.is_set() or any(_is_zoom_host(p.url) for p in pages)

            for p in pages:
                try:
                    # 3-second grace period for pages that are still redirecting
                    if now - first_seen.get(id(p), now) < 3:
                        continue
                    url = p.url
                    if not url or url.startswith(("about:", "chrome://")):
                        continue
                    if _is_zoom_host(url):
                        continue
                    host = urllib.parse.urlparse(url).hostname or ""
                    # Keep portal tab alive until Zoom is officially active
                    if not zoom_is_on and (_host_matches(host, portal_host) or _host_matches(host, portal_base)):
                        continue
                    if any(_host_matches(host, h) for h in allowed_extra):
                        continue
                    log(f"Blocked an unwanted tab: {mask_url(url)}")
                    await p.close()
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(1)


# --------------------------------------------------------------------------
# State 2: portal login + navigation
# --------------------------------------------------------------------------
async def login(page) -> None:
    step.record("Portal Login", "🌐", f"Opening portal: {mask_url(PORTAL_URL)}")
    await page.goto(PORTAL_URL, wait_until="domcontentloaded")
    try:
        pw_field = page.locator(PASS_SEL).first
        await pw_field.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        step.record("Portal Login", "ℹ️", "No password field visible; session already active")
        return

    step.record("Portal Login", "🔑", f"Entering credentials for user: {PORTAL_USER[:3]}***")
    user_field = page.locator(USER_SEL).first
    await user_field.fill(PORTAL_USER)
    await pw_field.fill(PORTAL_PASS)

    submitted = await click_any(page, [r"^log ?in$", r"sign ?in", r"^submit$", r"^login"])
    if not submitted:
        try:
            await page.locator('button[type="submit"], input[type="submit"]').first.click(timeout=1500)
        except Exception:
            await pw_field.press("Enter")

    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(1.0)
    try:
        await page.locator(PASS_SEL).first.wait_for(state="hidden", timeout=20000)
    except PWTimeout:
        step.record("Portal Login", "❌", "Login failed (password field still visible)")
        raise RuntimeError("Portal login appears to have failed (password field still visible)")
    step.record("Portal Login", "✅", "Login success! Portal session authenticated")
    await dismiss_popups(page)


async def open_todays_classes(page, *, first_time: bool = False) -> None:
    """Navigate to Live Classes > Today's Classes tab.
    Only logs step.record on the *first* call (first_time=True) to avoid
    spamming GitHub Actions Summary on every periodic refresh."""
    await dismiss_popups(page)
    if first_time:
        step.record("Live Classes", "📋", "Navigating to Live Classes / Today's Classes tab")
    try:
        await page.get_by_text(re.compile(r"Live\s*Classes", re.I)).first.click(timeout=8000)
    except Exception:
        pass
    try:
        await page.get_by_text(re.compile(r"Today.?s\s*Classes", re.I)).first.click(timeout=5000)
    except Exception:
        pass  # may already be the default tab
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(1.0)
    await dismiss_popups(page)
    if first_time:
        step.record("Live Classes", "✅", "Live Classes tab loaded successfully")


async def join_ready(btn) -> bool:
    try:
        if not await btn.is_visible() or not await btn.is_enabled():
            return False
        info = await btn.evaluate(
            "e=>({d:e.hasAttribute('disabled'),a:e.getAttribute('aria-disabled'),"
            "c:String(e.className||''),p:getComputedStyle(e).pointerEvents,"
            "cur:getComputedStyle(e).cursor})"
        )
        if info["d"] or info["a"] == "true" or info["p"] == "none" or info["cur"] == "not-allowed":
            return False
        if re.search(r"\bdisabled\b|btn-secondary|inactive", info["c"].lower()):
            return False
        return True
    except Exception:
        return False


async def find_active_join(page):
    """Find the join button for CLASS_KEYWORD. Searches candidate cards and
    prefers the one whose Join button is ready/active. Returns (card, button)."""
    join_any = page.locator("a, button, [role='button']").filter(
        has_text=re.compile(r"\bjoin\b", re.I)
    )
    kw_re = keyword_pattern(CLASS_KEYWORD)
    cards = (
        page.locator("div, li, tr, article, section")
        .filter(has_text=kw_re)
        .filter(has=join_any)
    )
    try:
        count = await cards.count()
    except Exception:
        return None, None
    if count == 0:
        return None, None

    fallback_card = None
    fallback_btn = None
    for i in reversed(range(count)):
        c = cards.nth(i)
        btn = c.locator("a, button, [role='button']").filter(
            has_text=re.compile(r"\bjoin\b", re.I)
        ).first
        try:
            if await btn.count() > 0:
                if fallback_btn is None:
                    fallback_card = c
                    fallback_btn = btn
                if await join_ready(btn):
                    return c, btn
        except Exception:
            continue

    return fallback_card, fallback_btn


# --------------------------------------------------------------------------
# State 3: precision Join-button polling
# --------------------------------------------------------------------------
async def wait_and_join(context, page):
    now = datetime.now(IST)
    is_immediate = not CLASS_START or CLASS_START.lower() in ("now", "test", "immediate")
    start_dt = class_start_dt()
    diff_to_start = (start_dt - now).total_seconds()

    # ----------------------------------------------------------------------
    # Scenario A: Explicit immediate test requested (e.g. class_start="now")
    # ----------------------------------------------------------------------
    if is_immediate:
        step.record(
            "Immediate Run",
            "🚀",
            f"Immediate run requested for '{CLASS_KEYWORD}'. Scanning for active Join button...",
        )
        start_dt = now
        diff_to_start = 0

    # ----------------------------------------------------------------------
    # Scenario B: Class was scheduled for hours ago (already concluded)
    # ----------------------------------------------------------------------
    elif diff_to_start < -(CLASS_DURATION_MIN * 60 + 1800):
        hours_ago = abs(diff_to_start) / 3600
        step.record(
            "Class Status",
            "⏹️",
            f"Class '{CLASS_KEYWORD}' was scheduled for {CLASS_START} IST ({hours_ago:.1f}h ago) and has already finished for today.",
        )
        await notify(f"ℹ️ Class '{CLASS_KEYWORD}' scheduled for {CLASS_START} IST has already concluded for today.")
        return None

    # ----------------------------------------------------------------------
    # Scenario C: Run triggered way too early (>45 minutes before class)
    # E.g. user ran at 08:40 AM for a 19:30 class.
    # We DO NOT wait 10 hours (runner would timeout at 4h).
    # We DO NOT click Join 10 hours early (portal button is not live yet).
    # Instead, we perform a clean pre-flight verification: verify login, verify card.
    # ----------------------------------------------------------------------
    elif diff_to_start > MAX_WAIT_MIN * 60:
        hours_away = diff_to_start / 3600
        mins_away = int(diff_to_start // 60)
        step.record(
            "Schedule Check",
            "ℹ️",
            f"Class '{CLASS_KEYWORD}' starts at {CLASS_START} IST ({hours_away:.1f}h / {mins_away}m away).",
        )
        card, btn = await find_active_join(page)
        if card is not None:
            step.record(
                "Portal Verification",
                "✅",
                f"Verified: Found '{CLASS_KEYWORD}' card on portal. Portal login & card search 100% operational.",
            )
        else:
            step.record(
                "Portal Verification",
                "ℹ️",
                f"Portal login successful. Card for '{CLASS_KEYWORD}' is not yet posted by host for today.",
            )
        step.record(
            "Scheduler Standby",
            "⏰",
            f"Standing by. Cloudflare Scheduler (https://class-scheduler.class-scheduler.workers.dev) triggers automatically 13 mins before {CLASS_START} IST.",
        )
        await notify(
            f"✅ Pre-flight check successful: portal login & navigation verified for '{CLASS_KEYWORD}'. "
            f"Class starts at {CLASS_START} IST ({hours_away:.1f}h away). Scheduler will trigger at class time."
        )
        return None

    # ----------------------------------------------------------------------
    # Scenario D: Class is in the near future (within 45 min) or ongoing!
    # ----------------------------------------------------------------------
    deadline = time.monotonic() + MAX_WAIT_MIN * 60
    last_wait_log = 0.0
    last_reload = time.monotonic()

    # --- Phase 1: Wait calmly until class start time (start_dt - 15s) ---
    while datetime.now(IST) < start_dt - timedelta(seconds=15):
        now = datetime.now(IST)
        rem_sec = int((start_dt - now).total_seconds())
        if rem_sec <= 15:
            break

        if time.monotonic() - last_wait_log >= 120 or last_wait_log == 0.0:
            last_wait_log = time.monotonic()
            mins, secs = divmod(rem_sec, 60)
            step.record(
                "Waiting for Class Time",
                "⏳",
                f"Class '{CLASS_KEYWORD}' starts at {CLASS_START} IST ({mins}m {secs:02d}s remaining). Waiting quietly...",
            )

        sleep_time = min(5.0, max(1.0, float(rem_sec - 15)))
        await asyncio.sleep(sleep_time)

    # --- Phase 2: Start time reached! One clean refresh to fetch active DOM ---
    if not is_immediate and diff_to_start > 0:
        step.record(
            "Class Time Reached",
            "🔄",
            f"Reached {CLASS_START} IST! Refreshing portal to fetch live Join button...",
        )
        try:
            await page.reload(wait_until="domcontentloaded")
            await open_todays_classes(page)
        except Exception as e:
            log(f"Initial class reload hiccup ({type(e).__name__})")
        last_reload = time.monotonic()
        last_wait_log = 0.0

    # --- Phase 3: Class is live. Scan for Join button, click, and enter Zoom ---
    while time.monotonic() < deadline:
        now = datetime.now(IST)
        card, btn = await find_active_join(page)
        found = card is not None and btn is not None

        if found:
            is_active = await join_ready(btn)
            too_early = (start_dt - now).total_seconds() > 30

            if is_active and not too_early:
                step.record(
                    "Join Button",
                    "🎯",
                    f"Join button is active and clickable! Clicking Join for '{CLASS_KEYWORD}'...",
                )
                prev_url = page.url
                zp = await click_join(context, page, btn, wait_sec=25)
                if zp is not None:
                    step.record("Join Button", "✅", "Clicked Join button; Zoom window opened successfully")
                    return zp

                step.record("Join Button", "🔄", "Join click did not open Zoom yet; waiting for host to activate meeting")
                await asyncio.sleep(5)
                since_reload = time.monotonic() - last_reload
                if since_reload >= 60:
                    try:
                        if page.url != prev_url:
                            await page.goto(prev_url, wait_until="domcontentloaded")
                        else:
                            await page.reload(wait_until="domcontentloaded")
                        await open_todays_classes(page)
                    except Exception as e:
                        log(f"Reload hiccup ({type(e).__name__})")
                    last_reload = time.monotonic()
                continue
            else:
                if time.monotonic() - last_wait_log >= 120:
                    last_wait_log = time.monotonic()
                    if too_early:
                        rem = int((start_dt - now).total_seconds())
                        mins, secs = divmod(rem, 60)
                        step.record(
                            "Waiting for Class Time",
                            "⏳",
                            f"Found '{CLASS_KEYWORD}' card. Waiting for class start time ({mins}m {secs:02d}s remaining)...",
                        )
                    else:
                        step.record(
                            "Waiting for Host",
                            "⏳",
                            f"Found '{CLASS_KEYWORD}' card. Waiting for host/teacher to activate Join button...",
                        )
                since_reload = time.monotonic() - last_reload
                if since_reload >= 60:
                    try:
                        await page.reload(wait_until="domcontentloaded")
                        await open_todays_classes(page)
                    except Exception as e:
                        log(f"Reload hiccup ({type(e).__name__})")
                    last_reload = time.monotonic()

                await asyncio.sleep(2)
                continue
        else:
            if time.monotonic() - last_wait_log >= 120:
                last_wait_log = time.monotonic()
                step.record(
                    "Searching Card",
                    "🔍",
                    f"Searching for '{CLASS_KEYWORD}' card in Today's Classes tab...",
                )

            since_reload = time.monotonic() - last_reload
            if since_reload >= 20:
                try:
                    await page.reload(wait_until="domcontentloaded")
                    await open_todays_classes(page)
                except Exception as e:
                    log(f"Reload hiccup ({type(e).__name__})")
                last_reload = time.monotonic()

        await asyncio.sleep(2)

    step.record("Waiting for Join", "❌", f"Join button for '{CLASS_KEYWORD}' did not become active within {MAX_WAIT_MIN} mins")
    raise TimeoutError(
        f"Join button for '{CLASS_KEYWORD}' did not become active within {MAX_WAIT_MIN} minutes"
    )


async def click_join(context, page, btn, wait_sec: int = 25):
    known_pages = set(context.pages)
    try:
        async with context.expect_page(timeout=6000) as new_page_info:
            await btn.click()
        await new_page_info.value
    except PWTimeout:
        pass  # same-tab redirect; handled by the URL scan below

    end = time.monotonic() + wait_sec
    while time.monotonic() < end:
        for p in context.pages:
            if re.search(r"zoom\.(us|com)", p.url):
                log(f"Captured Zoom URL: {mask_url(p.url)}")
                return p
        await asyncio.sleep(0.5)

    for p in list(context.pages):
        if p not in known_pages and not re.search(r"zoom\.(us|com)", p.url):
            try:
                await p.close()
            except Exception:
                pass
    return None


# --------------------------------------------------------------------------
# State 4: Zoom web client, strictly no mic / no video
# --------------------------------------------------------------------------
async def click_browser_join(zp) -> bool:
    pat = re.compile(r"join from (your )?browser", re.I)
    for frame in zp.frames:
        try:
            loc = frame.locator("a, button, [role='button']").filter(has_text=pat).first
            if await loc.count() and await loc.is_visible():
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


async def _has_webclient(context, zp):
    for p in context.pages:
        if "/wc/" in p.url:
            return p
        for f in p.frames:
            if "/wc/" in f.url:
                return p
    return None


async def to_web_client(context, zp):
    step.record("Zoom Client", "💻", "Navigating to Zoom in-browser Web Client")
    found = await _has_webclient(context, zp)
    if found:
        log(f"Web client active: {mask_url(found.url)}")
        return found

    clicked_browser = False
    for _ in range(30):
        found = await _has_webclient(context, zp)
        if found:
            log(f"Web client active: {mask_url(found.url)}")
            return found
        if not clicked_browser and await click_browser_join(zp):
            clicked_browser = True
            log("Clicked 'Join from browser'")
            end = time.monotonic() + 20
            while time.monotonic() < end:
                found = await _has_webclient(context, zp)
                if found:
                    log(f"Web client loaded: {mask_url(found.url)}")
                    return found
                if await in_meeting(zp):
                    return zp
                await asyncio.sleep(0.5)
            break
        await asyncio.sleep(0.5)

    if re.search(r"/(j|w)/", zp.url):
        new_url = re.sub(r"/(j|w)/", "/wc/join/", zp.url, count=1)
        log("Rewriting Zoom link to the web client")
        await zp.goto(new_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        log(f"After rewrite URL: {mask_url(zp.url)}")
    return zp


async def ensure_muted(zp) -> None:
    """Only ever clicks in the 'turn off' direction."""
    await click_any(zp, [r"^mute\b", r"^stop video"])


async def click_prejoin_join(zp) -> bool:
    for frame in zp.frames:
        for sel in [
            "button.preview-join-button",
            "button[class*='preview-join']",
            "button[class*='join-btn']",
            "button.zm-btn--primary",
            "[class*='preview'] button[type='button']",
        ]:
            try:
                btn = frame.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True, timeout=1500)
                    try:
                        await btn.evaluate("b => { b.click(); if (b.form) b.form.submit(); }")
                    except Exception:
                        pass
                    try:
                        await btn.press("Enter")
                    except Exception:
                        pass
                    log(f"Clicked pre-join button ({sel})")
                    return True
            except Exception:
                continue

        try:
            btns = frame.locator("button, [role='button']")
            count = await btns.count()
            for i in range(count):
                b = btns.nth(i)
                txt = (await b.inner_text() or "").strip()
                if re.fullmatch(r"join|join meeting", txt, re.I):
                    box = await b.bounding_box()
                    if box and box["y"] > 250:
                        await b.scroll_into_view_if_needed()
                        await b.click(force=True, timeout=1500)
                        try:
                            await b.evaluate("b => b.click()")
                        except Exception:
                            pass
                        log(f"Clicked pre-join button at y={box['y']:.0f}")
                        return True
        except Exception:
            continue
    return False


async def zoom_prejoin(zp) -> None:
    deadline = time.monotonic() + 60
    clicked_join = False
    step.record("Zoom Pre-Join", "⚙️", "Handling pre-join prompts (name entry, mic mute verification)")
    while time.monotonic() < deadline:
        if await in_meeting(zp):
            clicked_join = True
            break
        await click_browser_join(zp)
        await dismiss_popups(zp)
        await ensure_muted(zp)

        for frame in zp.frames:
            try:
                name_in = frame.locator(
                    "input#inputname, input[name='inputname'], input#display_name, "
                    "input[name='display_name'], input#input-for-name, "
                    "input[placeholder*='name' i], input[type='text']:visible"
                ).first
                if await name_in.count() and await name_in.is_visible():
                    val = await name_in.input_value()
                    if not val.strip():
                        await name_in.fill(PARTICIPANT_NAME)
                        step.record("Participant Name", "👤", f"Filled student display name: {PARTICIPANT_NAME}")
                        await name_in.press("Enter")
            except Exception:
                continue

        if await click_prejoin_join(zp):
            clicked_join = True
            step.record("Zoom Pre-Join", "🚀", "Clicked Zoom pre-join 'Join' button")
            await asyncio.sleep(2)
            if await in_meeting(zp):
                break
            text = await page_text(zp)
            if not re.search(r"cannot detect your camera|terms of service", text, re.I):
                break
        await asyncio.sleep(1)

    if not clicked_join:
        log("Join button on the pre-join screen was not found; continuing anyway")


POPUP_PATTERNS = [
    r"^got it$",
    r"^ok$",
    r"^i agree$",
    r"^i understand$",
    r"^accept( all)?( cookies)?$",
    r"^allow( all)?( cookies)?$",
    r"^dismiss$",
    r"^skip$",
    r"^not now$",
    r"^maybe later$",
    r"^no,? thanks$",
    r"^continue without (audio|microphone)$",
    r"^continue$",
]


async def dismiss_popups(zp) -> None:
    await click_any(zp, POPUP_PATTERNS)
    for frame in zp.frames:
        try:
            close_btn = frame.locator(
                "[role='dialog'] button[aria-label*='close' i], "
                ".zm-modal button[aria-label*='close' i], "
                "button[aria-label='Close' i]"
            ).first
            if await close_btn.count() and await close_btn.is_visible():
                label = (await close_btn.get_attribute("aria-label") or "").lower()
                if not re.search(r"leave|end|exit", label):
                    await close_btn.click(timeout=800)
        except Exception:
            continue


async def in_meeting(zp) -> bool:
    try:
        await zp.mouse.move(960, 1040)
    except Exception:
        pass
    for frame in zp.frames:
        try:
            loc = frame.locator(
                "#wc-footer, #foot-bar, [class*='footer-chat'], "
                "button[aria-label*='Leave' i], button:has-text('Leave'), "
                "button[aria-label*='Participants' i], button[aria-label*='Chat' i], "
                "button[aria-label*='Reactions' i], button[aria-label*='Share Screen' i]"
            ).first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            continue
    return False


async def wait_in_meeting(zp, max_wait: int = 300) -> None:
    """Wait through the waiting room; start recording only once inside."""
    deadline = time.monotonic() + max_wait
    started_wait = time.monotonic()
    last_diag = -60.0
    sent_debug = False
    while time.monotonic() < deadline:
        text = await page_text(zp)
        if END_RE.search(text):
            raise RuntimeError("Meeting ended before the recording could start")
        await dismiss_popups(zp)
        if await in_meeting(zp) and not WAITING_ROOM_RE.search(text):
            step.record("Meeting Status", "🎥", "Confirmed inside Zoom meeting! Ready to record")
            return
        elapsed = time.monotonic() - started_wait
        if elapsed - last_diag >= 30:
            last_diag = elapsed
            snippet = re.sub(r"\s+", " ", text).strip()[:160]
            log(f"Waiting for meeting UI ({mask_url(zp.url)}): {snippet}")
        if not sent_debug and elapsed >= 30:
            sent_debug = True
            await notify_photo(zp, f"⏳ Waiting to enter Zoom ({mask_url(zp.url)})")
        # If still stuck on the pre-join screen, retry clicking the real pre-join button
        if elapsed >= 4 and not WAITING_ROOM_RE.search(text) and int(elapsed) % 5 < 2:
            await click_prejoin_join(zp)
        await asyncio.sleep(2)
    step.record("Meeting Status", "⚠️", "Could not confirm meeting UI; starting recording anyway")


async def join_audio(zp) -> None:
    """Listen-only: join computer audio so we can hear, keep the mic muted."""
    step.record("Zoom Audio", "🔊", "Connecting to computer audio (listen-only, mic muted)")
    patterns = [
        r"join audio by computer",
        r"join with computer audio",
        r"computer audio",
        r"call via device audio",
        r"join computer audio",
        r"connect audio",
        r"^join audio",
    ]
    for _ in range(10):
        for frame in zp.frames:
            try:
                btn = frame.locator(
                    "button.join-audio-by-computer, button[class*='join-audio'], "
                    "button:has-text('Join Audio'), button:has-text('Computer Audio'), "
                    "button[aria-label*='Join Audio' i]"
                ).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=1500)
                    break
            except Exception:
                pass
        if await click_any(zp, patterns):
            break
        await asyncio.sleep(1.5)
    await ensure_muted(zp)
    await dismiss_popups(zp)


# --------------------------------------------------------------------------
# State 5: clean full screen
# --------------------------------------------------------------------------
async def go_fullscreen(zp) -> None:
    try:
        already = await zp.evaluate(
            "window.innerWidth >= screen.width - 2 && window.innerHeight >= screen.height - 2"
        )
        if not already:  # kiosk mode is normally fullscreen; F11 would toggle it OFF
            await zp.keyboard.press("F11")
            await asyncio.sleep(0.5)
    except Exception:
        pass
    try:
        if await click_any(zp, [r"^view$"]):
            if not await click_any(zp, [r"full screen"], role="menuitem"):
                await zp.keyboard.press("Escape")
    except Exception:
        pass
    await inject_hide_css(zp)


# --------------------------------------------------------------------------
# State 6: meeting monitor
# --------------------------------------------------------------------------
async def monitor(zp, started: float) -> str:
    failures = 0
    last_housekeeping = 0.0
    last_progress_log = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= MAX_MEETING_SEC:
            return "maximum duration reached"
        if zp.is_closed():
            return "Zoom tab closed"
        url = zp.url
        if "zoom." not in url or POST_MEETING_URL_RE.search(url):
            return "redirected away from the meeting"
        try:
            text = await page_text(zp)
            failures = 0
            if END_RE.search(text):
                return "meeting ended by host"
        except Exception:
            failures += 1
            if failures > 20:
                return "page became unreachable"

        if time.monotonic() - last_progress_log >= 300:
            last_progress_log = time.monotonic()
            mins_elapsed = int(elapsed / 60)
            step.record("Recording Active", "⏺️", f"Recording in progress: {mins_elapsed} mins elapsed | Host streaming")

        if time.monotonic() - last_housekeeping > 10:
            last_housekeeping = time.monotonic()
            await dismiss_popups(zp)
            await inject_hide_css(zp)
        await asyncio.sleep(3)


# --------------------------------------------------------------------------
# State 7: Telegram upload (MTProto, auto-split with size re-check)
# --------------------------------------------------------------------------
def probe_duration(path: str) -> float:
    out = sh([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", path,
    ]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def make_thumb(path: str) -> str | None:
    thumb = str(Path(path).with_suffix(".jpg"))
    dur = probe_duration(path)
    seek = "1" if dur < 10 else "10"
    res = sh([
        "ffmpeg", "-y", "-ss", seek, "-i", path, "-vframes", "1",
        "-vf", "scale=320:-2", "-q:v", "6", thumb,
    ])
    return thumb if res.returncode == 0 and os.path.exists(thumb) else None


def _resplit(part_path: str) -> list[str]:
    """Re-split a single part that exceeded the Telegram size limit."""
    size = os.path.getsize(part_path)
    duration = probe_duration(part_path)
    if duration <= 0:
        return [part_path]
    n_chunks = math.ceil(size / (TG_SIZE_LIMIT * 0.85))
    seg_time = max(30, int(duration / n_chunks))
    stem = Path(part_path).stem
    out_dir = Path(part_path).parent / f"{stem}_resplit"
    out_dir.mkdir(exist_ok=True)
    res = sh([
        "ffmpeg", "-y", "-i", part_path, "-map", "0", "-c", "copy",
        "-f", "segment", "-segment_time", str(seg_time),
        "-reset_timestamps", "1", "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        str(out_dir / f"{stem}_sub_%03d.mp4"),
    ])
    sub_parts = sorted(str(p) for p in out_dir.glob(f"{stem}_sub_*.mp4"))
    return sub_parts if sub_parts else [part_path]


def split_file(path: str) -> list[str]:
    size = os.path.getsize(path)
    duration = probe_duration(path)
    if duration <= 0:
        raise RuntimeError("Cannot split: unable to read video duration")
    avg_rate = size / duration  # bytes per second
    seg_time = max(60, int(TG_SIZE_LIMIT * 0.9 / avg_rate))  # 10% headroom
    out_dir = Path(path).parent / "parts"
    out_dir.mkdir(exist_ok=True)
    res = sh([
        "ffmpeg", "-y", "-i", path, "-map", "0", "-c", "copy",
        "-f", "segment", "-segment_time", str(seg_time),
        "-reset_timestamps", "1", "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        str(out_dir / "part_%03d.mp4"),
    ])
    parts = sorted(str(p) for p in out_dir.glob("part_*.mp4"))
    if res.returncode != 0 or not parts:
        raise RuntimeError("Splitting the recording failed")

    # Re-check: if any part still exceeds the limit, re-split it
    final: list[str] = []
    for part in parts:
        if os.path.getsize(part) > TG_SIZE_LIMIT:
            log(f"Part {Path(part).name} exceeds Telegram size limit; re-splitting")
            final.extend(_resplit(part))
        else:
            final.append(part)
    return final


def _chat_id():
    return int(TG_CHAT_ID) if re.fullmatch(r"-?\d+", TG_CHAT_ID) else TG_CHAT_ID


async def upload_to_telegram(path: str) -> None:
    from pyrogram import Client
    from pyrogram.errors import FloodWait

    size = os.path.getsize(path)
    parts = [path] if size <= TG_SIZE_LIMIT else await asyncio.to_thread(split_file, path)
    total = len(parts)
    base_caption = f"🎥 {CLASS_KEYWORD} | {datetime.now(IST).strftime('%Y-%m-%d')}"
    step.record("Telegram Upload", "📤", f"Uploading {total} part(s) ({size / 1024**3:.2f} GB total) to @Meeting_recorder_bot")

    async with Client(
        "recorder", api_id=int(TG_API_ID), api_hash=TG_API_HASH,
        bot_token=TG_BOT_TOKEN, in_memory=True,
    ) as app:
        for idx, part in enumerate(parts, 1):
            caption = base_caption + (f" (Part {idx}/{total})" if total > 1 else "")
            duration = int(await asyncio.to_thread(probe_duration, part))
            thumb = await asyncio.to_thread(make_thumb, part)
            for attempt in range(1, 4):
                try:
                    await app.send_video(
                        chat_id=_chat_id(), video=part, caption=caption,
                        supports_streaming=True, width=1920, height=1080,
                        duration=duration, thumb=thumb,
                    )
                    step.record("Telegram Upload", "✅", f"Part {idx}/{total} uploaded successfully")
                    break
                except FloodWait as fw:
                    await asyncio.sleep(int(fw.value) + 1)
                except Exception as e:
                    log(f"Upload attempt {attempt} failed ({type(e).__name__})")
                    if attempt == 3:
                        raise
                    await asyncio.sleep(5 * attempt)


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------
async def main() -> int:
    missing = [n for n in REQUIRED if not _env(n)]
    if missing:
        print("Missing required environment variables: " + ", ".join(missing), file=sys.stderr)
        return 2

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    try:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError):
        pass

    media = MediaStack()
    recorder = FFmpegRecorder(RECORD_PATH)
    error: str | None = None
    end_reason = ""
    tab_closer_task: asyncio.Task | None = None
    zoom_active = asyncio.Event()

    try:
        media.start()
        step.record("Virtual Environment", "🖥️", "Virtual 1080p display (Xvfb) and PulseAudio null sink ready")
        log("Virtual display and audio are ready")

        prof_dir = Path("/tmp/pw-profile")
        shutil.rmtree(prof_dir, ignore_errors=True)
        (prof_dir / "Default").mkdir(parents=True, exist_ok=True)
        (prof_dir / "Default" / "Preferences").write_text(json.dumps({
            "protocol_handler": {
                "excluded_schemes": {"zoommtg": True, "zoomus": True, "zoom": True}
            }
        }))

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                str(prof_dir),
                headless=False,
                args=CHROMIUM_ARGS,
                ignore_default_args=["--enable-automation"],
                permissions=[],
                accept_downloads=False,
                no_viewport=True,
            )

            # --- Inject popup_guard.js into every page/frame ---------------
            guard_js = Path("/app/popup_guard.js")
            if not guard_js.exists():
                guard_js = Path(__file__).parent / "popup_guard.js"
            if guard_js.exists():
                await context.add_init_script(path=str(guard_js))
                log("Injected popup_guard.js")
            else:
                log("popup_guard.js not found; skipping in-page popup guard")

            # --- Stealth + zoommtg:// blocker init script ------------------
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                "window.chrome = window.chrome || { runtime: {} };"
                "const _open=window.open;window.open=(u,...a)=>"
                "(u&&/^zoom(mtg|us)?:/i.test(String(u))?null:_open.call(window,u,...a));"
            )

            # --- Block zoommtg:// navigation at the network layer ---------
            async def _abort_route(route):
                await route.abort()

            await context.route(
                re.compile(r"^zoom(mtg|us)?://", re.I),
                _abort_route,
            )

            # --- Block tracker / ad domains --------------------------------
            if BLOCK_TRACKERS and TRACKER_DOMAINS:
                async def _block_tracker(route):
                    await route.abort()

                await context.route(TRACKER_RE, _block_tracker)
                log(f"Tracker blocking enabled ({len(TRACKER_DOMAINS)} domains)")

            # --- Auto-dismiss JS alert/confirm, refuse downloads -----------
            async def _dismiss_dialog(d):
                try:
                    await d.dismiss()
                except Exception:
                    pass

            async def _cancel_download(dl):
                try:
                    await dl.cancel()
                except Exception:
                    pass

            def _attach_page_handlers(p):
                p.on("dialog", lambda d: asyncio.create_task(_dismiss_dialog(d)))
                p.on("download", lambda dl: asyncio.create_task(_cancel_download(dl)))

            context.on("page", _attach_page_handlers)
            for p in context.pages:
                _attach_page_handlers(p)

            # --- Background tab closer -------------------------------------
            tab_closer_task = asyncio.create_task(
                _tab_closer(context, PORTAL_URL, ALLOWED_HOSTS, zoom_active)
            )

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await login(page)
                await open_todays_classes(page, first_time=True)
                zoom_page = await wait_and_join(context, page)

                if zoom_page is None:
                    step.record("Run Result", "✅", "Pre-flight verification completed successfully. No recording required.")
                    return 0

                zoom_active.set()
                await zoom_page.bring_to_front()

                zoom_page = await to_web_client(context, zoom_page)
                await zoom_prejoin(zoom_page)
                await wait_in_meeting(zoom_page)
                await join_audio(zoom_page)

                recorder.start()
                started = time.monotonic()
                step.record("1080p Recording", "🎥", f"Recording started (1080p @ 30fps) for '{CLASS_KEYWORD}'")
                await notify("✅ Joined class & started 1080p recording")

                await go_fullscreen(zoom_page)
                end_reason = await monitor(zoom_page, started)
                step.record("Meeting Concluded", "🏁", f"Meeting finished: {end_reason}")
                log(f"Recording ends: {end_reason}")
            except asyncio.CancelledError:
                error = "run was cancelled"
                raise
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                step.record("Run Error", "❌", f"Error: {redact(error)[:100]}")
                log(f"Error: {redact(error)[:300]}")
                await notify(f"⚠️ Alert: {error}")
            finally:
                if tab_closer_task:
                    tab_closer_task.cancel()
                # ALWAYS finalize FFmpeg so the recording is valid and uploadable
                step.record("Video Processing", "🔄", "Stopping FFmpeg & concatenating/remuxing segments to MP4 (+faststart)")
                await asyncio.to_thread(recorder.stop)
                try:
                    await context.close()
                except Exception:
                    pass
    except asyncio.CancelledError:
        error = error or "run was cancelled"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log(f"Fatal error: {redact(error)[:300]}")
        await notify(f"⚠️ Alert: {error}")
    finally:
        media.stop()

    # Upload whatever was recorded, even after an error
    out = Path(RECORD_PATH)
    if out.exists() and out.stat().st_size > 1_000_000:
        dur = probe_duration(str(out))
        size_mb = out.stat().st_size / (1024 * 1024)
        dur_str = f"{int(dur // 60)}m {int(dur % 60)}s"
        step.record("Video Processing", "✅", f"Faststart MP4 ready: {size_mb:.1f} MB (Duration: {dur_str})")
        try:
            await upload_to_telegram(str(out))
        except Exception as e:
            step.record("Telegram Upload", "❌", f"Upload failed ({type(e).__name__})")
            log(f"Upload failed ({type(e).__name__})")
            await notify(f"⚠️ Alert: recording finished but Telegram upload failed ({type(e).__name__})")
            return 1
        step.record("Telegram Upload", "✅", "Uploaded recording to Telegram successfully!")
        await notify("✅ Uploaded recording to Telegram")
    else:
        if not error:
            step.record("Recording Result", "⚠️", "No valid recording was produced (>1MB)")
            await notify("⚠️ Alert: no recording was produced")
        return 1

    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
