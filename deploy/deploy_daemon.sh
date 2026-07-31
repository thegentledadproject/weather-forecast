#!/usr/bin/env bash
# Deploy polyweather paper-trading daemon on Ubuntu EC2.
set -euo pipefail

REPO_URL="https://github.com/thegentledadproject/weather-forecast.git"
APP_DIR="$HOME/weather-forecast"
PKG_DIR="$APP_DIR/weather-forecast"
VENV="$APP_DIR/.venv"
SERVICE=polyweather

echo "== Clone or update repo =="
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$APP_DIR"
fi

echo "== Python venv + deps =="
if ! python3 -m venv "$VENV" 2>/dev/null; then
    sudo apt-get update -q && sudo apt-get install -y -q python3-venv
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
# py-clob-client-v2 (live trading only) intentionally excluded from the paper deployment.
"$VENV/bin/pip" install --quiet requests beautifulsoup4

echo "== Sanity check: entrypoint imports and argparse work =="
cd "$PKG_DIR"
"$VENV/bin/python" scheduler.py --help >/dev/null
"$VENV/bin/python" paper_trading_report.py --help >/dev/null

echo "== systemd service =="
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=polyweather paper-trading scheduler (WSSS/WMKK)
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=$PKG_DIR
ExecStart=$VENV/bin/python scheduler.py --mode paper
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE

sleep 5
echo "== Service status =="
systemctl is-active $SERVICE
sudo journalctl -u $SERVICE -n 20 --no-pager
