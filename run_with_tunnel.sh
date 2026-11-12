#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

# Source virtualenv
source "$DIR/venv/bin/activate"

# Compile C accelerator if not present
if [ ! -f "$DIR/eye4_accel.so" ]; then
    echo "[*] Compiling C accelerator..."
    gcc -O2 -shared -fPIC -o "$DIR/eye4_accel.so" "$DIR/eye4_accel.c"
fi

echo "[*] Starting Camera Hub WebUI on port 8080..."
python3 camera_hub.py --web-host 0.0.0.0 --web-port 8080 "$@" &
HUB_PID=$!

cleanup() {
    echo "[*] Shutting down services..."
    kill $HUB_PID 2>/dev/null || true
    kill $CF_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

sleep 2

echo "[*] Starting Cloudflared Tunnel..."
cloudflared tunnel --config /dev/null --url http://127.0.0.1:8080 &
CF_PID=$!

wait $HUB_PID
