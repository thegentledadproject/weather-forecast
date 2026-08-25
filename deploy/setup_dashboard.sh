#!/usr/bin/env bash
# Serve the polyweather dashboard publicly: nginx + 5-min regeneration timer.
set -euo pipefail

VENV_PY=/home/ubuntu/weather-forecast/.venv/bin/python

echo "== nginx =="
sudo apt-get install -y -q nginx >/dev/null
sudo rm -f /var/www/html/index.nginx-debian.html

echo "== generator into place =="
sudo mv /home/ubuntu/generate_dashboard.py /usr/local/bin/generate_dashboard.py
sudo chmod 644 /usr/local/bin/generate_dashboard.py
sudo mv /home/ubuntu/generate_backtest_dashboard.py /usr/local/bin/generate_backtest_dashboard.py
sudo chmod 644 /usr/local/bin/generate_backtest_dashboard.py

echo "== systemd service + timer =="
sudo tee /etc/systemd/system/polyweather-dashboard.service >/dev/null <<UNIT
[Unit]
Description=Regenerate polyweather status dashboard (paper trading + backtest lab)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '$VENV_PY /usr/local/bin/generate_dashboard.py --region asia && $VENV_PY /usr/local/bin/generate_dashboard.py --region europe && $VENV_PY /usr/local/bin/generate_backtest_dashboard.py'
UNIT

sudo tee /etc/systemd/system/polyweather-dashboard.timer >/dev/null <<UNIT
[Unit]
Description=Regenerate polyweather dashboard every 5 minutes

[Timer]
OnBootSec=90
OnUnitActiveSec=300

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now polyweather-dashboard.timer
sudo systemctl start polyweather-dashboard.service

echo "== first render =="
sudo systemctl status polyweather-dashboard.service --no-pager -n 5 | tail -3
echo "== local check =="
curl -s -o /dev/null -w "nginx says: HTTP %{http_code}, %{size_download} bytes\n" http://localhost/
