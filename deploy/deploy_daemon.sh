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

# RE-EXEC AFTER THE PULL. bash reads a script INCREMENTALLY, by byte offset
# -- it does not load the whole file up front. The pull above rewrites this
# very file, so any commit that changes the number of bytes ABOVE the
# not-yet-read portion shifts every following line out from under the
# interpreter, and bash resumes mid-statement somewhere in the new content.
#
# This is not hypothetical. On the 2026-08-10 deploy the commit changed both
# the pip line and the systemd unit block; the run completed with no error,
# reported "active", and left the service on the PREVIOUS unit file --
# still `--mode paper` while the repo on disk said `--mode simulation`. A
# deploy that silently applies the old config and reports success is the
# worst possible failure mode for this script.
#
# Re-exec once, after the code is settled, so the rest of this file runs
# from a version that is no longer changing. The guard variable prevents an
# infinite loop; the second pass's pull is a no-op.
if [ -z "${POLYWEATHER_DEPLOY_REEXEC:-}" ]; then
    export POLYWEATHER_DEPLOY_REEXEC=1
    echo "== Re-exec into the pulled version of this script =="
    exec bash "$APP_DIR/deploy/deploy_daemon.sh" "$@"
fi

echo "== Python venv + deps =="
if ! python3 -m venv "$VENV" 2>/dev/null; then
    sudo apt-get update -q && sudo apt-get install -y -q python3-venv
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
# py-clob-client-v2 IS now installed: WSSS runs in "simulation" mode, which
# builds real orders (tick size, share rounding, minimum order size) against
# the real book and submits nothing. That path needs the library even though
# it never spends anything. wallet_client imports it lazily, so a box where
# this install fails degrades to a clear per-order error rather than a
# scheduler that won't start.
"$VENV/bin/pip" install --quiet requests beautifulsoup4 py-clob-client-v2

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
    sudo cp "$APP_DIR/deploy/generate_realmoney_dashboard.py" /usr/local/bin/generate_realmoney_dashboard.py
    sudo chmod 644 /usr/local/bin/generate_dashboard.py /usr/local/bin/generate_backtest_dashboard.py /usr/local/bin/generate_realmoney_dashboard.py
    sudo systemctl start polyweather-dashboard.service 2>/dev/null || true
fi

echo "== execution mode (persisted OUTSIDE this script) =="
# THE MODE IS HOST STATE, NOT REPO STATE.
#
# This script rewrites the unit file from a heredoc on every run. When the
# mode was baked into that heredoc, promoting the daemon to --mode live was
# destroyed by the next `git pull && deploy` -- and after the restart,
# executor.close_position() refuses to close every open LIVE position,
# forever, because the process is no longer authorised for live. That
# demotion fired with ZERO operator error: an ordinary deploy silently
# disarmed the daemon and stranded whatever it was holding.
#
# So the mode lives in a file this script CREATES IF ABSENT and NEVER
# OVERWRITES. Changing modes means editing that file, not this one.
MODE_FILE=/etc/polyweather/mode.env
if [ ! -f "$MODE_FILE" ]; then
    echo "-- first deploy: seeding $MODE_FILE with the safe default"
    sudo mkdir -p "$(dirname "$MODE_FILE")"
    sudo tee "$MODE_FILE" >/dev/null <<MODEENV
# Execution mode for the polyweather daemon. Host state: deploy_daemon.sh
# creates this file once and never rewrites it, so a promotion survives
# deploys. systemd expands these into ExecStart.
#
# POLYWEATHER_MODE:          manual_review | paper | simulation | live
# POLYWEATHER_FALLBACK_MODE: manual_review | paper  (stations not eligible
#                            for simulation/live)
#
# "live" ALSO requires POLYWEATHER_LIVE_ARGS below to carry the explicit
# acknowledgement flag, and POLYMARKET_LIVE_TRADING=true from the drop-in at
# polyweather.service.d/. Three separate places, on purpose.
POLYWEATHER_MODE=simulation
POLYWEATHER_FALLBACK_MODE=paper
POLYWEATHER_LIVE_ARGS=
MODEENV
fi
sudo chmod 644 "$MODE_FILE"
CURRENT_MODE=$(sudo grep -E '^POLYWEATHER_MODE=' "$MODE_FILE" | cut -d= -f2)
echo "-- mode: ${CURRENT_MODE:-unset}"

echo "== refusing to demote a daemon holding live positions =="
# The other half of the same failure: even with the mode persisted, a
# deliberate demotion while real shares are held strands them. Refuse, and
# say what is at stake. POLYWEATHER_ALLOW_DEMOTION=1 overrides for the case
# where the deploy IS the fix.
if [ "$CURRENT_MODE" != "live" ] && [ -f "$PKG_DIR/data/polyweather.sqlite3" ]; then
    OPEN_LIVE=$("$VENV/bin/python" - <<'PYCHECK' || echo "ERROR"
import sqlite3, sys
try:
    c = sqlite3.connect("data/polyweather.sqlite3")
    n, = c.execute(
        "SELECT COUNT(*) FROM positions WHERE status='open' AND execution_mode='live'"
    ).fetchone()
    print(n)
except Exception:
    print("ERROR")
PYCHECK
)
    if [ "$OPEN_LIVE" = "ERROR" ]; then
        echo "!! could not read open live positions -- continuing, but verify by hand"
    elif [ "${OPEN_LIVE:-0}" -gt 0 ]; then
        if [ "${POLYWEATHER_ALLOW_DEMOTION:-}" = "1" ]; then
            echo "!! $OPEN_LIVE open LIVE position(s) and mode is '$CURRENT_MODE' -- "
            echo "!! proceeding because POLYWEATHER_ALLOW_DEMOTION=1. They stay unmanageable."
        else
            echo "REFUSING TO DEPLOY: $OPEN_LIVE open LIVE position(s) exist and the mode is"
            echo "'$CURRENT_MODE', not 'live'. Restarting now would leave real shares that this"
            echo "daemon cannot close -- executor.close_position() will refuse them every cycle."
            echo
            echo "Resolve them (redeem/sell on the exchange and close the rows), or set the mode"
            echo "to live in $MODE_FILE, or re-run with POLYWEATHER_ALLOW_DEMOTION=1 if you"
            echo "accept leaving them stranded."
            exit 1
        fi
    fi
fi

echo "== systemd service =="
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=polyweather scheduler (mode from /etc/polyweather/mode.env)
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=$PKG_DIR
# The mode comes from /etc/polyweather/mode.env, which this deploy script
# creates once and never rewrites -- see the "execution mode" step above for
# why baking it in here was a silent-demotion bug. \${VAR} form so each
# expands to exactly one argument.
#
# --mode simulation promotes ONLY the stations config.live_mode_is_permitted()
# allows (today: WSSS alone) onto the real order path; --fallback-mode keeps
# the rest where they were. Nothing here submits an order: that additionally
# requires mode=live, the acknowledgement flag in POLYWEATHER_LIVE_ARGS, AND
# POLYMARKET_LIVE_TRADING=true from the polyweather.service.d/ drop-in.
EnvironmentFile=/etc/polyweather/mode.env
ExecStart=$VENV/bin/python scheduler.py --mode \${POLYWEATHER_MODE} --fallback-mode \${POLYWEATHER_FALLBACK_MODE} \$POLYWEATHER_LIVE_ARGS
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
