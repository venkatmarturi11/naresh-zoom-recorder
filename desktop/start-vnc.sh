#!/usr/bin/env bash
set -e

# VNC and noVNC (HTML5 browser remote desktop) startup
DISPLAY_NUM="${DISPLAY:-:99}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PASSWORD="${VNC_PASSWORD:-}"

echo "[vnc] Configuring VNC server for display ${DISPLAY_NUM}..."

# Setup VNC password authentication
AUTH_ARGS=()
if [ -n "$VNC_PASSWORD" ]; then
    mkdir -p "$HOME/.vnc"
    x11vnc -storepasswd "$VNC_PASSWORD" "$HOME/.vnc/passwd" >/dev/null 2>&1
    AUTH_ARGS=("-rfbauth" "$HOME/.vnc/passwd")
    echo "[vnc] Password authentication enabled."
else
    echo "[vnc] Notice: VNC_PASSWORD not set; operating without password."
    AUTH_ARGS=("-nopw")
fi

# Start x11vnc bound strictly to 127.0.0.1 (never exposed directly to public internet)
x11vnc -display "${DISPLAY_NUM}" \
       -forever \
       -shared \
       -rfbport "${VNC_PORT}" \
       -listen 127.0.0.1 \
       "${AUTH_ARGS[@]}" \
       -bg \
       -o /tmp/x11vnc.log

echo "[vnc] x11vnc active on 127.0.0.1:${VNC_PORT}"

# Locate noVNC static web assets
NOVNC_DIR="/usr/share/novnc"
if [ ! -d "$NOVNC_DIR" ]; then
    NOVNC_DIR="/opt/novnc"
fi

# Ensure mobile-friendly index.html exists
if [ -f "${NOVNC_DIR}/vnc.html" ] && [ ! -f "${NOVNC_DIR}/index.html" ]; then
    ln -sf "${NOVNC_DIR}/vnc.html" "${NOVNC_DIR}/index.html" 2>/dev/null || true
fi

echo "[vnc] Starting noVNC web interface on 127.0.0.1:${NOVNC_PORT}..."
websockify --web "$NOVNC_DIR" 127.0.0.1:"${NOVNC_PORT}" 127.0.0.1:"${VNC_PORT}" >/dev/null 2>&1 &

echo "[vnc] noVNC ready on 127.0.0.1:${NOVNC_PORT}"
