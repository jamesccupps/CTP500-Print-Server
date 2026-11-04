#!/bin/sh
# Installs the CTP500 print server as a service on a Raspberry Pi (or any Debian-style Linux).
#   ./pi/install.sh                      # finds the printer by itself (switch it on first)
#   ./pi/install.sh AA:BB:CC:DD:EE:FF    # this printer (list nearby ones: python3 -m ctp500 find)
#   ./pi/install.sh sim                  # no printer: a simulated one, to try the web app
# Safe to run again (also to update after a git pull); it keeps your settings.
set -eu

DIR=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=$(id -un)
PRINTER=${1:-}
if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user (it uses sudo when it needs to), not as root." >&2
    exit 1
fi

echo "== packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 python3-pil python3-qrcode python3-numpy \
    python3-venv fonts-dejavu-core bluez >/dev/null
sudo systemctl enable --now bluetooth >/dev/null 2>&1 || true

echo "== AI line art (onnxruntime in $DIR/.venv, which also sees the system packages above)"
[ -x "$DIR/.venv/bin/python" ] || python3 -m venv --system-site-packages "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --disable-pip-version-check onnxruntime \
    || echo "   onnxruntime didn't install; drawing styles fall back to edge detection"
PY="$DIR/.venv/bin/python"
(cd "$DIR" && "$PY" -m ctp500 models) || echo "   models didn't download; drawing styles fall back to edge detection"

echo "== settings"
cd "$DIR"
CURRENT=$(python3 -c "from ctp500 import config; print(config.load()['printer'])")
if [ -z "$PRINTER" ] && { [ -z "$CURRENT" ] || [ "$CURRENT" = sim ]; }; then
    echo "== looking for the printer for 15 s (switch it on; it mustn't be connected to a phone)"
    FOUND=$(python3 -m ctp500 find --seconds 15 2>/dev/null | grep "looks like a CTP500" | cut -d' ' -f1 || true)
    COUNT=$(printf '%s' "$FOUND" | grep -c . || true)
    if [ "$COUNT" = 1 ]; then
        PRINTER=$FOUND
        echo "   found $PRINTER"
    elif [ "$COUNT" -gt 1 ]; then
        echo "   found more than one; run again with the address of yours:"
        python3 -m ctp500 find --seconds 1 2>/dev/null | grep "looks like a CTP500" | sed 's/^/     /' || true
    else
        echo "   no printer found: using a simulated one for now. Switch the printer on and run this again"
        echo "   (or give its address: ./pi/install.sh AA:BB:CC:DD:EE:FF)."
    fi
fi
if [ -n "$PRINTER" ]; then
    python3 -m ctp500 config printer="$PRINTER" >/dev/null
elif [ -z "$CURRENT" ]; then
    python3 -m ctp500 config printer=sim >/dev/null
fi

if [ -n "$PRINTER" ] && [ "$PRINTER" != "sim" ]; then
    echo "== pairing $PRINTER (the printer must be on and not connected to a phone)"
    python3 -m ctp500 pair "$PRINTER" || echo "   pairing didn't finish; the server will still try to connect"
fi

echo "== service"
sudo tee /etc/systemd/system/ctp500.service >/dev/null <<EOF
[Unit]
Description=CTP500 thermal printer server
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$PY -m ctp500 serve --quiet
Restart=always
RestartSec=3
Nice=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable ctp500 >/dev/null 2>&1
sudo systemctl restart ctp500
sleep 2
systemctl --no-pager --lines=3 status ctp500 | sed -n '1,3p;/ctp500 0/p'
PORT=$(python3 -c "from ctp500 import config; print(config.load()['port'])")
NOW=$(python3 -c "from ctp500 import config; print(config.load()['printer'])")
TOKEN=$(python3 -c "from ctp500 import config; print('yes' if config.load()['token'] else '')")
IP=$(hostname -I | awk '{print $1}')
echo
echo "Ready. Open the web app on any phone or computer on this network:"
echo "    http://$IP:$PORT    (or http://$(hostname).local:$PORT)"
[ "$NOW" = sim ] && echo "Printer: simulated (prints show up in History). Run this again with the printer on to use it."
[ -z "$TOKEN" ] && echo "Anyone on your network can print. To require a token: see \"Lock it down\" in README.md."
echo "Logs: journalctl -u ctp500 -f"
