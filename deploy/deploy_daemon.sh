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

echo "== dashboard generators into place =="
# The dashboard timer (see setup_dashboard.sh) runs FROZEN COPIES in
# /usr/local/bin, not the repo files -- a git pull alone leaves the live
# page rendering old code. Bit us on the 13-station expansion deploy
# (2026-08-05): the box served the two-station page from new code for a
# render cycle. Refresh the copies on every deploy; skip silently if the
# dashboard was never set up on this box.
if [ -f /usr/local/bin/generate_dashboard.py ]; then
    sudo cp "$APP_DIR/deploy/generate_dashboard.py" /usr/local/bin/generate_dashboard.py
    sudo cp "$APP_DIR/deploy/generate_backtest_dashboard.py" /usr/local/bin/generate_backtest_dashboard.py
    sudo chmod 644 /usr/local/bin/generate_dashboard.py /usr/local/bin/generate_backtest_dashboard.py
    sudo systemctl start polyweather-dashboard.service 2>/dev/null || true
fi

echo "== systemd service =="
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=polyweather paper-trading scheduler (all registered stations)
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
sudo systemctl enable $SERVICE
# restart, not `enable --now`: --now is a no-op on an already-running service,
# which left every redeploy onto a live box running the OLD code (bit us 2026-08-07).
sudo systemctl restart $SERVICE

sleep 5
echo "== Service status =="
systemctl is-active $SERVICE
sudo journalctl -u $SERVICE -n 20 --no-pager
