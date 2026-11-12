#!/usr/bin/env python3
"""
Autonomous zero-device 1080p Zoom class recorder + Telegram archiver.

Pipeline:  Xvfb + PulseAudio -> Chromium (Playwright) -> portal login ->
           poll Join button -> Zoom web client (muted, no camera) ->
           FFmpeg x11grab/pulse recording -> crash-safe finalize ->
           Pyrogram MTProto upload (auto-split above ~1.95 GB).

All configuration comes from environment variables (GitHub Secrets).
"""
import asyncio
import math
import os
import re
import signal
import subprocess
import sys
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
    return os.environ.get(name, default).strip()


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
CLASS_DURATION_MIN = int(_env("CLASS_DURATION_MIN", "90"))
MAX_WAIT_MIN = int(_env("MAX_WAIT_MIN", "45"))  # wait for Join to activate
MAX_MEETING_SEC = int(_env("MAX_MEETING_SEC", str(CLASS_DURATION_MIN * 60)))  # 90 min default
PARTICIPANT_NAME = _env("PARTICIPANT_NAME", "") or (
    PORTAL_USER.split("@")[0] if "@" in PORTAL_USER else "Student"
)

RECORD_PATH = _env("RECORD_PATH", "/tmp/class_recording.mp4")
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

TG_SIZE_LIMIT = int(1.95 * 1024**3)  # single-file ceiling for MTProto upload

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
    hh, mm = (int(x) for x in CLASS_START.split(":"))
    return datetime.now(IST).replace(hour=hh, minute=mm, second=0, microsecond=0)


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
# FFmpeg recorder
# --------------------------------------------------------------------------
class FFmpegRecorder:
    def __init__(self, out_path: str):
        self.out_path = out_path
        self.proc = None

    def start(self) -> None:
        Path(self.out_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y",
            "-thread_queue_size", "1024", "-f", "x11grab",
            "-video_size", "1920x1080", "-framerate", "30", "-i", f"{DISPLAY}.0",
            "-thread_queue_size", "1024", "-f", "pulse", "-i", f"{SINK}.monitor",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            self.out_path,
        ]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=open(FFMPEG_LOG, "wb"),
        )
        time.sleep(2)
        if self.proc.poll() is not None:
            tail = Path(FFMPEG_LOG).read_text(errors="ignore")[-600:]
            raise RuntimeError(f"FFmpeg exited immediately: {tail}")

    def stop(self) -> None:
        """Graceful 'q' so the MP4 is finalized and playable, with fallbacks."""
        p = self.proc
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
# State 2: portal login + navigation
# --------------------------------------------------------------------------
async def login(page) -> None:
    await page.goto(PORTAL_URL, wait_until="domcontentloaded")
    try:
        pw_field = page.locator(PASS_SEL).first
        await pw_field.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        log("No login form found; assuming an existing session")
        return

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
        raise RuntimeError("Portal login appears to have failed (password field still visible)")
    log("Logged in to portal")


async def open_todays_classes(page) -> None:
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


def card_locator(page):
    join_any = page.locator("a, button, [role='button']").filter(
        has_text=re.compile(r"\bjoin\b", re.I)
    )
    # .last = innermost matching element (outer wrappers also match)
    return (
        page.locator("div, li, tr, article, section")
        .filter(has_text=re.compile(re.escape(CLASS_KEYWORD), re.I))
        .filter(has=join_any)
        .last
    )


def join_button(card):
    return card.locator("a, button, [role='button']").filter(
        has_text=re.compile(r"\bjoin\b", re.I)
    ).first


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


