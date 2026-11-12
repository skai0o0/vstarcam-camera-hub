#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

# Check & create virtualenv if not present
if [ ! -d "$DIR/venv" ]; then
    echo "[*] Creating Python virtual environment..."
    python3 -m venv "$DIR/venv"
    source "$DIR/venv/bin/activate"
    echo "[*] Installing dependencies..."
    pip install --upgrade pip
    pip install -r "$DIR/requirements.txt"
else
    source "$DIR/venv/bin/activate"
fi

# Compile C accelerator if gcc is available and .so is missing
if [ ! -f "$DIR/eye4_accel.so" ] && command -v gcc >/dev/null 2>&1; then
    echo "[*] Compiling C accelerator (eye4_accel.so)..."
    gcc -O2 -shared -fPIC -o "$DIR/eye4_accel.so" "$DIR/eye4_accel.c" || echo "[!] C accelerator compile skipped, will use pure Python fallback."
fi

# Show local IP addresses for convenience
echo "==========================================================================="
echo "   VSTARCAM CAMERA HUB - LAN MODE"
echo "==========================================================================="
echo "   Dashboard WebUI: http://127.0.0.1:8080"
if command -v hostname >/dev/null 2>&1; then
    for ip in $(hostname -I 2>/dev/null); do
        echo "   LAN Access     : http://$ip:8080"
    done
fi
echo "==========================================================================="
echo ""

exec python3 "$DIR/camera_hub.py" --web-host 0.0.0.0 --web-port 8080 "$@"
