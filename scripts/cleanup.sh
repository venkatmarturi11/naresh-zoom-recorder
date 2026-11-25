#!/usr/bin/env bash
# Gracefully stops all background processes
echo "[cleanup] Shutting down background services..."

pkill -f "cloudflared" 2>/dev/null || true
pkill -f "websockify" 2>/dev/null || true
pkill -f "x11vnc" 2>/dev/null || true
pkill -f "xfwm4" 2>/dev/null || true
pkill -f "xfsettingsd" 2>/dev/null || true
pkill -f "unclutter" 2>/dev/null || true
pkill -f "Xvfb" 2>/dev/null || true
pulseaudio --kill 2>/dev/null || true

echo "[cleanup] Cleanup complete."