# --------------------------------------------------------------------------
# State 3: precision Join-button polling
# --------------------------------------------------------------------------
async def wait_and_join(context, page):
    start_dt = class_start_dt()
    reload_until = start_dt - timedelta(seconds=15)
    deadline = time.monotonic() + MAX_WAIT_MIN * 60
    last_reload = time.monotonic()
    reloaded_at_start = False
    early_click_failed = False
    log(f"Waiting for the Join button (class start {CLASS_START} IST)")

    while time.monotonic() < deadline:
        now = datetime.now(IST)
        card = card_locator(page)
        try:
            found = await card.count() > 0
        except Exception:
            found = False

        if found and (now >= start_dt or not early_click_failed):
            btn = join_button(card)
            if await join_ready(btn):
                log("Join button is active; clicking")
                prev_url = page.url
                zp = await click_join(
                    context, page, btn, wait_sec=25 if now >= start_dt else 12
                )
                if zp is not None:
                    return zp
                if now < start_dt:
                    early_click_failed = True
                    log(f"Join did not open Zoom yet; waiting until {CLASS_START} IST")
                else:
                    log("Join click did not open Zoom yet; retrying after reload")
                try:
                    if page.url != prev_url:
                        await page.goto(prev_url, wait_until="domcontentloaded")
                    else:
                        await page.reload(wait_until="domcontentloaded")
                    await open_todays_classes(page)
                except Exception as e:
                    log(f"Reload hiccup ({type(e).__name__})")
                last_reload = time.monotonic()
                await asyncio.sleep(3)
                continue

        since_reload = time.monotonic() - last_reload
        need_reload = False
        if now < reload_until and since_reload >= 15:
            need_reload = True  # lightweight refresh every 15 s until 7:29:45
        elif now >= start_dt and not reloaded_at_start:
            need_reload = True  # one refresh exactly at the start time
            reloaded_at_start = True
        elif now >= start_dt and since_reload >= 30:
            need_reload = True  # safety net if the button is still inactive
        elif not found and since_reload >= 10:
            need_reload = True  # card missing: page state probably lost

        if need_reload:
            try:
                await page.reload(wait_until="domcontentloaded")
                await open_todays_classes(page)
            except Exception as e:
                log(f"Reload hiccup ({type(e).__name__})")
            last_reload = time.monotonic()

        await asyncio.sleep(1.5 if now < reload_until else 1.0)

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
    found = await _has_webclient(context, zp)
    if found:
        log(f"Web client active: {mask_url(found.url)}")
        return found

    clicked_browser = False
    for _ in range(10):
        found = await _has_webclient(context, zp)
        if found:
            log(f"Web client active: {mask_url(found.url)}")
            return found
        if not clicked_browser and await click_browser_join(zp):
            clicked_browser = True
            log("Clicked 'Join from browser'")
            end = time.monotonic() + 18
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


async def zoom_prejoin(zp) -> None:
    deadline = time.monotonic() + 60
    clicked_join = False
    while time.monotonic() < deadline:
        if await in_meeting(zp):
            clicked_join = True
            break
        await click_browser_join(zp)
        await click_any(zp, [r"^accept cookies", r"^accept all", r"^i agree$"])
        await click_any(zp, [r"continue without (audio|microphone)"])
        await ensure_muted(zp)

        for frame in zp.frames:
            try:
                name_in = frame.locator("#input-for-name, input[placeholder*='name' i]").first
                if await name_in.count() and await name_in.is_visible():
                    if not (await name_in.input_value()).strip():
                        await name_in.fill(PARTICIPANT_NAME)
            except Exception:
                continue

        if await click_any(zp, [r"^join$", r"^join meeting$"]):
            clicked_join = True
            break
        await asyncio.sleep(1)

    if not clicked_join:
        log("Join button on the pre-join screen was not found; continuing anyway")


