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

if [ -n "$CF_TUNNEL_TOKEN" ]; then
    echo "[entrypoint] Starting Cloudflare Named Tunnel..."
    cloudflared tunnel --no-autoupdate run --token "$CF_TUNNEL_TOKEN" >/tmp/cloudflared.log 2>&1 &
    echo "[entrypoint] Cloudflare Named Tunnel started."
else
    echo "[entrypoint] Starting Cloudflare Quick Tunnel (zero-config)..."
    cloudflared tunnel --no-autoupdate --url http://127.0.0.1:6080 >/tmp/cloudflared.log 2>&1 &
    # Poll for the trycloudflare.com URL
    for i in $(seq 1 30); do
        QUICK_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log | head -n 1 || true)
        if [ -n "$QUICK_URL" ]; then
            FULL_VNC_URL="${QUICK_URL}/vnc.html?autoconnect=true&resize=scale"
            echo "[entrypoint] 🔗 Cloudflare Quick Tunnel active: ${FULL_VNC_URL}"
            export VNC_PUBLIC_URL="$FULL_VNC_URL"
            echo "$FULL_VNC_URL" > /out/vnc_url.txt
            break
        fi
        sleep 0.5
    done
fi

# Set trap for graceful cleanup
trap '/app/scripts/cleanup.sh' EXIT INT TERM

# Run python recorder with arguments
exec python /app/recorder.py "$@"
