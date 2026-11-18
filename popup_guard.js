/* Popup guard: injected into EVERY page and iframe (portal + Zoom web client)
 * via Playwright's add_init_script.
 *  - hides cookie banners / consent overlays immediately
 *  - blocks window.open() inside Zoom pages (marketing / survey popups)
 *  - auto-dismisses harmless dialogs, toasts and banners ("Got it", "Not now",
 *    cookie accept, recording notice, "Stay muted", ...)
 *  - NEVER touches dialogs that contain input fields, passcode/name prompts,
 *    the Join Audio dialog, error/ended notices, or anything that says leave/end.
 */
(() => {
  "use strict";
  if (window.__popupGuardInstalled) return;
  window.__popupGuardInstalled = true;

  const host = location.hostname || "";
  const isZoom = /(^|\.)zoom(gov)?\.(us|com|cn)$/i.test(host);

  if (isZoom) {
    try { window.open = function () { return null; }; } catch (e) {}
  }

  const CSS =
    "#onetrust-consent-sdk,#onetrust-banner-sdk,#onetrust-pc-sdk,.onetrust-pc-dark-filter," +
    "#CybotCookiebotDialog,#cookie-law-info-bar,.cc-window{display:none!important;visibility:hidden!important}" +
    (isZoom
      ? "[class*='toast' i],[class*='snackbar' i],[role='tooltip']{display:none!important}"
      : "");

  function ensureCss() {
    try {
      if (document.getElementById("__pg_css")) return;
      const root = document.head || document.documentElement;
      if (!root) return;
      const s = document.createElement("style");
      s.id = "__pg_css";
      s.textContent = CSS;
      root.appendChild(s);
    } catch (e) {}
  }

  const CONTAINERS = [
    '[role="dialog"]', '[role="alertdialog"]', "dialog[open]",
    ".zm-modal", ".ReactModal__Content", ".modal-dialog", ".modal.show", ".modal.in",
    "#onetrust-banner-sdk", "#onetrust-pc-sdk", '[id*="cookie" i]',
    '[class*="cookie-banner" i]', '[class*="cookie-consent" i]', '[class*="toast" i]',
    '[class*="snackbar" i]', '[class*="notification" i]',
  ].join(",");

  const BUTTONS = 'button,[role="button"],a,input[type="button"],input[type="submit"]';

  const SAFE = new RegExp(
    "^(got it|ok|okay|dismiss|close|not now|maybe later|remind me later|later|skip|" +
    "skip for now|skip tour|no thanks|no, thanks|don'?t show( this)? again|" +
    "do not show( this)? again|accept all( cookies)?|accept cookies|reject all( cookies)?|" +
    "i agree|i understand|stay muted|keep muted|" +
    "continue without (microphone|audio|camera|microphone and camera|mic and camera))$", "i");
  const CONSENT = /^(continue|i consent)$/i;
  const CLOSE = /^(close|dismiss|close dialog|close popup|×|✕|✖|x)$/i;
  const PROTECT = new RegExp(
    "join audio|computer audio|audio conference|test speaker|passcode|password|" +
    "enter your name|waiting room|host will let you in|please wait|invalid|expired|" +
    "has ended|been ended|removed from|leave meeting|end meeting|sign in|log ?in", "i");
  const FIELDS =
    'input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([type="button"]):not([type="submit"]),textarea,select';

  const visible = (el) => {
    try {
      if (!el || !el.isConnected || !el.getClientRects().length) return false;
      const s = getComputedStyle(el);
      return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
    } catch (e) { return false; }
  };
  const label = (b) =>
    String(b.getAttribute("aria-label") || b.innerText || b.value || b.title || "")
      .replace(/\s+/g, " ").trim();
  const cls = (b) => {
    const c = b.className;
    return String(c && c.baseVal !== undefined ? c.baseVal : c || "");
  };

  function dismissOnce() {
    ensureCss();
    let n = 0;
    let list;
    try { list = document.querySelectorAll(CONTAINERS); } catch (e) { return 0; }
    const clicked = new Set();
    for (const c of list) {
      try {
        if (!visible(c)) continue;
        const text = String(c.innerText || "").trim();
        if (text.length > 700) continue;           // big layout area, not a popup
        if (PROTECT.test(text)) continue;          // never touch these
        if (c.querySelector(FIELDS)) continue;     // forms / name / passcode inputs
        const btns = Array.from(c.querySelectorAll(BUTTONS)).filter(visible);
        if (!btns.length) continue;
        let t = btns.find((b) => SAFE.test(label(b)));
        if (!t && /record/i.test(text)) t = btns.find((b) => CONSENT.test(label(b)));
        if (!t) t = btns.find((b) => CLOSE.test(label(b)) || /(^|[\s_-])close([\s_-]|$)/i.test(cls(b)));
        if (t && !clicked.has(t)) { clicked.add(t); t.click(); n++; }
      } catch (e) {}
    }
    return n;
  }
  window.__dismissPopups = dismissOnce;

  let pending = false;
  const schedule = () => {
    if (pending) return;
    pending = true;
    setTimeout(() => { pending = false; dismissOnce(); }, 300);
  };

  ensureCss();
  try {
    new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}
  setInterval(dismissOnce, 2000);
  document.addEventListener("DOMContentLoaded", () => { ensureCss(); schedule(); });
})();
