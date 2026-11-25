#!/usr/bin/env bash
set -e

# Container entrypoint script
# 1. Starts desktop and display
# 2. Starts audio
# 3. Starts VNC and noVNC
# 4. Optionally starts cloudflared if CF_TUNNEL_TOKEN is provided inside container
# 5. Executes recorder.py

/app/desktop/start-desktop.sh
/app/desktop/start-audio.sh
/app/desktop/start-vnc.sh

/app/scripts/health-check.sh || true

if [ -n "$CF_TUNNEL_TOKEN" ]; then
    echo "[entrypoint] Starting Cloudflare Named Tunnel..."
    cloudflared tunnel --no-autoupdate run --token "$CF_TUNNEL_TOKEN" >/tmp/cloudflared.log 2>&1 &
    echo "[entrypoint] Cloudflare Named Tunnel started."
fi

# Set trap for graceful cleanup
trap '/app/scripts/cleanup.sh' EXIT INT TERM

# Run python recorder with arguments
exec python /app/recorder.py "$@"
