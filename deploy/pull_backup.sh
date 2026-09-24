#!/usr/bin/env bash
# pull_backup.sh -- run FROM WINDOWS (Git Bash), via PuTTY plink/pscp.
# Triggers offbox_backup.sh on the box, pulls the resulting gz+sha256 files
# down, verifies each sha256 LOCALLY, deletes the box's copies only after
# verification, and prunes local history to the newest 7 sets. Gap audit
# 2026-09-24 item 3(b) -- the box only ever keeps one set (it has ~2.1GB
# free); the real off-box copy lives here.
#
# A "set" is one UTC timestamp: polyweather-<stamp>.sqlite3.gz +
# market_data-<stamp>.sqlite3.gz, each with a .sha256 sidecar.
#
# Usage:
#   deploy/pull_backup.sh [local_dir]
#   BACKUP_LOCAL_DIR=... deploy/pull_backup.sh
# Default local_dir: C:/Users/user/polyweather-backups
#
# pscp fetches ONE remote source per call, so each file is pulled with its
# own pscp invocation -- there is no batch/glob form here.
#
# Windows Task Scheduler entry to run this daily: documented in
# weather-forecast/docs/runbooks/operations.md. This script does NOT
# install that task and is not run against the server by this commit.
#
# PULL_BACKUP_FAKE_REMOTE=<dir> replaces plink/pscp with a local directory
# standing in for the box's ~/offbox -- lets the fetch/verify/delete/prune
# logic be tested offline, with no network and no server. Never set this in
# production use. See test_pull_backup.sh.
set -euo pipefail

PLINK="/c/Program Files/PuTTY/plink.exe"
PSCP="/c/Program Files/PuTTY/pscp.exe"
KEY="C:\\Users\\user\\Downloads\\multicityweatherbot.ppk"
HOST="ubuntu@43.216.25.99"
REMOTE_SCRIPT="bash ~/weather-forecast/deploy/offbox_backup.sh"
KEEP=7

LOCAL_DIR="${1:-${BACKUP_LOCAL_DIR:-C:/Users/user/polyweather-backups}}"
mkdir -p "$LOCAL_DIR"
LOG="$LOCAL_DIR/pull_backup.log"
FAKE_REMOTE="${PULL_BACKUP_FAKE_REMOTE:-}"
FAILED=0

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$LOG"; }

remote_list_files() {
    # Prints one remote path per line (both .gz and .sha256). The grep is
    # wrapped so a legitimate zero-match result (nothing produced) does not
    # trip `pipefail` and abort the script -- callers check for an empty
    # result themselves.
    if [ -n "$FAKE_REMOTE" ]; then
        find "$FAKE_REMOTE" -maxdepth 1 -type f \( -name '*.gz' -o -name '*.sha256' \)
    else
        "$PLINK" -batch -ssh -i "$KEY" "$HOST" "$REMOTE_SCRIPT" | { grep -E '\.(gz|sha256)$' || true; }
    fi
}

fetch_remote_file() {
    local remote="$1" local_path="$2"
    if [ -n "$FAKE_REMOTE" ]; then
        cp "$remote" "$local_path"
    else
        "$PSCP" -batch -i "$KEY" "$HOST:$remote" "$local_path"
    fi
}

delete_remote_file() {
    local remote="$1"
    if [ -n "$FAKE_REMOTE" ]; then
        rm -f "$remote"
    else
        "$PLINK" -batch -ssh -i "$KEY" "$HOST" "rm -f '$remote'"
    fi
}

log "== triggering offbox_backup.sh and listing produced files =="
REMOTE_LIST=$(remote_list_files) || { log "!! could not list remote backup files"; exit 1; }
if [ -z "$REMOTE_LIST" ]; then
    log "!! offbox_backup.sh produced no .gz/.sha256 files"
    exit 1
fi
echo "$REMOTE_LIST" | tee -a "$LOG" >/dev/null

while IFS= read -r remote_gz; do
    [[ "$remote_gz" == *.gz ]] || continue
    base=$(basename "$remote_gz")
    gz_local="$LOCAL_DIR/$base"
    sha_remote="${remote_gz}.sha256"
    sha_local="${gz_local}.sha256"

    log "-- fetching $base"
    if ! fetch_remote_file "$remote_gz" "$gz_local"; then
        log "!! failed to fetch $remote_gz"; FAILED=1; continue
    fi
    if ! fetch_remote_file "$sha_remote" "$sha_local"; then
        log "!! failed to fetch $sha_remote"; FAILED=1; continue
    fi

    log "-- verifying sha256 for $base"
    expected=$(cut -d' ' -f1 "$sha_local")
    actual=$(sha256sum "$gz_local" | cut -d' ' -f1)
    if [ "$expected" != "$actual" ]; then
        log "!! SHA256 MISMATCH for $base: expected $expected got $actual -- NOT deleting remote copy"
        FAILED=1
        continue
    fi
    log "-- verified ok: $base"

    log "-- deleting remote copies of $base"
    delete_remote_file "$remote_gz" || { log "!! failed to delete remote $remote_gz"; FAILED=1; }
    delete_remote_file "$sha_remote" || { log "!! failed to delete remote $sha_remote"; FAILED=1; }
done <<< "$REMOTE_LIST"

log "== pruning local history to newest $KEEP sets =="
# A "set" is identified by its UTC stamp; group files by stamp, keep the
# newest KEEP stamps (stamps sort lexically = chronologically), delete the
# rest.
stamps=$(find "$LOCAL_DIR" -maxdepth 1 -type f -name '*.sqlite3.gz*' \
    | { grep -oE '[0-9]{8}T[0-9]{6}Z' || true; } | sort -u)
n=$(echo "$stamps" | grep -c . || true)
if [ "$n" -gt "$KEEP" ]; then
    to_delete=$(echo "$stamps" | head -n $((n - KEEP)))
    for stamp in $to_delete; do
        find "$LOCAL_DIR" -maxdepth 1 -type f -name "*${stamp}*" -delete
        log "-- pruned local set $stamp"
    done
fi

if [ "$FAILED" -ne 0 ]; then
    log "!! pull_backup.sh finished with failures"
    exit 1
fi
log "== pull_backup.sh ok =="
