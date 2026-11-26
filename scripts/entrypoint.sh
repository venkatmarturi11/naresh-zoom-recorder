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
    cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" >/tmp/cloudflared-named.log 2>&1 &
    echo "[entrypoint] Cloudflare Named Tunnel started."
fi

# --- Always start a Quick Tunnel for guaranteed public noVNC URL ---
echo "[entrypoint] Starting Cloudflare Quick Tunnel for noVNC (zero-config)..."
cloudflared tunnel --url http://127.0.0.1:6080 >/tmp/cloudflared.log 2>&1 &
CF_PID=$!

# Poll for the trycloudflare.com URL (up to 30 seconds)
for i in $(seq 1 30); do
    if ! kill -0 "$CF_PID" 2>/dev/null; then
        echo "[entrypoint] ❌ cloudflared process exited unexpectedly! Log output:"
        cat /tmp/cloudflared.log || true
        break
    fi
    QUICK_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log | head -n 1 || true)
    if [ -n "$QUICK_URL" ]; then
        FULL_VNC_URL="${QUICK_URL}/vnc.html?autoconnect=true&resize=scale"
        echo "[entrypoint] 🔗 Quick Tunnel noVNC URL: ${FULL_VNC_URL}"
        # Override VNC_PUBLIC_URL if unset or pointing to localhost
        if [ -z "$VNC_PUBLIC_URL" ] || [[ "$VNC_PUBLIC_URL" == *"127.0.0.1"* ]] || [[ "$VNC_PUBLIC_URL" == *"localhost"* ]]; then
            export VNC_PUBLIC_URL="$FULL_VNC_URL"
        fi
        echo "$FULL_VNC_URL" > /out/vnc_url.txt
        break
    fi
    sleep 1
done

cp -f /tmp/cloudflared.log /out/cloudflared.log 2>/dev/null || true

if [ ! -f /out/vnc_url.txt ]; then
    echo "[entrypoint] ⚠️ Quick Tunnel URL not detected yet. Cloudflared log snippet:"
    tail -n 20 /tmp/cloudflared.log || true
fi

# Set trap for graceful cleanup
trap '/app/scripts/cleanup.sh' EXIT INT TERM

# Run python recorder with arguments
exec python /app/recorder.py "$@"
