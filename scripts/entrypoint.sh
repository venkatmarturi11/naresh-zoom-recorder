#!/usr/bin/env bash
set -e

# Container entrypoint script
# 1. Starts desktop and display
# 2. Starts audio
# 3. Starts VNC and noVNC
# 4. Starts Cloudflare Named Tunnel or automatic Quick Tunnel
# 5. Executes recorder.py

/app/desktop/start-desktop.sh
/app/desktop/start-audio.sh
/app/desktop/start-vnc.sh

/app/scripts/health-check.sh || true

mkdir -p /out

# --- Cloudflare Named Tunnel (optional, runs alongside Quick Tunnel) ---
if [ -n "$CF_TUNNEL_TOKEN" ]; then
    echo "[entrypoint] Starting Cloudflare Named Tunnel (background)..."
    cloudflared tunnel --no-autoupdate run --token "$CF_TUNNEL_TOKEN" >/tmp/cloudflared-named.log 2>&1 &
    echo "[entrypoint] Cloudflare Named Tunnel started."
fi

# --- Always start a Quick Tunnel for guaranteed public noVNC URL ---
# The Quick Tunnel auto-generates a public https://xxx.trycloudflare.com URL.
# This works even if CF_TUNNEL_TOKEN is set (they run independently).
echo "[entrypoint] Starting Cloudflare Quick Tunnel for noVNC (zero-config)..."
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:6080 >/tmp/cloudflared.log 2>&1 &

# Poll for the trycloudflare.com URL (up to 15 seconds)
for i in $(seq 1 30); do
    QUICK_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log | head -n 1 || true)
    if [ -n "$QUICK_URL" ]; then
        FULL_VNC_URL="${QUICK_URL}/vnc.html?autoconnect=true&resize=scale"
        echo "[entrypoint] 🔗 Quick Tunnel noVNC URL: ${FULL_VNC_URL}"
        # Only override VNC_PUBLIC_URL if user didn't set a custom one
        if [ -z "$VNC_PUBLIC_URL" ]; then
            export VNC_PUBLIC_URL="$FULL_VNC_URL"
        fi
        echo "$FULL_VNC_URL" > /out/vnc_url.txt
        break
    fi
    sleep 0.5
done

if [ ! -f /out/vnc_url.txt ]; then
    echo "[entrypoint] ⚠️ Quick Tunnel URL not detected yet (will retry in recorder.py)"
fi

# Set trap for graceful cleanup
trap '/app/scripts/cleanup.sh' EXIT INT TERM

# Run python recorder with arguments
exec python /app/recorder.py "$@"
