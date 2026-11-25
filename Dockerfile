FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Kolkata \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    PULSE_RUNTIME_PATH=/tmp/pulse \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    HOME=/home/pwuser

# Install Xvfb, XFCE desktop components, VNC, noVNC, PulseAudio, FFmpeg, cloudflared
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11-utils \
        xdotool \
        unclutter \
        xfwm4 \
        xfce4-settings \
        x11vnc \
        novnc \
        websockify \
        pulseaudio \
        pulseaudio-utils \
        ffmpeg \
        netcat-openbsd \
        curl \
        ca-certificates \
        build-essential \
        python3-dev \
        tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime \
    && echo "Asia/Kolkata" > /etc/timezone \
    && curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
    && (dpkg -i /tmp/cloudflared.deb || apt-get install -fy) \
    && rm -f /tmp/cloudflared.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && (pip install --no-cache-dir tgcrypto==1.2.5 || echo 'tgcrypto skipped')

# Chromium enterprise policy: grant media stream permissions & disable protocol popups
RUN mkdir -p /etc/chromium/policies/managed /etc/opt/chrome/policies/managed
COPY chromium-policy/zoom.json /etc/chromium/policies/managed/zoom.json
COPY chromium-policy/zoom.json /etc/opt/chrome/policies/managed/zoom.json

COPY desktop/ ./desktop/
COPY scripts/ ./scripts/
COPY recorder.py popup_guard.js ./

RUN chmod +x ./desktop/*.sh ./scripts/*.sh \
    && chown -R pwuser:pwuser /app /home/pwuser

USER pwuser

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
