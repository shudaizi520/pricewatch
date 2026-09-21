#!/bin/sh
set -eu

python -m pricewatch.bootstrap

Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
xvfb_pid=$!
attempt=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        echo "Xvfb exited before its display was ready" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "Xvfb display did not become ready" >&2
        exit 1
    fi
    sleep 0.1
done

export DISPLAY=:99
exec uvicorn pricewatch.app:create_app --factory --host 0.0.0.0 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
