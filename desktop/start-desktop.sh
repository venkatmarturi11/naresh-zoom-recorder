#!/usr/bin/env bash
set -e

# Starts virtual display (Xvfb) and XFCE window manager
DISPLAY_NUM="${DISPLAY:-:99}"
RESOLUTION="${RESOLUTION:-1920x1080x24}"

echo "[desktop] Initializing virtual display on ${DISPLAY_NUM} (${RESOLUTION})..."
mkdir -p /tmp/.X11-unix
rm -f "/tmp/.X${DISPLAY_NUM#:}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM#:}"

# Start Xvfb if not already running
if ! xdpyinfo -display "${DISPLAY_NUM}" >/dev/null 2>&1; then
    Xvfb "${DISPLAY_NUM}" -screen 0 "${RESOLUTION}" -ac +extension GLX +render -noreset &
    for i in $(seq 1 40); do
        if xdpyinfo -display "${DISPLAY_NUM}" >/dev/null 2>&1; then
            echo "[desktop] Xvfb ready on ${DISPLAY_NUM}"
            break
        fi
        sleep 0.25
    done
fi

export DISPLAY="${DISPLAY_NUM}"

# Start XFCE Window Manager / Settings daemon
echo "[desktop] Starting XFCE desktop components..."
if command -v xfsettingsd >/dev/null 2>&1; then
    xfsettingsd --replace &
fi
if command -v xfwm4 >/dev/null 2>&1; then
    xfwm4 --compositor=off --replace &
elif command -v xfce4-session >/dev/null 2>&1; then
    xfce4-session --disable-tcp &
fi

# Set dark background color for a clean look
xsetroot -solid "#1e1e2e" 2>/dev/null || true

echo "[desktop] Desktop environment ready on ${DISPLAY_NUM}"
