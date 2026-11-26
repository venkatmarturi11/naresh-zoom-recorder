/* Popup guard: injected into EVERY page and iframe (portal + Zoom web client)
 * via Playwright's add_init_script.
 *  - hides cookie banners / consent overlays immediately
 *  - blocks window.open() to non-Zoom / zoommtg:// targets inside Zoom pages
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
    // Block the desktop-app launcher (zoommtg://) and foreign popups, but let
    // Zoom's own navigation through - the "Join from browser" button relies on it.
    try {
      const _open = window.open;
      window.open = function (u) {
        try {
          if (!u) return _open.apply(window, arguments);
          const url = new URL(String(u), location.href);
          if (/^https?:$/.test(url.protocol) &&
            /(^|\.)zoom(gov)?\.(us|com|cn)$/i.test(url.hostname)) {
            return _open.apply(window, arguments);
          }
        } catch (e) { }
        return null;
      };
    } catch (e) { }
  }

  const CSS =
    "#onetrust-consent-sdk,#onetrust-banner-sdk,#onetrust-pc-sdk,.onetrust-pc-dark-filter," +
    "#CybotCookiebotDialog,#cookie-law-info-bar,.cc-window{display:none!important;visibility:hidden!important}" +
    (isZoom
      ? "\n[class*='toast' i],[class*='snackbar' i],[role='tooltip']," +
      "[class*='notes-guide' i],[class*='coachmark' i],[class*='popover' i],[class*='popover-content' i]," +
      "[class*='tip-bubble' i],[class*='guide-bubble' i],[id*='notes-guide' i]," +
      "[class*='hardware-acceleration' i],[class*='system-warning' i],.warning-bar,[class*='warning-bar' i]," +
      "[class*='system-notice' i],[class*='announcement' i],[class*='announcement-bar' i]," +
      ".speaker-bar,.speaker-bar-container,.filmstrip-box,.filmstrip,.sharing-layout__speaker-bar," +
      ".speaker-active-container,.video-avatar-box,.video-box__video-list," +
      "[class*='filmstrip' i],[class*='speaker-bar' i],[class*='speaker-video' i],[class*='gallery-video' i]," +
      "[class*='video-avatar-list' i],[class*='video-bar' i]" +
      "{display:none!important;visibility:hidden!important;opacity:0!important}"
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
    } catch (e) { }
  }

  const CONTAINERS = [
    '[role="dialog"]', '[role="alertdialog"]', "dialog[open]",
    ".zm-modal", ".ReactModal__Content", ".modal-dialog", ".modal.show", ".modal.in",
    "#onetrust-banner-sdk", "#onetrust-pc-sdk", '[id*="cookie" i]',
    '[class*="cookie-banner" i]', '[class*="cookie-consent" i]', '[class*="toast" i]',
    '[class*="snackbar" i]', '[class*="notification" i]',
    '[class*="coachmark" i]', '[class*="popover" i]', '[class*="tip-bubble" i]',
    '[class*="guide" i]', '[class*="banner" i]', '[class*="bubble" i]',
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

  function dismissOne(c) {
    try {
      if (!visible(c)) return false;
      const text = (c.innerText || c.textContent || "").trim();
      if (!text && !c.querySelector(BUTTONS)) return false;

      // NEVER touch dialogs that contain inputs, passcodes, name prompts, leave/end notices,
      // or sign-in prompts (let user interact freely via remote desktop)
      if (PROTECT.test(text) || c.querySelector(FIELDS)) return false;

      const btns = Array.from(c.querySelectorAll(BUTTONS)).filter(visible);
      for (const b of btns) {
        const l = label(b);
        if (SAFE.test(l)) {
          b.click();
          return true;
        }
      }
      for (const b of btns) {
        const l = label(b);
        if (CONSENT.test(l)) {
          b.click();
          return true;
        }
      }
      for (const b of btns) {
        const l = label(b);
        const cl = cls(b);
        if (CLOSE.test(l) || /close|dismiss|btn-close|banner-close/i.test(cl)) {
          b.click();
          return true;
        }
      }
    } catch (e) { }
    return false;
  }

  function sweep() {
    ensureCss();
    try {
      const candidates = document.querySelectorAll(CONTAINERS);
      for (const c of candidates) {
        dismissOne(c);
      }
    } catch (e) { }
  }

  sweep();
  document.addEventListener("DOMContentLoaded", sweep, { passive: true });
  window.addEventListener("load", sweep, { passive: true });

  const mo = new MutationObserver(() => {
    sweep();
  });
  const root = document.documentElement || document.body;
  if (root) {
    mo.observe(root, { childList: true, subtree: true });
  }
  setInterval(sweep, 1500);
})();
