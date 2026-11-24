#!/bin/sh
set -eu
umask 077
mkdir -p /data/chrome-profile /data/reports
Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp &
# Wait until the virtual X socket appears on slow/small VPS instances.
attempt=0
until [ -S /tmp/.X11-unix/X99 ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 40 ]; then
        echo "Xvfb did not start" >&2
        exit 1
    fi
    sleep 0.25
done
sleep 0.3
fluxbox -display :99 >/dev/null 2>&1 &
# VNC port 5900 is accessible *inside* the container only.
x11vnc -display :99 -localhost -forever -shared -nopw -rfbport 5900 -quiet >/dev/null 2>&1 &
# Browser UI is published by docker-compose on HOST LOOPBACK ONLY.
websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/dev/null 2>&1 &
exec python -m guardian