async def dismiss_popups(zp) -> None:
    await click_any(zp, [r"^got it$", r"^ok$", r"^i agree$", r"^dismiss$"])


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
                "button[aria-label*='Audio' i], button[aria-label*='Participants' i], "
                "button[aria-label*='Chat' i], #wc-container-left"
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
            log("Inside the meeting")
            return
        elapsed = time.monotonic() - started_wait
        if elapsed - last_diag >= 30:
            last_diag = elapsed
            snippet = re.sub(r"\s+", " ", text).strip()[:160]
            log(f"Waiting for meeting UI ({mask_url(zp.url)}): {snippet}")
        if not sent_debug and elapsed >= 30:
            sent_debug = True
            await notify_photo(zp, f"⏳ Waiting to enter Zoom ({mask_url(zp.url)})")
        await asyncio.sleep(2)
    log("Could not confirm meeting UI; starting the recording anyway")


async def join_audio(zp) -> None:
    """Listen-only: join computer audio so we can hear, keep the mic muted."""
    for _ in range(8):
        if await click_any(
            zp,
            [r"join audio by computer", r"join with computer audio", r"computer audio"],
        ):
            break
        if await click_any(zp, [r"^join audio"]):
            await asyncio.sleep(1)
            continue
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
    while True:
        if time.monotonic() - started >= MAX_MEETING_SEC:
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

        if time.monotonic() - last_housekeeping > 30:
            last_housekeeping = time.monotonic()
            await dismiss_popups(zp)
            await inject_hide_css(zp)
        await asyncio.sleep(3)


# --------------------------------------------------------------------------
# State 7: Telegram upload (MTProto, auto-split)
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
    res = sh([
        "ffmpeg", "-y", "-ss", "10", "-i", path, "-vframes", "1",
        "-vf", "scale=320:-2", "-q:v", "6", thumb,
    ])
    return thumb if res.returncode == 0 and os.path.exists(thumb) else None


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
    return parts


def _chat_id():
    return int(TG_CHAT_ID) if re.fullmatch(r"-?\d+", TG_CHAT_ID) else TG_CHAT_ID


async def upload_to_telegram(path: str) -> None:
    from pyrogram import Client
    from pyrogram.errors import FloodWait

    size = os.path.getsize(path)
    parts = [path] if size <= TG_SIZE_LIMIT else await asyncio.to_thread(split_file, path)
    total = len(parts)
    base_caption = f"🎥 {CLASS_KEYWORD} | {datetime.now(IST).strftime('%Y-%m-%d')}"
    log(f"Uploading {total} file(s), {size / 1024**3:.2f} GB total")

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
                    log(f"Uploaded part {idx}/{total}")
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

    try:
        media.start()
        log("Virtual display and audio are ready")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False, args=CHROMIUM_ARGS, ignore_default_args=["--enable-automation"]
            )
            # permissions=[] hard-blocks microphone and camera
            context = await browser.new_context(permissions=[], no_viewport=True)
            page = await context.new_page()
            try:
                await login(page)
                await open_todays_classes(page)
                zoom_page = await wait_and_join(context, page)

                zoom_page = await to_web_client(context, zoom_page)
                await zoom_prejoin(zoom_page)
                await wait_in_meeting(zoom_page)
                await join_audio(zoom_page)

                recorder.start()
                started = time.monotonic()
                log("Recording started")
                await notify("✅ Joined class & started 1080p recording")

                await go_fullscreen(zoom_page)
                end_reason = await monitor(zoom_page, started)
                log(f"Recording ends: {end_reason}")
            except asyncio.CancelledError:
                error = "run was cancelled"
                raise
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log(f"Error: {redact(error)[:300]}")
                await notify(f"⚠️ Alert: {error}")
            finally:
                # ALWAYS finalize FFmpeg so the MP4 is valid and uploadable
                await asyncio.to_thread(recorder.stop)
                try:
                    await browser.close()
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
        try:
            await upload_to_telegram(str(out))
        except Exception as e:
            log(f"Upload failed ({type(e).__name__})")
            await notify(f"⚠️ Alert: recording finished but Telegram upload failed ({type(e).__name__})")
            return 1
    else:
        if not error:
            await notify("⚠️ Alert: no recording was produced")
        return 1

    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
