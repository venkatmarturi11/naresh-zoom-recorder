#!/usr/bin/env bash
# Verifies that Xvfb, PulseAudio, VNC, and noVNC are running
DISPLAY_NUM="${DISPLAY:-:99}"
ERRORS=0

echo "[health-check] Checking system status..."

# 1. Display
if xdpyinfo -display "${DISPLAY_NUM}" >/dev/null 2>&1; then
    echo "  [OK] X11 display ${DISPLAY_NUM} is active."
else
    echo "  [FAIL] X11 display ${DISPLAY_NUM} is unreachable."
    ERRORS=$((ERRORS + 1))
fi

# 2. PulseAudio
if pactl info >/dev/null 2>&1; then
    echo "  [OK] PulseAudio daemon is running."
else
    echo "  [FAIL] PulseAudio is not responding."
    ERRORS=$((ERRORS + 1))
fi

# 3. VNC port
if nc -z 127.0.0.1 5900 >/dev/null 2>&1; then
    echo "  [OK] VNC server is listening on 127.0.0.1:5900."
else
    echo "  [WARN] VNC server not listening on port 5900."
fi

# 4. noVNC port
if nc -z 127.0.0.1 6080 >/dev/null 2>&1; then
    echo "  [OK] noVNC is listening on 127.0.0.1:6080."
else
    echo "  [WARN] noVNC not listening on port 6080."
fi

if [ $ERRORS -eq 0 ]; then
    echo "[health-check] All core services are healthy."
    exit 0
else
    echo "[health-check] Core services failed health check ($ERRORS errors)."
    exit 1
fi
