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

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY recorder.py .
RUN chown -R pwuser:pwuser /app

# PulseAudio refuses to run as root, so run as the non-root user
# that ships with the Playwright image.
USER pwuser

CMD ["python", "recorder.py"]
