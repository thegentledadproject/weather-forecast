#!/usr/bin/env bash
# Serve the polyweather dashboard publicly: nginx + 5-min regeneration timer.
#
# IDEMPOTENT (WAVE 3, 3b). Re-run it whenever the unit below changes; every
# step tolerates having been done before. The first version mv'd the
# generators out of /home/ubuntu and could only be run once.
set -euo pipefail

APP_DIR=/home/ubuntu/weather-forecast
VENV_PY=$APP_DIR/.venv/bin/python
WEB_ROOT=/var/www/html

echo "== nginx =="
sudo apt-get install -y -q nginx >/dev/null
sudo rm -f $WEB_ROOT/index.nginx-debian.html

echo "== web root owned by ubuntu =="
# The generators run as ubuntu (User= below) and os.replace() their page into
# this directory. The path stays /var/www/html -- nginx serves it and every
# bookmark points at it -- and the recursive chown also takes the .html files
# an earlier root-run timer left behind.
sudo chown -R ubuntu:ubuntu $WEB_ROOT

echo "== ubuntu may read the system journal =="
# generate_dashboard.py tails `journalctl -u polyweather`; as root that was
# free. Group membership is read by systemd when the service starts, so the
# next timer tick sees it.
sudo usermod -aG systemd-journal ubuntu

echo "== generators into place =="
# FROZEN COPIES in /usr/local/bin, refreshed from the repo here and on every
# deploy_daemon.sh run (a git pull alone leaves the page rendering old code).
for gen in generate_dashboard.py generate_backtest_dashboard.py generate_realmoney_dashboard.py; do
    sudo cp "$APP_DIR/deploy/$gen" /usr/local/bin/$gen
    sudo chmod 644 /usr/local/bin/$gen
done

echo "== systemd service + timer =="
# User=ubuntu (WAVE 3, 3b): the generators open the trading database READ-ONLY
# (storage opens mode=ro unless a process calls set_writable, which none of
# the three does -- tests/test_wave3_dashboard_as_ubuntu.py) and write only
# under $WEB_ROOT. Running them as root was how a root-owned file could land
# beside the daemon's ubuntu-owned database.
sudo tee /etc/systemd/system/polyweather-dashboard.service >/dev/null <<UNIT
[Unit]
Description=Regenerate polyweather status dashboard (paper trading + backtest lab)
After=network-online.target

[Service]
Type=oneshot
User=ubuntu
ExecStart=/bin/sh -c '$VENV_PY /usr/local/bin/generate_dashboard.py --region asia && $VENV_PY /usr/local/bin/generate_dashboard.py --region europe && $VENV_PY /usr/local/bin/generate_dashboard.py --region americas && $VENV_PY /usr/local/bin/generate_backtest_dashboard.py && $VENV_PY /usr/local/bin/generate_realmoney_dashboard.py'
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
systemctl show polyweather-dashboard.service -p User
ls -l $WEB_ROOT/*.html
echo "== local check =="
curl -s -o /dev/null -w "nginx says: HTTP %{http_code}, %{size_download} bytes\n" http://localhost/
