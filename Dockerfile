FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Kolkata \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    PULSE_RUNTIME_PATH=/tmp/pulse \
    PIP_BREAK_SYSTEM_PACKAGES=1

# Virtual display, virtual audio, encoder, cursor hiding, timezone
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        pulseaudio \
        pulseaudio-utils \
        ffmpeg \
        xdotool \
        unclutter \
        x11-utils \
        ca-certificates \
        build-essential \
        python3-dev \
        tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime \
    && echo "Asia/Kolkata" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && (pip install --no-cache-dir tgcrypto==1.2.5 || echo 'tgcrypto skipped (uploads still work, just slower)')

# Chromium policy: never show the "Open xdg-open?" / "Open Zoom Meetings?" prompt
RUN mkdir -p /etc/chromium/policies/managed /etc/opt/chrome/policies/managed
COPY chromium-policy/zoom.json /etc/chromium/policies/managed/zoom.json
COPY chromium-policy/zoom.json /etc/opt/chrome/policies/managed/zoom.json

COPY recorder.py popup_guard.js ./
RUN chown -R pwuser:pwuser /app

# PulseAudio refuses to run as root, so run as the non-root user
# that ships with the Playwright image.
USER pwuser

CMD ["python", "recorder.py"]
