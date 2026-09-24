#!/usr/bin/env bash
# test_pull_backup.sh -- offline self-check for pull_backup.sh's fetch/
# verify/delete/prune logic, using PULL_BACKUP_FAKE_REMOTE instead of a real
# box. No network, no server, never touches production. Run:
#   bash deploy/test_pull_backup.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

FAKE_REMOTE="$WORK/remote"
LOCAL_DIR="$WORK/local"
mkdir -p "$FAKE_REMOTE" "$LOCAL_DIR"

fail() { echo "FAIL: $*"; exit 1; }

make_set() {
    # make_set <stamp> <valid|corrupt>
    local stamp="$1" mode="$2"
    for name in polyweather market_data; do
        local gz="$FAKE_REMOTE/${name}-${stamp}.sqlite3.gz"
        echo "fake db content for $name $stamp" | gzip -1 > "$gz"
        if [ "$mode" = "valid" ]; then
            (cd "$FAKE_REMOTE" && sha256sum "$(basename "$gz")" > "$(basename "$gz").sha256")
        else
            echo "0000000000000000000000000000000000000000000000000000000000000000  $(basename "$gz")" \
                > "${gz}.sha256"
        fi
    done
}

echo "== test 1: valid set is fetched, verified, and deleted remotely =="
make_set "20260101T010000Z" valid
PULL_BACKUP_FAKE_REMOTE="$FAKE_REMOTE" bash "$HERE/pull_backup.sh" "$LOCAL_DIR" \
    || fail "pull_backup.sh exited nonzero on a valid set"
[ -f "$LOCAL_DIR/polyweather-20260101T010000Z.sqlite3.gz" ] || fail "gz not fetched locally"
[ -f "$LOCAL_DIR/polyweather-20260101T010000Z.sqlite3.gz.sha256" ] || fail "sha not fetched locally"
[ -f "$FAKE_REMOTE/polyweather-20260101T010000Z.sqlite3.gz" ] && fail "remote gz not deleted after verify"
echo "ok"

echo "== test 2: sha256 mismatch is NOT deleted remotely and exits nonzero =="
make_set "20260102T010000Z" corrupt
if PULL_BACKUP_FAKE_REMOTE="$FAKE_REMOTE" bash "$HERE/pull_backup.sh" "$LOCAL_DIR"; then
    fail "pull_backup.sh should have exited nonzero on a sha256 mismatch"
fi
[ -f "$FAKE_REMOTE/polyweather-20260102T010000Z.sqlite3.gz" ] || fail "mismatched remote file was deleted -- unsafe"
echo "ok"

echo "== test 3: retention keeps only the newest 7 local sets =="
rm -f "$FAKE_REMOTE"/*.gz "$FAKE_REMOTE"/*.sha256
rm -f "$LOCAL_DIR"/*.gz "$LOCAL_DIR"/*.sha256
for i in 1 2 3 4 5 6 7 8 9; do
    stamp=$(printf "202601%02dT010000Z" "$i")
    make_set "$stamp" valid
    PULL_BACKUP_FAKE_REMOTE="$FAKE_REMOTE" bash "$HERE/pull_backup.sh" "$LOCAL_DIR" >/dev/null
done
kept=$(find "$LOCAL_DIR" -maxdepth 1 -name '*.sqlite3.gz' | grep -oE '[0-9]{8}T[0-9]{6}Z' | sort -u | wc -l)
[ "$kept" -eq 7 ] || fail "expected 7 sets retained, found $kept"
[ -f "$LOCAL_DIR/polyweather-20260101T010000Z.sqlite3.gz" ] && fail "oldest set should have been pruned"
[ -f "$LOCAL_DIR/polyweather-20260109T010000Z.sqlite3.gz" ] || fail "newest set should have been kept"
echo "ok"

echo "ALL PASS"
