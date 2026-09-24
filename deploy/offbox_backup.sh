#!/usr/bin/env bash
# offbox_backup.sh -- run ON THE BOX. Takes an online sqlite backup of both
# trading DBs, gzips them, and leaves them in ~/offbox for pull_backup.sh
# (run from the operator's Windows PC) to fetch and delete. Gap audit
# 2026-09-24 item 3: deploy_daemon.sh's backups (~/polyweather-pre-deploy-
# *.sqlite3) live on the SAME box as the live DB, so a lost/corrupted disk
# takes both. This is the off-box half.
#
# The box has ~2.1GB free and each gzipped set is ~125MB, so this keeps AT
# MOST ONE set on the box at a time -- older sets are deleted BEFORE the new
# backup is taken, not after, so a failed run never leaves two sets fighting
# for the same headroom.
#
# Install in ubuntu's crontab (pull_backup.sh calls this remotely instead;
# a local cron entry is only needed if you also want a box-side copy on a
# schedule independent of the Windows puller):
#   30 1 * * * cd ~/weather-forecast/weather-forecast && bash ~/weather-forecast/deploy/offbox_backup.sh >> ~/offbox_backup.log 2>&1
set -euo pipefail

APP_DIR="$HOME/weather-forecast"
PKG_DIR="$APP_DIR/weather-forecast"
OUT_DIR="$HOME/offbox"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$OUT_DIR"

echo "== pruning old sets (keep at most 1 on the box) =="
rm -f "$OUT_DIR"/*.sqlite3.gz "$OUT_DIR"/*.sha256

backup_one() {
    local db_path="$1"
    local name="$2"
    if [ ! -f "$db_path" ]; then
        echo "!! $db_path does not exist -- skipping $name"
        return 0
    fi
    local raw="$OUT_DIR/${name}-${STAMP}.sqlite3"
    local gz="${raw}.gz"
    local sha="${gz}.sha256"

    echo "== backing up $name =="
    # Read-only source, same reasoning as deploy_daemon.sh's backup step:
    # the source connection must never become a second writer racing the
    # live daemon for the file.
    python3 - "$db_path" "$raw" <<'PYBACKUP'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
PYBACKUP

    gzip -1 "$raw"
    (cd "$OUT_DIR" && sha256sum "$(basename "$gz")" > "$(basename "$sha")")

    echo "$gz"
    echo "$sha"
}

backup_one "$PKG_DIR/data/polyweather.sqlite3" "polyweather"
backup_one "$PKG_DIR/data/market_data.sqlite3" "market_data"

echo "== done =="
df -h "$HOME" | tail -n 1
