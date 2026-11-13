FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    PULSE_RUNTIME_PATH=/tmp/pulse

# Virtual display, virtual audio, encoder, cursor hiding, build tools for tgcrypto
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    pulseaudio \
    pulseaudio-utils \
    ffmpeg \
    xdotool \
    unclutter \
    x11-utils \
    ca-certificates \
    gcc \
    libc6-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Pre-approve external protocol handlers (zoommtg, zoomus, zoom) in Chromium policies
# and replace xdg-open with a no-op so the "Open xdg-open?" browser prompt never appears.
RUN mkdir -p /etc/chromium/policies/managed /etc/opt/chrome/policies/managed /etc/chromium-browser/policies/managed \
    && echo '{"AutoLaunchProtocolsFromOrigins":[{"protocol":"zoommtg","allowed_origins":["*"]},{"protocol":"zoomus","allowed_origins":["*"]},{"protocol":"zoom","allowed_origins":["*"]}]}' > /etc/chromium/policies/managed/protocols.json \
    && cp /etc/chromium/policies/managed/protocols.json /etc/opt/chrome/policies/managed/protocols.json \
    && cp /etc/chromium/policies/managed/protocols.json /etc/chromium-browser/policies/managed/protocols.json \
    && echo '#!/bin/sh\nexit 0' > /usr/local/bin/xdg-open \
    && chmod +x /usr/local/bin/xdg-open \
    && cp /usr/local/bin/xdg-open /usr/bin/xdg-open

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY recorder.py .
RUN chown -R pwuser:pwuser /app

# PulseAudio refuses to run as root, so run as the non-root user
# that ships with the Playwright image.
USER pwuser

CMD ["python", "recorder.py"]
