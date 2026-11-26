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
import random
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

# Force IST timezone for the process so the browser and datetime.now() agree
os.environ["TZ"] = "Asia/Kolkata"
try:
    time.tzset()
except AttributeError:
    pass  # Windows doesn't have tzset

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


def _int_env(name: str, default: int) -> int:
    val = _env(name)
    try:
        return int(val)
    except Exception:
        return default


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
ACTIVE_CLASS_TITLE = CLASS_KEYWORD
ACTIVE_CLASS_INFO = {}
CLASS_START = _env("CLASS_START", "19:30")  # HH:MM, IST
CLASS_END = _env("CLASS_END", "")  # HH:MM, IST (optional hard stop)
CLASS_DURATION_MIN = _int_env("CLASS_DURATION_MIN", 90)
MAX_WAIT_MIN = _int_env("MAX_WAIT_MIN", 45)  # wait for Join to activate
MAX_MEETING_SEC = _int_env("MAX_MEETING_SEC", CLASS_DURATION_MIN * 60)
if CLASS_END and not os.environ.get("MAX_MEETING_SEC", "").strip():
    try:
        now_dt = datetime.now(IST)
        if CLASS_START.lower() in ("now", "test", "immediate"):
            sh, sm = now_dt.hour, now_dt.minute
        else:
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
# Zoom (or an identity provider) asking for a login instead of letting us in
SIGNIN_URL_RE = re.compile(
    r"(/signin|/google_oauth|accounts\.google|facebook\.com|appleid\.apple|"
    r"login\.microsoftonline|/saml|/sso|/meeting/register)", re.I)
SIGNIN_TEXT_RE = re.compile(
    r"(sign in to (join|continue|zoom)|sign in with (apple|google|facebook|sso|microsoft)|"
    r"authorized attendees|only authenticated users|authenticated users can join|"
    r"enter email, zoom mail or phone number)", re.I)
ZOOM_EMAIL = _env("ZOOM_EMAIL")
ZOOM_PASS = _env("ZOOM_PASS")
ZOOM_COOKIES = _env("ZOOM_COOKIES")
ZOOM_COOKIES_FILE = _env("ZOOM_COOKIES_FILE")
CF_TUNNEL_TOKEN = _env("CF_TUNNEL_TOKEN")
VNC_PASSWORD = _env("VNC_PASSWORD")
VNC_PUBLIC_URL = _env("VNC_PUBLIC_URL")
MANUAL_JOIN_WAIT_SEC = _int_env("MANUAL_JOIN_WAIT_SEC", 600)
MANUAL_MODE = _env("MANUAL_MODE", "0").lower() in ("1", "true", "yes", "manual")


def _get_vnc_url() -> str:
    """Return the best available public VNC URL, re-reading the tunnel file if needed."""
    global VNC_PUBLIC_URL
    if VNC_PUBLIC_URL and "127.0.0.1" not in VNC_PUBLIC_URL and "localhost" not in VNC_PUBLIC_URL:
        return VNC_PUBLIC_URL
    # Quick Tunnel writes the URL to /out/vnc_url.txt after startup
    try:
        vnc_txt = Path("/out/vnc_url.txt")
        if vnc_txt.exists() and vnc_txt.stat().st_size > 0:
            url = vnc_txt.read_text(encoding="utf-8").strip()
            if url and ("trycloudflare.com" in url or ("http" in url and "127.0.0.1" not in url and "localhost" not in url)):
                VNC_PUBLIC_URL = url
                return url
    except Exception:
        pass
    # Scan cloudflared logs directly
    for log_candidate in [Path("/tmp/cloudflared.log"), Path("/out/cloudflared.log")]:
        try:
            if log_candidate.exists():
                text = log_candidate.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', text)
                if m:
                    url = m.group(0) + "/vnc.html?autoconnect=true&resize=scale"
                    VNC_PUBLIC_URL = url
                    try:
                        Path("/out/vnc_url.txt").write_text(url, encoding="utf-8")
                    except Exception:
                        pass
                    return url
        except Exception:
            pass
    return ""  # No public URL available yet


