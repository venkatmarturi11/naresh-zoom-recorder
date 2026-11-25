#!/usr/bin/env bash
set -e

# PulseAudio virtual audio configuration
export PULSE_RUNTIME_PATH="${PULSE_RUNTIME_PATH:-/tmp/pulse}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/xdg-runtime}"
mkdir -p "$PULSE_RUNTIME_PATH" "$XDG_RUNTIME_DIR"
chmod 700 "$PULSE_RUNTIME_PATH" "$XDG_RUNTIME_DIR"

SINK="${SINK:-VirtualSpeaker}"

echo "[audio] Initializing PulseAudio daemon..."
pulseaudio --kill 2>/dev/null || true
pulseaudio -D --exit-idle-time=-1 --system=false

for i in $(seq 1 30); do
    if pactl info >/dev/null 2>&1; then
        echo "[audio] PulseAudio daemon ready."
        break
    fi
    sleep 0.25
done

# Create null sink for audio playback (Chromium audio output -> recorded by FFmpeg)
pactl load-module module-null-sink sink_name="${SINK}" sink_properties=device.description="${SINK}" >/dev/null 2>&1 || true
pactl set-default-sink "${SINK}" >/dev/null 2>&1 || true

# Create null sink for virtual microphone input (satisfies WebRTC audio input requirements)
pactl load-module module-null-sink sink_name="VirtualMic" sink_properties=device.description="VirtualMic" >/dev/null 2>&1 || true
pactl set-default-source "VirtualMic.monitor" >/dev/null 2>&1 || true

echo "[audio] VirtualSpeaker (output) and VirtualMic (input) ready."