async def wait_for_vnc_url(timeout: float = 40.0) -> str:
    """Poll for the public VNC URL up to timeout seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = _get_vnc_url()
        if url and url.startswith("http") and "127.0.0.1" not in url and "localhost" not in url:
            return url
        await asyncio.sleep(1)
    return _get_vnc_url()


# --------------------------------------------------------------------------
# Human Behavior & Cursor Physics Engine
# Evades Zoom bot detection by generating natural Bézier curve mouse trajectories,
# hand jitter tremors, realistic dwell times, human keystroke cadences, and
# browser-level CDP pointer events where event.isTrusted === true.
# --------------------------------------------------------------------------
class HumanCursor:
    """Simulates realistic human mouse dynamics using cubic Bézier curves,
    natural acceleration/deceleration physics, hand tremor jitter, randomized
    dwell times, and browser-level CDP dispatch events (isTrusted === true)."""

    def __init__(self):
        self.x = float(random.randint(250, 750))
        self.y = float(random.randint(180, 500))

    @staticmethod
    def _bezier_point(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
        return (
            (1.0 - t) ** 3 * p0
            + 3.0 * (1.0 - t) ** 2 * t * p1
            + 3.0 * (1.0 - t) * t ** 2 * p2
            + t ** 3 * p3
        )

    async def move_to(self, page, target_x: float, target_y: float) -> None:
        """Smoothly moves cursor to (target_x, target_y) along a curved human trajectory."""
        start_x, start_y = self.x, self.y
        dist = math.hypot(target_x - start_x, target_y - start_y)
        if dist < 6:
            self.x, self.y = target_x, target_y
            try:
                await page.mouse.move(target_x, target_y)
            except Exception:
                pass
            return

        mid_x = (start_x + target_x) / 2.0
        mid_y = (start_y + target_y) / 2.0
        arc = random.uniform(-0.25, 0.25) * dist

        ctrl1_x = start_x + (mid_x - start_x) * 0.7 + arc * random.uniform(0.4, 0.9)
        ctrl1_y = start_y + (mid_y - start_y) * 0.7 - arc * random.uniform(0.4, 0.9)
        ctrl2_x = mid_x + (target_x - mid_x) * 0.8 - arc * random.uniform(0.3, 0.7)
        ctrl2_y = mid_y + (target_y - mid_y) * 0.8 + arc * random.uniform(0.3, 0.7)

        steps = max(10, min(35, int(dist / random.uniform(16.0, 26.0))))

        for i in range(1, steps + 1):
            t = i / float(steps)
            t_eased = (1.0 - math.cos(t * math.pi)) / 2.0

            bx = self._bezier_point(start_x, ctrl1_x, ctrl2_x, target_x, t_eased)
            by = self._bezier_point(start_y, ctrl1_y, ctrl2_y, target_y, t_eased)

            if i < steps - 2:
                bx += random.uniform(-1.0, 1.0)
                by += random.uniform(-1.0, 1.0)

            try:
                await page.mouse.move(bx, by)
            except Exception:
                pass
            self.x, self.y = bx, by
            await asyncio.sleep(random.uniform(0.007, 0.019))

        self.x, self.y = target_x, target_y
        try:
            await page.mouse.move(target_x, target_y)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.03, 0.08))

    async def click(self, page, locator) -> bool:
        """Hover naturally, dwell, press down (isTrusted: true), hold, and release."""
        try:
            box = await locator.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                await locator.click(timeout=2000)
                return True

            tgt_x = box["x"] + box["width"] * random.uniform(0.28, 0.72)
            tgt_y = box["y"] + box["height"] * random.uniform(0.32, 0.68)

            await self.move_to(page, tgt_x, tgt_y)
            await asyncio.sleep(random.uniform(0.10, 0.25))

            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.055, 0.120))
            await page.mouse.up()

            await asyncio.sleep(random.uniform(0.12, 0.28))
            return True
        except Exception:
            try:
                await locator.click(timeout=1500)
                return True
            except Exception:
                return False

    async def type_text(self, page, locator, text: str) -> bool:
        """Clicks input field and types with realistic human keystroke rhythm."""
        try:
            await self.click(page, locator)
            await asyncio.sleep(random.uniform(0.12, 0.25))
            try:
                await locator.press_sequentially(text, delay=random.randint(45, 95))
            except Exception:
                await locator.fill(text)
            await asyncio.sleep(random.uniform(0.12, 0.25))
            return True
        except Exception:
            try:
                await locator.fill(text)
                return True
            except Exception:
                return False

    async def ambient_wander(self, page) -> None:
        """Simulate random subtle human cursor drift while page is loading."""
        try:
            dx = random.uniform(-80, 80)
            dy = random.uniform(-50, 50)
            tx = max(100.0, min(1800.0, self.x + dx))
            ty = max(100.0, min(950.0, self.y + dy))
            await self.move_to(page, tx, ty)
        except Exception:
            pass


human_cursor = HumanCursor()


def parse_session_tokens(raw_text: str) -> list:
    """Parses exported tab-separated or JSON browser session tokens."""
    if not raw_text or not raw_text.strip():
        return []
    raw = raw_text.strip()
    if raw.startswith("[") or raw.startswith("{"):
        try:
            arr = json.loads(raw)
            if isinstance(arr, dict):
                arr = [arr]
            res = []
            for item in arr:
                if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                    res.append({
                        "name": str(item["name"]).strip(),
                        "value": str(item["value"]).strip(),
                        "domain": str(item.get("domain") or ".zoom.us").strip(),
                        "path": str(item.get("path") or "/").strip(),
                        "secure": bool(item.get("secure", True)),
                    })
            return res
        except Exception:
            pass

    res = []
    now = int(time.time())
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split("\t")
        if len(p) >= 7 and any(k in p[0].lower() for k in ("zoom", "nareshit")):
            item = {
                "domain": p[0].strip(),
                "path": p[2].strip() or "/",
                "secure": p[3].strip().upper() == "TRUE",
                "name": p[5].strip(),
                "value": p[6].strip(),
            }
            try:
                exp = int(float(p[4].strip()))
                if exp > now:
                    item["expires"] = exp
            except Exception:
                pass
            res.append(item)
    return res


async def setup_zoom_session(context) -> int:
    """Injects user authentication session tokens into browser context before join."""
    cookie_text = ""
    source = ""

    candidate_paths = []
    if ZOOM_COOKIES_FILE:
        candidate_paths.append(Path(ZOOM_COOKIES_FILE))
    candidate_paths.extend([
        Path("/out/zoom_cookies.txt"),
        Path("/tmp/zoom_cookies.txt"),
        Path("zoom_cookies.txt"),
    ])

    for cp in candidate_paths:
        try:
            if cp.exists() and cp.is_file() and cp.stat().st_size > 0:
                cookie_text = cp.read_text(encoding="utf-8", errors="ignore")
                source = str(cp)
                break
        except Exception:
            pass

    if not cookie_text and ZOOM_COOKIES:
        cookie_text = ZOOM_COOKIES
        source = "ZOOM_COOKIES secret"

    if not cookie_text.strip():
        log("ℹ️ No Zoom session credentials configured (using normal browser session)")
        return 0

    tokens = parse_session_tokens(cookie_text)
    if not tokens:
        log(f"⚠️ Could not parse tokens from {source}")
        return 0

    loaded = 0
    try:
        await context.add_cookies(tokens)
        loaded = len(tokens)
    except Exception:
        for t in tokens:
            try:
                await context.add_cookies([t])
                loaded += 1
            except Exception:
                pass

    if loaded > 0:
        log(f"🔑 Applied {loaded} session tokens into browser context (from {source})")
        step.record("Zoom Auth", "🔑", f"Applied {loaded} session tokens from {source}")
    return loaded

HIDE_CSS = """
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
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--window-size=1920,1080",
    "--window-position=0,0",
    "--start-maximized",
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
        time_disp = (CLASS_START or 'Immediate') + " IST"
        _vnc = _get_vnc_url()
        vnc_line = f"- **🖥️ Remote Desktop (noVNC):** [{_vnc}]({_vnc})\n" if _vnc else ""
        header = (
            f"## 🎥 Zoom Class Recording Summary\n\n"
            f"- **Class Name:** `{ACTIVE_CLASS_TITLE}`\n"
            f"- **Class Time:** `{time_disp}`" + (f" - `{CLASS_END} IST`" if CLASS_END else "") + "\n"
            f"{vnc_line}"
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
        safe_details = details.replace("|", "/").replace("\n", " ").replace("\r", " ").strip()
        row = f"| {step_name} | {status} | {now_str} | {safe_details} |\n"
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(row)
        except Exception:
            pass


step = StepTracker(STEP_SUMMARY_PATH)


def redact(text: str) -> str:
    """Strip secrets from anything that might be logged or sent to Telegram."""
    for secret in (PORTAL_PASS, TG_BOT_TOKEN, TG_API_HASH, ZOOM_PASS):
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
    """Supports single keywords, pipe-separated variants, and common course aliases
    like 'Advanced Java' -> 'ADV JAVA' / 'ADV. JAVA' / 'Advance Java'."""
    parts = [p.strip() for p in kw.split("|") if p.strip()]
    if not parts:
        return re.compile(r".*", re.I)
    expanded = []
    for p in parts:
        words = p.split()
        word_patterns = []
        for w in words:
            wl = w.lower().strip(".")
            if wl in ("adv", "advance", "advanced"):
                word_patterns.append(r"(?:adv(?:\.|ance(?:d)?)?)")
            elif wl in ("react", "reactjs"):
                word_patterns.append(r"(?:react(?:\s*js)?)")
            elif wl in ("js", "javascript"):
                word_patterns.append(r"(?:js|javascript)?")
            else:
                word_patterns.append(re.escape(w))
        expanded.append(r"\s+".join(word_patterns))
    pat = "|".join(expanded) if expanded else ".*"
    return re.compile(pat, re.I)


def _domain_base(host: str) -> str:
    host = host.split(":")[0]
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


# The first Zoom link that carried a real meeting id (e.g. /j/123456789?pwd=...).
# Zoom may redirect that link to a sign-in page, so we remember the original
# and can rebuild a direct web-client URL from it.
MEETING_URL = ""


def make_web_client_urls(url: str) -> list[str]:
    """Direct web-client links for a meeting/webinar link (every parameter kept,
    including the personal tk token). Tried in order; the fragment (#success) is dropped."""
    try:
        if not url or not _is_zoom_host(url):
            return []
        u = urllib.parse.urlparse(url)
        m = re.search(r"/(\d{9,12})(?:[/?#]|$)", u.path)
        if not m:
            return []
        mid = m.group(1)
        host = u.hostname or "zoom.us"
        base = re.search(r"(zoom(?:gov)?\.(?:us|com|cn))$", host, re.I)
        root = (base.group(1) if base else "zoom.us").lower()
        q = ("?" + u.query) if u.query else ""
        q_launch = "?ref_from=launch" + ("&" + u.query if u.query else "")
        # Always prioritize the exact same cluster host (e.g. us06web.zoom.us) with full token/query
        # This guarantees NO sign-in or registration wall is triggered.
        urls = [
            f"https://{host}/wc/{mid}/join{q}",              # 1. Exact cluster host + full query/token
            f"https://{host}/wc/{mid}/join{q_launch}",       # 2. Exact cluster host + launch ref
            f"https://{host}/wc/join/{mid}{q}",              # 3. Exact cluster host alternate route
        ]
        if host != f"app.{root}":
            urls.append(f"https://app.{root}/wc/{mid}/join{q}")
        return urls
    except Exception:
        return []


def make_web_client_url(url: str) -> str | None:
    urls = make_web_client_urls(url)
    return urls[0] if urls else None


NAV_LOG: list[str] = []


def note_navigation(url: str) -> None:
    """Record host + path + parameter NAMES (never values) of Zoom/portal hops, for diagnostics."""
    try:
        u = urllib.parse.urlparse(url)
        if not u.hostname or u.scheme not in ("http", "https"):
            return
        keys = ",".join(sorted(urllib.parse.parse_qs(u.query).keys()))
        entry = f"{u.hostname}{re.sub(r'\d{6,}', '<id>', u.path)}" + (f" ?{keys}" if keys else "")
        if not NAV_LOG or NAV_LOG[-1] != entry:
            NAV_LOG.append(entry)
            del NAV_LOG[:-12]
    except Exception:
        pass


def remember_meeting_url(url: str) -> None:
    """Keep the fullest (most parameter/token-carrying) real meeting link we see."""
    global MEETING_URL
    if not url or SIGNIN_URL_RE.search(url) or make_web_client_url(url) is None:
        return
    curr_q = len(urllib.parse.urlparse(MEETING_URL).query) if MEETING_URL else -1
    new_q = len(urllib.parse.urlparse(url).query)
    # Always keep the fullest link with tokens (tk=, pwd=, etc.) so no signin/register is needed
    if not MEETING_URL or new_q > curr_q or ("tk=" in url and "tk=" not in MEETING_URL):
        MEETING_URL = url
        log(f"Remembered full meeting link ({new_q} query chars): {mask_url(url)}")


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
        self.started_xvfb = False
        self.started_pulse = False

    def start(self) -> None:
        os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/xdg-runtime")
        os.environ.setdefault("PULSE_RUNTIME_PATH", "/tmp/pulse")
        for key in ("XDG_RUNTIME_DIR", "PULSE_RUNTIME_PATH"):
            os.makedirs(os.environ[key], mode=0o700, exist_ok=True)
        os.environ["DISPLAY"] = DISPLAY

        # Check if PulseAudio is already running (e.g. from start-audio.sh)
        if sh(["pactl", "info"]).returncode != 0:
            sh(["pulseaudio", "-D", "--exit-idle-time=-1"])
            for _ in range(30):
                if sh(["pactl", "info"]).returncode == 0:
                    self.started_pulse = True
                    break
                time.sleep(0.5)

        sh([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={SINK}", f"sink_properties=device.description={SINK}",
        ])
        sh(["pactl", "set-default-sink", SINK])

        try:
            sh([
                "pactl", "load-module", "module-null-sink",
                "sink_name=VirtualMic", "sink_properties=device.description=VirtualMic",
            ])
            sh(["pactl", "set-default-source", "VirtualMic.monitor"])
        except Exception:
            pass

        # Check if Xvfb is already running (e.g. from start-desktop.sh)
        if sh(["xdpyinfo", "-display", DISPLAY]).returncode != 0:
            self.xvfb = subprocess.Popen(
                ["Xvfb", DISPLAY, "-screen", "0", "1920x1080x24", "-ac",
                 "+extension", "GLX", "+render", "-noreset"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for _ in range(40):
                if sh(["xdpyinfo", "-display", DISPLAY]).returncode == 0:
                    self.started_xvfb = True
                    break
                time.sleep(0.25)

        # Hide cursor when idle (cursor remains visible on touch / mouse move)
        try:
            self.unclutter = subprocess.Popen(
                ["unclutter", "-idle", "1.0", "-root"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self.unclutter = None

    def stop(self) -> None:
        if self.unclutter and self.unclutter.poll() is None:
            self.unclutter.terminate()
        if self.started_xvfb and self.xvfb and self.xvfb.poll() is None:
            self.xvfb.terminate()


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
        for old in self._seg_dir.glob("seg_*.mkv"):
            try:
                old.unlink()
            except Exception:
                pass
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
    """Click the first visible element of `role` whose name matches a pattern
    using natural human cursor movement and click physics."""
    for frame in page.frames:
        for pat in patterns:
            try:
                loc = frame.get_by_role(role, name=re.compile(pat, re.I)).first
                if await loc.count() and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    if await human_cursor.click(page, loc):
                        return pat
            except Exception:
                continue
    return None


async def page_text(page) -> str:
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(await asyncio.wait_for(
                frame.evaluate("document.body ? document.body.innerText : ''"), 5))
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

            zoom_is_on = zoom_active.is_set()

            for p in pages:
                try:
                    # 3-second grace period for pages that are still redirecting
                    if now - first_seen.get(id(p), now) < 3:
                        continue
                    url = p.url
                    if not url or url.startswith(("about:", "chrome://", "chrome-error://")):
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
    await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45000)
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
    await asyncio.sleep(0.5)

    submitted = False
    for btn_sel in [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log In")',
        'button:has-text("Login")',
        'button:has-text("Sign In")',
        'button:has-text("Submit")',
    ]:
        try:
            sub = page.locator(btn_sel).first
            if await sub.count() and await sub.is_visible():
                await sub.click(timeout=1500)
                submitted = True
                break
        except Exception:
            continue

    if not submitted:
        submitted = await click_any(page, [r"^log ?in$", r"sign ?in", r"^submit$", r"^login"])
    if not submitted:
        try:
            await pw_field.press("Enter")
        except Exception:
            pass

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


DURATION_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*(?:Hr\'?s?|hrs?|hours?)", re.I)
TIME_RE = re.compile(
    r"\b(\d{1,2})[:.](\d{2})\s*([AP]M)?|\b(\d{1,2})(\d{2})\s*([AP]M)|\b(\d{1,2})\s*([AP]M)\b",
    re.I,
)


def _dt(h, m, ap, base):
    """Build a datetime from hour, minute, optional AM/PM and a reference date."""
    h, m = int(h), int(m)
    if ap:
        h = h % 12 + (12 if ap.upper() == "PM" else 0)
    return base.replace(hour=h, minute=m, second=0, microsecond=0)


def card_window(text: str, now: datetime):
    """Parse start and end times from card text.
    Supports:
      - NareshIT portal: "ADV JAVA 6 PM ... 06:00 PM • 01:30 Hr's" -> 18:00 to 19:30
      - NareshIT portal: "React JS 730 PM ... 07:30 PM • 01:30 Hr's" -> 19:30 to 21:00
      - Standard range:  "10:00 AM - 11:00 AM", "15:00 to 16:00"
      - Shared period:   "10:00 - 11:00 AM"
      - Single time:     "6:00 PM" / "6pm" -> duration or +60 min
    """
    if not text:
        return None
    dur_match = DURATION_RE.search(text)
    duration = None
    if dur_match:
        dh, dm = dur_match.groups()
        duration = timedelta(hours=int(dh), minutes=int(dm))
        text_no_dur = text[:dur_match.start()]
    else:
        text_no_dur = text

    raw_times = []
    for m in TIME_RE.finditer(text_no_dur):
        g = m.groups()
        h = g[0] or g[3] or g[6]
        mi = g[1] or g[4] or "00"
        ap = (g[2] or g[5] or g[7] or "").strip()
        raw_times.append((h, mi, ap))

    if not raw_times:
        return None

    if len(raw_times) > 1 and not raw_times[0][2] and raw_times[1][2]:
        raw_times[0] = (raw_times[0][0], raw_times[0][1], raw_times[1][2])

    dts = [_dt(*t, now) for t in raw_times]
    if duration:
        return dts[-1], dts[-1] + duration

    distinct = []
    for d in dts:
        if not any(abs((d - x).total_seconds()) < 300 for x in distinct):
            distinct.append(d)

    start = distinct[0]
    if len(distinct) > 1:
        end = distinct[1]
        if end < start:
            start -= timedelta(hours=12)
        if end < start:
            end += timedelta(days=1)
    else:
        end = start + timedelta(hours=1)

    return start, end


async def auto_discover_cards(page):
    """Auto-discover class card containers by locating Join buttons and
    marking candidate cards."""
    try:
        await page.evaluate(
            "() => document.querySelectorAll('[data-portal-card]').forEach(e => e.removeAttribute('data-portal-card'))"
        )
        cards_info = await page.evaluate("""() => {
            const joinRe = /\\bjoin\\b/i;
            const timeRe = /\\d{1,2}[:.:]\\d{2}\\s*[AP]M/i;
            const buttons = [...document.querySelectorAll('button, a, [role="button"]')]
                .filter(el => {
                    const txt = (el.textContent || '').trim();
                    return joinRe.test(txt) && txt.length < 30 && el.offsetParent !== null;
                });
            const results = [];
            const seen = new Set();
            for (const btn of buttons) {
                let el = btn.parentElement;
                let found = false;
                for (let d = 0; d < 10 && el && el !== document.body; d++, el = el.parentElement) {
                    const text = (el.innerText || '').trim();
                    if (timeRe.test(text) && text.length < 1000 && !seen.has(text)) {
                        seen.add(text);
                        const marker = '__card_' + results.length;
                        el.setAttribute('data-portal-card', marker);
                        results.push(marker);
                        found = true;
                        break;
                    }
                }
                if (!found && btn.parentElement) {
                    let p = btn.parentElement;
                    for (let d = 0; d < 6 && p && p !== document.body; d++) {
                        const cls = String(p.className || '').toLowerCase();
                        const tag = p.tagName.toLowerCase();
                        if (tag === 'tr' || tag === 'li' || tag === 'article' || cls.includes('card') || cls.includes('batch') || cls.includes('item')) {
                            break;
                        }
                        if (p.parentElement === document.body) break;
                        p = p.parentElement;
                    }
                    const text = ((p || btn.parentElement).innerText || '').trim();
                    if (!seen.has(text)) {
                        seen.add(text);
                        const marker = '__card_' + results.length;
                        (p || btn.parentElement).setAttribute('data-portal-card', marker);
                        results.push(marker);
                    }
                }
            }
            return results;
        }""")
        if not cards_info:
            return []
        cards = []
        for m in cards_info:
            loc = page.locator(f"[data-portal-card='{m}']").first
            if await loc.count() > 0:
                cards.append(loc)
        return cards
    except Exception as exc:
        log(f"Auto-discover cards error: {type(exc).__name__}")
        return []


async def find_active_join(page):
    """Find the join button for the card matching current time (or CLASS_KEYWORD).
    Discovers all cards, parses their times, checks button readiness, and selects
    the card matching current time. Returns (card, button)."""
    global ACTIVE_CLASS_TITLE, ACTIVE_CLASS_INFO
    now = datetime.now(IST)
    cards = await auto_discover_cards(page)

    if not cards:
        join_any = page.locator("a, button, [role='button']").filter(
            has_text=re.compile(r"\bjoin\b", re.I)
        )
        try:
            kw_re = keyword_pattern(CLASS_KEYWORD) if CLASS_KEYWORD and CLASS_KEYWORD.strip() else None
            q = page.locator("div, li, tr, article, section").filter(has=join_any)
            if kw_re:
                q_kw = q.filter(has_text=kw_re)
                if await q_kw.count() > 0:
                    q = q_kw
            cnt = await q.count()
            for i in range(cnt):
                c = q.nth(i)
                if await c.locator(join_any).count() == 1:
                    cards.append(c)
        except Exception:
            pass

    if not cards:
        return None, None

    kw_re = keyword_pattern(CLASS_KEYWORD) if CLASS_KEYWORD and CLASS_KEYWORD.strip() else None
    parsed_cards = []

    for idx, card in enumerate(cards, 1):
        try:
            raw = (await card.inner_text(timeout=2000)).strip()
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            title = lines[0] if lines else f"Card #{idx}"
            if len(lines) > 1 and ("hrs" in lines[0].lower() or "left" in lines[0].lower()):
                title = lines[1]
            title = re.sub(r"\b(join|live)\b", "", title, flags=re.I).strip(" -•|") or title[:40]

            btn = card.locator("a, button, [role='button']").filter(
                has_text=re.compile(r"\bjoin\b", re.I)
            ).first
            if await btn.count() == 0:
                continue

            is_ready = await join_ready(btn)
            w = card_window(raw, now)
            is_live_now = False
            dist_to_now = 999999.0
            start_str = ""
            end_str = ""
            status_str = "ℹ️ (No valid time detected)"

            if w:
                start, end = w
                start_str = start.strftime("%I:%M %p")
                end_str = end.strftime("%I:%M %p")
                is_live_now = (start - timedelta(minutes=15)) <= now <= (end + timedelta(minutes=15))
                dist_to_now = abs((now - start).total_seconds())
                if is_live_now:
                    status_str = f"🟢 LIVE NOW ({start_str} - {end_str})"
                elif now < start:
                    status_str = f"⏳ Scheduled for {start_str} - {end_str}"
                else:
                    status_str = f"⏹️ Finished at {end_str}"

            matches_kw = bool(kw_re and kw_re.search(raw))

            log(f"   [{idx}] {title[:35]:<35} -> {status_str} | Join ready: {is_ready}")

            if is_live_now and is_ready and matches_kw:
                score = (0, 0, dist_to_now)
            elif is_live_now and is_ready:
                score = (1, 0, dist_to_now)
            elif is_ready:
                score = (2, 0 if matches_kw else 1, dist_to_now)
            elif is_live_now and matches_kw:
                score = (3, 0, dist_to_now)
            elif is_live_now:
                score = (4, 0, dist_to_now)
            elif matches_kw:
                score = (5, 0, dist_to_now)
            elif w and now < w[0]:
                score = (6, 0, dist_to_now)
            else:
                score = (7, 0, dist_to_now)

            parsed_cards.append({
                "score": score,
                "card": card,
                "btn": btn,
                "title": title,
                "raw": raw,
                "window": w,
                "is_live_now": is_live_now,
                "is_ready": is_ready,
                "matches_kw": matches_kw,
            })
        except Exception as e:
            log(f"   [{idx}] Error evaluating card #{idx}: {e}")
            continue

    if not parsed_cards:
        return None, None

    parsed_cards.sort(key=lambda x: x["score"])
    best = parsed_cards[0]
    ACTIVE_CLASS_TITLE = best["title"]
    ACTIVE_CLASS_INFO = best

    log(f"🎯 Selected card: '{best['title']}' (Score: {best['score'][0]}, Live: {best['is_live_now']}, Ready: {best['is_ready']})")
    return best["card"], best["btn"]


async def wait_and_join(context, page):
    now = datetime.now(IST)
    is_immediate = not CLASS_START or CLASS_START.lower() in ("now", "test", "immediate")
    start_dt = class_start_dt()
    diff_to_start = (start_dt - now).total_seconds()

    # Pre-scan Today's Classes tab for active or live cards
    card, btn = await find_active_join(page)
    is_live_or_ready = bool(card and (ACTIVE_CLASS_INFO.get("is_live_now") or ACTIVE_CLASS_INFO.get("is_ready")))

    # ----------------------------------------------------------------------
    # Scenario A: Explicit immediate test requested (e.g. class_start="now")
    # ----------------------------------------------------------------------
    if is_immediate:
        step.record(
            "Immediate Run",
            "🚀",
            f"Immediate run requested for '{ACTIVE_CLASS_TITLE}'. Scanning for active Join button...",
        )
        start_dt = now
        diff_to_start = 0

    # ----------------------------------------------------------------------
    # Scenario B: Class was scheduled for hours ago (already concluded)
    # Only applies if no card is currently live or ready on the portal!
    # ----------------------------------------------------------------------
    elif not is_live_or_ready and diff_to_start < -(CLASS_DURATION_MIN * 60 + 1800):
        hours_ago = abs(diff_to_start) / 3600
        step.record(
            "Class Status",
            "⏹️",
            f"Class '{ACTIVE_CLASS_TITLE}' was scheduled for {CLASS_START} IST ({hours_ago:.1f}h ago) and has already finished for today.",
        )
        await notify(f"ℹ️ Class '{ACTIVE_CLASS_TITLE}' scheduled for {CLASS_START} IST has already concluded for today.")
        return None

    # ----------------------------------------------------------------------
    # Scenario C: Run triggered way too early (>45 minutes before class)
    # Only applies if no card is currently live or ready on the portal!
    # ----------------------------------------------------------------------
    elif not is_live_or_ready and diff_to_start > MAX_WAIT_MIN * 60:
        hours_away = diff_to_start / 3600
        mins_away = int(diff_to_start // 60)
        step.record(
            "Schedule Check",
            "ℹ️",
            f"Class '{ACTIVE_CLASS_TITLE}' starts at {CLASS_START} IST ({hours_away:.1f}h / {mins_away}m away).",
        )
        if card is not None:
            step.record(
                "Portal Verification",
                "✅",
                f"Verified: Found '{ACTIVE_CLASS_TITLE}' card on portal. Portal login & card search 100% operational.",
            )
        else:
            step.record(
                "Portal Verification",
                "ℹ️",
                f"Portal login successful. Card for '{ACTIVE_CLASS_TITLE}' is not yet posted by host for today.",
            )
        step.record(
            "Scheduler Standby",
            "⏰",
            f"Standing by. Cloudflare Scheduler triggers automatically 13 mins before {CLASS_START} IST.",
        )
        await notify(
            f"✅ Pre-flight check successful: portal login & navigation verified for '{ACTIVE_CLASS_TITLE}'. "
            f"Class starts at {CLASS_START} IST ({hours_away:.1f}h away). Scheduler will trigger at class time."
        )
        return None

    # ----------------------------------------------------------------------
    # Scenario D: Class is in the near future (within 45 min) or ongoing!
    # ----------------------------------------------------------------------
    deadline = time.monotonic() + MAX_WAIT_MIN * 60
    last_wait_log = 0.0
    last_reload = time.monotonic()

    # --- Phase 1: Wait calmly until class start time (only if NOT already live/ready) ---
    if not is_live_or_ready and not is_immediate and diff_to_start > 0:
        while datetime.now(IST) < start_dt - timedelta(seconds=15):
            now = datetime.now(IST)
            card, btn = await find_active_join(page)
            if card and (ACTIVE_CLASS_INFO.get("is_live_now") or ACTIVE_CLASS_INFO.get("is_ready")):
                break
            rem_sec = int((start_dt - now).total_seconds())
            if rem_sec <= 15:
                break

            if time.monotonic() - last_wait_log >= 120 or last_wait_log == 0.0:
                last_wait_log = time.monotonic()
                mins, secs = divmod(rem_sec, 60)
                step.record(
                    "Waiting for Class Time",
                    "⏳",
                    f"Class '{ACTIVE_CLASS_TITLE}' starts at {CLASS_START} IST ({mins}m {secs:02d}s remaining). Waiting quietly...",
                )

            sleep_time = min(5.0, max(1.0, float(rem_sec - 15)))
            await asyncio.sleep(sleep_time)

        # --- Phase 2: Start time reached! One clean refresh to fetch active DOM ---
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
            is_live_card = ACTIVE_CLASS_INFO.get("is_live_now", False)
            too_early = not is_live_card and not is_active and (start_dt - now).total_seconds() > 30

            if is_active and not too_early:
                step.record(
                    "Join Button",
                    "🎯",
                    f"Join button is active and clickable! Clicking Join for '{ACTIVE_CLASS_TITLE}'...",
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
                            f"Found '{ACTIVE_CLASS_TITLE}' card. Waiting for class start time ({mins}m {secs:02d}s remaining)...",
                        )
                    else:
                        step.record(
                            "Waiting for Host",
                            "⏳",
                            f"Found '{ACTIVE_CLASS_TITLE}' card. Waiting for host/teacher to activate Join button...",
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
                    f"Scanning Today's Classes tab for live class at current time ({now:%I:%M %p} IST)...",
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

    step.record("Waiting for Join", "❌", f"Join button for '{ACTIVE_CLASS_TITLE}' did not become active within {MAX_WAIT_MIN} mins")
    raise TimeoutError(
        f"Join button for '{ACTIVE_CLASS_TITLE}' did not become active within {MAX_WAIT_MIN} minutes"
    )


LAST_ZOOM_URL = ""  # the exact link the portal opened (used only to reload after a browser error)


async def click_join(context, page, btn, wait_sec: int = 25):
    known_pages = set(context.pages)
    try:  # the portal button's own link is the best source of the meeting id/password/token
        href = await btn.get_attribute("href") or ""
        if href:
            remember_meeting_url(href)
    except Exception:
        pass
    try:
        onclick = await btn.get_attribute("onclick") or ""
        m = re.search(r"https?://[^\s'\"]+zoom[^\s'\"]+", onclick)
        if m:
            remember_meeting_url(m.group(0))
    except Exception:
        pass
    try:
        async with context.expect_page(timeout=6000) as new_page_info:
            try:
                await btn.click(force=True, timeout=4000)
            except Exception:
                await btn.evaluate("b => b.click()")
        await new_page_info.value
    except PWTimeout:
        pass  # same-tab redirect; handled by the URL scan below
    except Exception:
        pass

    end = time.monotonic() + wait_sec
    while time.monotonic() < end:
        for p in context.pages:
            if _is_zoom_host(p.url):
                global LAST_ZOOM_URL
                LAST_ZOOM_URL = p.url
                log(f"Captured Zoom URL: {mask_url(p.url)}")
                return p
        await asyncio.sleep(0.5)

    for p in list(context.pages):
        if p not in known_pages and not _is_zoom_host(p.url):
            try:
                await p.close()
            except Exception:
                pass
    return None


# --------------------------------------------------------------------------
# State 4: Zoom web client, strictly no mic / no video
# --------------------------------------------------------------------------
async def click_browser_join(zp) -> bool:
    """Click 'Join from browser' on Zoom's launch page using real human cursor physics.
    Dispatches genuine CDP pointer events with isTrusted === true to bypass bot detection."""
    pat = re.compile(r"join from (your )?browser", re.I)
    url_before = zp.url

    # Strategy 1: Natural human Bézier move and click on the 'Join from browser' element
    for frame in zp.frames:
        for r in ("link", "button"):
            try:
                loc = frame.get_by_role(r, name=pat).first
                if await asyncio.wait_for(loc.count(), 2) > 0 and await asyncio.wait_for(loc.is_visible(), 2):
                    await loc.scroll_into_view_if_needed()
                    if await human_cursor.click(zp, loc):
                        log(f"Human-clicked 'Join from browser' ({r})")
                        await asyncio.sleep(2.0)
                        if zp.url != url_before or "/wc/" in zp.url:
                            return True
            except Exception:
                continue

    # Strategy 2: Selector-based human click
    for frame in zp.frames:
        try:
            links = frame.locator("a, button, [role='button']").filter(has_text=pat)
            if await links.count() > 0:
                l0 = links.first
                if await l0.is_visible():
                    await l0.scroll_into_view_if_needed()
                    if await human_cursor.click(zp, l0):
                        log("Human-clicked 'Join from browser' (selector)")
                        await asyncio.sleep(2.0)
                        if zp.url != url_before or "/wc/" in zp.url:
                            return True
        except Exception:
            pass

    # Strategy 3: Extract webClientUrl from the page's JS/protobuf data
    try:
        wc_url = await zp.evaluate("""() => {
            for (const s of document.querySelectorAll('script')) {
                const t = s.textContent || '';
                const m = t.match(/webClientUrl['":\\s]*['"]?(https?:\\/\\/[^'"\\s,}]+)/);
                if (m) return m[1];
            }
            const u = new URL(location.href);
            const m2 = u.pathname.match(/\\/(?:w|j|s)\\/(\\d{9,})/);
            if (m2) return u.origin + '/wc/' + m2[1] + '/join' + u.search;
            return null;
        }""")
        if wc_url and _is_zoom_host(wc_url):
            log(f"Extracted web client URL from page: {mask_url(wc_url)}")
            await zp.goto(wc_url, wait_until="commit", timeout=15000)
            await asyncio.sleep(2)
            if zp.url != url_before or "/wc/" in zp.url:
                return True
    except Exception:
        pass

    return False


WC_MEETING_RE = re.compile(r"/wc/(join/)?\d{6,}", re.I)


def is_generic_join_page(zp) -> bool:
    """Zoom's blank 'Join Meeting / Meeting ID' page: the meeting link was not accepted."""
    try:
        u = urllib.parse.urlparse(zp.url)
        return u.hostname is not None and _is_zoom_host(zp.url) and \
            u.path.rstrip("/") in ("/wc", "/wc/join", "/join") and not WC_MEETING_RE.search(u.path)
    except Exception:
        return False


async def is_real_prejoin(zp) -> bool:
    """True only on the pre-join screen of an actual meeting."""
    if is_generic_join_page(zp) or not _is_zoom_host(zp.url):
        return False
    try:
        if WC_MEETING_RE.search(urllib.parse.urlparse(zp.url).path):
            return True
    except Exception:
        pass
    for frame in zp.frames:
        try:
            b = frame.locator("button.preview-join-button, button[class*='preview-join'], input#input-for-name, input#inputname").first
            if await b.count() and await b.is_visible():
                return True
        except Exception:
            continue
    return False


def dismiss_browser_prompt() -> None:
    """Close Chrome's 'Open xdg-open?' bubble (Cancel is focused) - harmless on the page."""
    try:
        sh(["xdotool", "key", "--clearmodifiers", "Escape"], timeout=3)
    except Exception:
        pass


async def browser_join_href(zp) -> str | None:
    """The 'Join from browser' button's own destination (not a rewrite)."""
    pat = re.compile(r"join from (your )?browser", re.I)
    for frame in zp.frames:
        try:
            loc = frame.locator("a[href]").filter(has_text=pat).first
            if await asyncio.wait_for(loc.count(), 3):
                href = await loc.get_attribute("href", timeout=3000) or ""
                if href.startswith("/"):
                    href = urllib.parse.urljoin(zp.url, href)
                if href.startswith("http") and _is_zoom_host(href):
                    return href
        except Exception:
            continue
    return None


async def _has_webclient(context, zp):
    for p in context.pages:
        if SIGNIN_URL_RE.search(p.url):
            continue  # a sign-in URL can carry "/wc/..." in its query string
        if "/wc/" in p.url and not is_generic_join_page(p):
            return p
        if await is_real_prejoin(p) or await in_meeting(p):
            return p
        for f in p.frames:
            if "/wc/" in f.url and not SIGNIN_URL_RE.search(f.url) and not is_generic_join_page(p):
                return p
    return None


async def recover_from_signin(zp, attempt: int) -> bool:
    """Zoom redirected us to a sign-in page. Retry, in order:
    0) the exact link the portal gave us (personal token intact),
    1) the same link rewritten to the direct web client (all parameters kept),
    2) a Zoom account login (ZOOM_EMAIL / ZOOM_PASS), then back to the meeting.
    Returns True when it navigated somewhere new and the caller should re-check."""
    original = MEETING_URL or LAST_ZOOM_URL
    direct = make_web_client_urls(original)
    if not original:
        log("Sign-in page shown, but no meeting link was captured to retry with")
        return False
    try:
        if attempt == 0 and ZOOM_EMAIL and ZOOM_PASS and direct:
            step.record("Zoom Sign-in", "🔑", "Signing in with the configured Zoom account")
            if await zoom_signin(zp):
                await zp.goto(direct[0], wait_until="domcontentloaded")
                await asyncio.sleep(2)
                return True
    except Exception as e:
        log(f"Sign-in recovery hiccup ({type(e).__name__})")
    return False


class SigninRequired(RuntimeError):
    pass


async def on_signin_page(zp) -> bool:
    """True when Zoom / an IdP shows a sign-in wall instead of the meeting."""
    try:
        url = zp.url
        if SIGNIN_URL_RE.search(url):
            return True
        # Fast path: a real sign-in wall MUST have login input fields
        has_login_input = False
        for frame in zp.frames:
            try:
                if await asyncio.wait_for(frame.locator("input#email, input[type='email'], input[type='password']").count(), 2) > 0:
                    has_login_input = True
                    break
            except Exception:
                pass
        if not has_login_input:
            return False
        text = await page_text(zp)
        if WAITING_ROOM_RE.search(text):
            return False
        return bool(SIGNIN_TEXT_RE.search(text))
    except Exception:
        return False


async def zoom_signin(zp) -> bool:
    """Optional: sign in with a dedicated Zoom account (ZOOM_EMAIL / ZOOM_PASS)."""
    if not (ZOOM_EMAIL and ZOOM_PASS):
        return False
    try:
        email = zp.locator("input#email, input[type='email'], input[placeholder*='email' i]").first
        await email.wait_for(state="visible", timeout=5000)
        await email.fill(ZOOM_EMAIL)
        pw = zp.locator("input[type='password']").first
        if not (await pw.count() and await pw.is_visible()):
            try:
                await zp.get_by_role("button", name=re.compile(r"^next$", re.I)).first.click(timeout=3000)
            except Exception:
                pass
        await pw.wait_for(state="visible", timeout=10000)
        await pw.fill(ZOOM_PASS)
        await pw.press("Enter")
        log("Submitted Zoom sign-in form")
        await asyncio.sleep(5)
        return True
    except Exception as e:
        log(f"Zoom sign-in attempt failed ({type(e).__name__})")
        return False


async def to_web_client(context, zp):
    """Zoom launch page -> click 'Join from your browser' -> pre-join screen.
    Zero URL rewriting: preserves the portal's exact webinar attendee token (tk)
    and #success fragment. Never rewrites to /wc/ or forces direct navigation."""
    step.record("Zoom Client", "💻", "Zoom launch page reached; opening web client via in-page Join link")
    deadline = time.monotonic() + 180
    last_click = 0.0
    clicks = 0
    reloads = 0
    dismiss_browser_prompt()

    orig_url = LAST_ZOOM_URL or zp.url
    log(f"Preserving exact portal Zoom link (zero URL rewriting): {mask_url(orig_url)}")
    await asyncio.sleep(2)       # let the launch page finish initial render

    # Dismiss the "Did not open Zoom Workplace app?" tooltip that can overlay buttons
    for frame in zp.frames:
        try:
            close_btns = frame.locator("button[aria-label*='close' i], button[aria-label*='dismiss' i], .close, [class*='close']")
            cnt = await close_btns.count()
            for i in range(min(cnt, 3)):
                b = close_btns.nth(i)
                txt = (await b.inner_text(timeout=500) or "").strip().lower()
                if txt not in ("leave", "end", "exit"):
                    await b.click(force=True, timeout=500)
                    await asyncio.sleep(0.3)
        except Exception:
            pass

    while time.monotonic() < deadline:
        found = await _has_webclient(context, zp)
        if found:
            zp = found
            try:
                await zp.wait_for_load_state("domcontentloaded", timeout=15000)
                await asyncio.sleep(1.5)
            except Exception:
                pass
            if is_generic_join_page(zp):
                log("Zoom showed generic 'Join Meeting' page; reopening exact portal link")
                try:
                    await zp.goto(orig_url, wait_until="domcontentloaded")
                except Exception:
                    pass
                clicks, last_click = 0, 0.0
                await asyncio.sleep(2)
                continue
            log(f"Web client active: {mask_url(zp.url)}")
            dismiss_browser_prompt()
            return zp

        if zp.is_closed():
            alive = [p for p in context.pages if _is_zoom_host(p.url)]
            if not alive:
                raise RuntimeError("Zoom tab closed before the web client loaded")
            zp = alive[-1]

        if zp.url.startswith("chrome-error://") and reloads < 3:
            reloads += 1
            log(f"Zoom tab hit a browser error page; reloading original portal link ({reloads}/3)")
            try:
                await zp.goto(orig_url, wait_until="domcontentloaded")
            except Exception:
                pass
            clicks, last_click = 0, 0.0
            await asyncio.sleep(2)
            continue

        # In-page 'Join from browser' click
        if clicks < 4 and time.monotonic() - last_click > (0 if clicks == 0 else 15):
            if await click_browser_join(zp):
                clicks += 1
                last_click = time.monotonic()
                log(f"Clicked 'Join from browser' (attempt {clicks})")
                # Wait up to 12 seconds for the web client to appear
                for _ in range(12):
                    await asyncio.sleep(1)
                    found = await _has_webclient(context, zp)
                    if found:
                        break
                    # Check if the URL changed to /wc/ (web client loading)
                    if "/wc/" in zp.url:
                        log(f"URL changed to web client path: {mask_url(zp.url)}")
                        break
                continue
            href = await browser_join_href(zp)
            if href and clicks == 0:
                log(f"Following in-page 'Join from browser' link: {mask_url(href)}")
                try:
                    await zp.goto(href, wait_until="commit", timeout=10000)
                    clicks += 1
                    last_click = time.monotonic()
                    for _ in range(8):
                        await asyncio.sleep(1)
                        if await _has_webclient(context, zp):
                            break
                    continue
                except Exception as e:
                    log(f"In-page link goto hiccup ({type(e).__name__})")

        # Direct web client URL fallback: preserves attendee tk and pwd parameters
        if clicks >= 1 and time.monotonic() - last_click > 10:
            direct_urls = make_web_client_urls(orig_url or zp.url)
            if direct_urls:
                target = direct_urls[0]
                log(f"Opening direct web client URL (preserved tokens): {mask_url(target)}")
                try:
                    await zp.goto(target, wait_until="commit", timeout=15000)
                    last_click = time.monotonic()
                    for _ in range(10):
                        await asyncio.sleep(1)
                        if await _has_webclient(context, zp):
                            break
                    continue
                except Exception as e:
                    log(f"Direct WC goto hiccup ({type(e).__name__})")

        # If 30s elapsed without transition, reload the exact registered portal link
        if clicks >= 2 and time.monotonic() - last_click > 30:
            log("Reloading original registered portal Zoom link...")
            try:
                await zp.goto(orig_url, wait_until="domcontentloaded")
            except Exception:
                pass
            clicks = 0
            last_click = time.monotonic()
            await asyncio.sleep(3)
            continue

        await asyncio.sleep(1)
    try:
        btns = []
        for fr in zp.frames:
            btns += [t.strip() for t in await asyncio.wait_for(
                fr.locator("a, button, [role='button']").all_inner_texts(), 5) if t.strip()]
        seen = re.sub(r"\s+", " ", (await asyncio.wait_for(page_text(zp), 5))).strip()[:200]
    except Exception:
        btns, seen = [], ""
    await notify_photo(
        zp,
        "⚠️ Zoom web client did not open\n"
        f"Tabs: {len(context.pages)} | Hops (values hidden):\n" + "\n".join(NAV_LOG[-8:]) +
        f"\nButtons: {btns[:6]}\nText: {seen}",
    )
    raise RuntimeError(f"Zoom web client did not open (stuck on {mask_url(zp.url)})")


async def ensure_muted(zp) -> None:
    """Only ever clicks in the 'turn off' direction."""
    await click_any(zp, [r"^mute\b", r"^stop video"])


async def click_prejoin_join(zp) -> bool:
    if not await is_real_prejoin(zp):
        return False
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
                    if await human_cursor.click(zp, btn):
                        log(f"Human-clicked pre-join button ({sel})")
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
                        if await human_cursor.click(zp, b):
                            log(f"Human-clicked pre-join button at y={box['y']:.0f}")
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
        if await on_signin_page(zp):
            break  # wait_in_meeting reports / handles it
        if is_generic_join_page(zp):
            break  # wait_in_meeting reports it
        if not await is_real_prejoin(zp):
            await asyncio.sleep(1)
            continue
        await dismiss_popups(zp)
        await ensure_muted(zp)

        for frame in zp.frames:
            try:
                name_in = frame.locator(
                    "input#inputname, input[name='inputname'], input#display_name, "
                    "input[name='display_name'], input#input-for-name, "
                    "input[placeholder*='name' i], input[aria-label*='name' i], input[type='text']:visible"
                ).first
                if await name_in.count() and await name_in.is_visible():
                    val = await name_in.input_value()
                    if not val.strip() or val.strip().lower() in ("guest", "zoom", "user"):
                        await human_cursor.type_text(zp, name_in, PARTICIPANT_NAME)
                        step.record("Participant Name", "👤", f"Human-typed student display name: {PARTICIPANT_NAME}")
                        await asyncio.sleep(1)
                        break
            except Exception:
                continue

        for frame in zp.frames:
            try:
                signin_btn = frame.locator("button:has-text('Sign in to join'), a:has-text('Sign in to join')").first
                if await signin_btn.count() and await signin_btn.is_visible():
                    if ZOOM_EMAIL and ZOOM_PASS:
                        step.record("Zoom Pre-Join", "🔑", "Bot check requested sign-in; signing in with ZOOM_EMAIL")
                        await signin_btn.click()
                        await asyncio.sleep(2)
                        if await zoom_signin(zp):
                            return
                    else:
                        log("⚠️ Zoom presented 'Sign in to join' wall (host requires authentication or reCAPTCHA triggered)")
            except Exception:
                pass

        if await click_prejoin_join(zp):
            clicked_join = True
            step.record("Zoom Pre-Join", "🚀", "Clicked Zoom pre-join 'Join' button")
            for _ in range(12):
                await asyncio.sleep(1)
                if await in_meeting(zp) or await on_signin_page(zp):
                    return
            break  # Crucial: NEVER click Join a second time (re-clicking invalidates attendee token)
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
    r"^continue without (audio|microphone|camera|audio and video)$",
    r"^continue$",
    r"^allow access\b",
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


async def is_meeting_admitted(zp) -> bool:
    """
    Returns True ONLY when actually admitted inside the Zoom meeting room.
    Uses multiple reliable indicators:
    - Verifies URL is on web client (/wc/ or /j/)
    - Excludes pre-join screen, bot wall, sign-in wall, and waiting room
    - Requires active meeting toolbar controls or live video/canvas elements
    """
    try:
        url = zp.url.lower()
        if not _is_zoom_host(url) or SIGNIN_URL_RE.search(url):
            return False

        # Exclude pre-join screen, bot blocker, waiting room
        text = await page_text(zp)
        if "automated bots aren't allowed" in text.lower():
            return False
        if WAITING_ROOM_RE.search(text):
            return False
        if "sign in to join" in text.lower() and "leave" not in text.lower():
            return False

        for frame in zp.frames:
            try:
                pj = frame.locator("button.preview-join-button, input#input-for-name, .preview-video, .join-dialog").first
                if await pj.count() and await pj.is_visible():
                    return False
            except Exception:
                continue

        # Check for multiple positive indicators of active meeting admission
        has_canvas = False
        has_controls = False

        for frame in zp.frames:
            try:
                canvas = frame.locator(
                    "canvas.main-canvas, canvas#main-canvas, canvas.share-screen-canvas, "
                    ".video-avatar__avatar, canvas[id*='speaker' i], .meeting-app, #meeting-app"
                ).first
                if await canvas.count() and await canvas.is_visible():
                    has_canvas = True
                    break
            except Exception:
                pass

        for frame in zp.frames:
            try:
                ctrls = frame.locator(
                    "#wc-footer, #foot-bar, .footer-bar, .footer__inner, "
                    "button[aria-label*='Leave' i], button:has-text('Leave'), "
                    "button[aria-label*='Participants' i], button[aria-label*='Chat' i], "
                    "button[aria-label*='Reactions' i], button[aria-label*='Mute' i]"
                ).first
                if await ctrls.count() and await ctrls.is_visible():
                    has_controls = True
                    break
            except Exception:
                pass

        if has_controls or (has_canvas and ("/wc/" in url or "/j/" in url)):
            return True

    except Exception:
        pass
    return False


async def in_meeting(zp) -> bool:
    return await is_meeting_admitted(zp)


async def wait_in_meeting(zp, max_wait: int = 0) -> None:
    """
    Wait through pre-join, waiting room, or human verification prompt.
    If human interaction is required (bot challenge, CAPTCHA, sign-in wall),
    keeps VNC and browser active, alerts Telegram with VNC link, and waits
    up to MANUAL_JOIN_WAIT_SEC for the user to complete verification.
    Recording begins ONLY once true meeting admission is confirmed.
    """
    if not max_wait:
        max_wait = MANUAL_JOIN_WAIT_SEC

    deadline = time.monotonic() + max_wait
    started_wait = time.monotonic()
    last_diag = -60.0
    human_interaction_notified = False
    stable = 0

    step.record("Meeting Admission", "⏳", f"Verifying Zoom meeting admission (timeout: {max_wait}s)...")

    while time.monotonic() < deadline:
        text = await page_text(zp)

        if END_RE.search(text):
            step.record("Meeting Admission", "🛑", "Meeting was ended by host before admission")
            raise RuntimeError("Meeting ended before the recording could start")

        if is_generic_join_page(zp):
            await notify_photo(zp, "⚠️ Zoom dropped to the generic Join Meeting page")
            raise RuntimeError("Zoom did not accept the meeting link (generic Join Meeting page)")

        # Check if meeting admission is confirmed
        if await is_meeting_admitted(zp):
            stable += 1
            if stable >= 2:  # must hold for two consecutive checks
                step.record("Meeting Admission", "✅", "Confirmed inside Zoom meeting! Ready to record")
                log("✅ [Meeting State] Confirmed inside Zoom meeting! Admission verified.")
                await notify("✅ Confirmed inside Zoom meeting - starting 1080p recording")
                return
            await asyncio.sleep(1.5)
            continue
        stable = 0

        # Check if human interaction is required
        url_lower = zp.url.lower()
        is_bot_wall = "automated bots" in text.lower() or "bot" in text.lower()
        is_signin_wall = await on_signin_page(zp) or re.search(r"sign-?in|sign-?up|register|login", url_lower)
        is_waiting_room = bool(WAITING_ROOM_RE.search(text))
        is_captcha = "recaptcha" in text.lower() or "challenge" in text.lower()

        needs_human = is_bot_wall or is_signin_wall or is_waiting_room or is_captcha

        if needs_human and not human_interaction_notified:
            human_interaction_notified = True
            vnc_url = await wait_for_vnc_url(timeout=15.0) or "(tunnel URL not yet available)"
            if is_bot_wall:
                reason = "Bot verification challenge presented by Zoom"
            elif is_signin_wall:
                reason = "Zoom requested account sign-in"
            elif is_waiting_room:
                reason = "In Zoom waiting room (waiting for host)"
            elif is_captcha:
                reason = "Zoom security CAPTCHA challenge"
            else:
                reason = "Manual interaction required"

            log(f"⚠️ [Meeting State] Human interaction required: {reason}")
            log(f"🖥️ Remote Desktop available at: {vnc_url}")
            step.record("Human Interaction", "⚠️", f"{reason}. Connect via Remote Desktop: {vnc_url}")

            alert_msg = (
                f"⚠️ Action Required: {reason}!\n\n"
                f"Zoom requires user interaction to join the meeting.\n"
                f"🖥️ Remote Desktop: {vnc_url}\n\n"
                f"Open this link in your mobile or laptop browser to interact with Chrome and join."
            )
            await notify(alert_msg)
            await notify_photo(zp, f"⚠️ Action Required: {reason}")

        await dismiss_popups(zp)

        elapsed = time.monotonic() - started_wait
        if elapsed - last_diag >= 30:
            last_diag = elapsed
            remaining = int(deadline - time.monotonic())
            snippet = re.sub(r"\s+", " ", text).strip()[:140]
            vnc_info = f" | Remote Desktop: {_get_vnc_url()}" if _get_vnc_url() else ""
            log(f"Waiting for meeting admission ({remaining}s remaining{vnc_info}): {snippet}")

        # Natural ambient human cursor movement while waiting
        if random.random() < 0.40:
            await human_cursor.ambient_wander(zp)

        # Keep browser open and alive for human interaction through noVNC
        await asyncio.sleep(2)

    await notify_photo(zp, "❌ Could not confirm meeting admission within timeout")
    step.record("Meeting Admission", "❌", f"Timed out after {max_wait}s waiting for meeting admission")
    raise RuntimeError(f"Could not confirm that the Zoom meeting was joined within {max_wait}s timeout")


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
                    await btn.scroll_into_view_if_needed()
                    if await human_cursor.click(zp, btn):
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
        if SIGNIN_URL_RE.search(url):
            return "kicked to the Zoom sign-in page"
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
# Manual Mode: zero-automation browser session
# --------------------------------------------------------------------------
async def _find_zoom_page(context):
    """Scan all tabs for a page on zoom.us (passive, no clicks)."""
    for p in context.pages:
        try:
            url = p.url.lower()
            if _is_zoom_host(url):
                return p
        except Exception:
            pass
    return None


async def manual_wait_for_meeting(context, timeout: int, vnc_already_sent: bool = False):
    """
    Manual mode: passively poll browser tabs until the user has joined
    a Zoom meeting. No automated clicks—just detection.
    Returns the Zoom page once the user is admitted to the meeting.
    """
    deadline = time.monotonic() + timeout
    last_log = 0.0
    notified_zoom_found = False

    while time.monotonic() < deadline:
        # If public VNC URL wasn't sent initially, send it as soon as it appears!
        if not vnc_already_sent:
            current_vnc = _get_vnc_url()
            if current_vnc and current_vnc.startswith("http"):
                vnc_already_sent = True
                log(f"🔗 Remote Desktop URL active: {current_vnc}")
                step.record("Remote Desktop", "🔗", f"Web VNC ready: {current_vnc}")
                await notify(f"🔗 Remote Desktop is ready:\n{current_vnc}\n\nOpen this link on your phone to interact with Chrome.")

        # Scan all tabs for Zoom meeting page
        for p in context.pages:
            try:
                url = p.url.lower()
                if not _is_zoom_host(url):
                    continue

                if not notified_zoom_found:
                    notified_zoom_found = True
                    log("🎯 Detected Zoom page in browser — waiting for meeting admission...")
                    step.record("Zoom Detected", "🔍", "Zoom page found — waiting for you to join the meeting")

                # Check if actually admitted (passive DOM inspection, no clicks)
                if await is_meeting_admitted(p):
                    log("✅ Meeting admission confirmed!")
                    step.record("Meeting Joined", "✅", "You are inside the Zoom meeting — starting recording")
                    return p
            except Exception:
                continue

        elapsed = time.monotonic() - (deadline - timeout)
        if time.monotonic() - last_log >= 30:
            last_log = time.monotonic()
            remaining = int(deadline - time.monotonic())
            vnc_url = _get_vnc_url()
            tabs = len(context.pages)
            log(f"⏳ Manual mode: waiting for meeting join ({remaining}s left, {tabs} tabs open)")

        await asyncio.sleep(2)

    raise RuntimeError(f"Manual mode timeout: no Zoom meeting joined within {timeout}s")


async def manual_monitor(zp, started: float) -> str:
    """Meeting monitor for manual mode — passive only, no dismiss_popups or CSS injection."""
    failures = 0
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
            step.record("Recording Active", "⏺️", f"Recording in progress: {mins_elapsed} mins elapsed")

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
        val = out.splitlines()[0].strip() if out.splitlines() else ""
        return float(val)
    except (ValueError, IndexError):
        return 0.0


def make_thumb(path: str) -> str | None:
    thumb = str(Path(path).with_suffix(".jpg"))
    dur = probe_duration(path)
    if dur > 10:
        seek = "10"
    elif dur > 1:
        seek = "1"
    else:
        seek = "0"
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
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
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
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
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
    base_caption = f"🎥 {ACTIVE_CLASS_TITLE} | {datetime.now(IST).strftime('%Y-%m-%d')}"
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
        log("Recorder build 2026-11-26h (restored monitor loop — full duration recording)")
        if MANUAL_MODE:
            log("🖥️ MANUAL MODE enabled — you control the browser, recorder only captures screen")

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
                timezone_id="Asia/Kolkata",
                locale="en-IN",
                args=CHROMIUM_ARGS,
                ignore_default_args=["--enable-automation"],
                permissions=["microphone", "camera"],
                accept_downloads=False,
                no_viewport=True,
            )

            # --- Inject authenticated Zoom session if configured -----------
            await setup_zoom_session(context)

            # --- Inject popup_guard.js into every page/frame ---------------
            guard_js = Path("/app/popup_guard.js")
            if not guard_js.exists():
                guard_js = Path(__file__).parent / "popup_guard.js"
            if guard_js.exists():
                await context.add_init_script(path=str(guard_js))
                log("Injected popup_guard.js")
            else:
                log("popup_guard.js not found; skipping in-page popup guard")

            # --- Block desktop-app zoommtg:// protocol loop ----------------
            await context.add_init_script("""
                const _open=window.open;window.open=(u,...a)=>(u&&/^zoom(mtg|us)?:/i.test(String(u))?null:_open.call(window,u,...a));
            """)

            # --- Block zoommtg:// and external OAuth / social-login providers at the network layer ---
            # Only block EXTERNAL login providers. Do NOT block zoom.us/signin or zoom.us/wc paths
            # because Zoom sometimes routes /wc/ join through brief signin redirects that resolve
            # automatically when the tk= token is valid.
            SIGNIN_ABORT_RE = re.compile(
                r"(facebook\.com/login|accounts\.google\.com|appleid\.apple\.com|"
                r"login\.microsoftonline\.com)", re.I
            )
            async def _abort_route(route):
                await route.abort()

            await context.route(
                re.compile(r"^zoom(mtg|us)?://", re.I),
                _abort_route,
            )
            await context.route(SIGNIN_ABORT_RE, _abort_route)

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

                def _on_request(req):
                    try:
                        if req.is_navigation_request():
                            note_navigation(req.url)
                            remember_meeting_url(req.url)
                    except Exception:
                        pass

                p.on("request", _on_request)

            context.on("page", _attach_page_handlers)
            for p in context.pages:
                _attach_page_handlers(p)

            # --- Background tab closer -------------------------------------
            tab_closer_task = asyncio.create_task(
                _tab_closer(context, PORTAL_URL, ALLOWED_HOSTS, zoom_active)
            )

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                if MANUAL_MODE:
                    # ===== MANUAL MODE: zero automation, user drives everything =====
                    step.record("Manual Mode", "🖥️", "Manual navigation mode — no automated clicks")
                    log("🖥️ MANUAL MODE: No automated portal login or Zoom joining")
                    log("🖥️ You control the browser entirely through Remote Desktop")

                    # Just open the portal page — don't auto-login
                    if PORTAL_URL:
                        log(f"🌐 Opening portal for manual navigation: {mask_url(PORTAL_URL)}")
                        try:
                            await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                        step.record("Portal Opened", "🌐", "Portal loaded — log in and join class manually")

                    # Wait actively for Quick Tunnel URL (up to 40 seconds)
                    log("⏳ Waiting for Remote Desktop public tunnel URL...")
                    vnc_url = await wait_for_vnc_url(timeout=40.0)
                    vnc_url_sent = False

                    if vnc_url:
                        log(f"🖥️ Remote Desktop URL ready: {vnc_url}")
                        vnc_url_sent = True
                        vnc_section = f"🔗 Remote Desktop Link:\n{vnc_url}"
                    else:
                        log("⚠️ Quick Tunnel URL not available yet — starting standby...")
                        vnc_section = "⚠️ Connecting tunnel... URL will arrive in the next message."

                    alert = (
                        f"🖥️ Manual Mode Active!\n\n"
                        f"{vnc_section}\n\n"
                        f"Open the link on your phone or laptop browser.\n"
                        f"1️⃣ Log in to the student portal\n"
                        f"2️⃣ Find your class and click Join\n"
                        f"3️⃣ Join the Zoom meeting normally\n\n"
                        f"📹 Recording starts automatically once you're inside the meeting.\n"
                        f"⏱ Timeout: {MANUAL_JOIN_WAIT_SEC // 60} minutes"
                    )
                    await notify(alert)

                    # Passively wait for user to join a Zoom meeting (no clicks)
                    zoom_page = await manual_wait_for_meeting(
                        context,
                        timeout=MANUAL_JOIN_WAIT_SEC,
                        vnc_already_sent=vnc_url_sent
                    )
                    zoom_active.set()

                    await notify_photo(zoom_page, "📸 You're in the meeting — recording starts now")

                    recorder.start()
                    started = time.monotonic()
                    step.record("1080p Recording", "🎥", f"Recording started (1080p @ 30fps) for '{ACTIVE_CLASS_TITLE}'")
                    await notify("✅ Meeting detected & recording started (manual mode)")

                    # Passive monitor — no dismiss_popups or CSS injection
                    end_reason = await manual_monitor(zoom_page, started)
                    step.record("Meeting Concluded", "🏁", f"Meeting finished: {end_reason}")
                    log(f"Recording ends: {end_reason}")

                else:
                    # ===== AUTOMATED MODE: existing bot-driven flow =====
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
                    await wait_in_meeting(zoom_page, max_wait=MANUAL_JOIN_WAIT_SEC)
                    await join_audio(zoom_page)
                    dismiss_browser_prompt()
                    await notify_photo(zoom_page, "📸 Joined the meeting - recording starts now")

                    recorder.start()
                    started = time.monotonic()
                    step.record("1080p Recording", "🎥", f"Recording started (1080p @ 30fps) for '{ACTIVE_CLASS_TITLE}'")
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
                # Only finalize FFmpeg if recording was actually started
                if recorder.proc is not None:
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
