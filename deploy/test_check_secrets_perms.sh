#!/usr/bin/env bash
# test_check_secrets_perms.sh -- offline self-check for check_secrets_perms.sh.
# No root, no systemd, no server. Run: bash deploy/test_check_secrets_perms.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/check_secrets_perms.sh"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*"; exit 1; }

echo "== test 1: missing drop-in dir is silent (no crash, no warn) =="
out=$(SYSTEMD_DROPIN_DIR="$WORK/does-not-exist" check_secrets_permissions)
echo "$out" | grep -q "nothing to check" || fail "expected a 'nothing to check' line"
echo "ok"

echo "== test 2: a .conf with no PRIVATE_KEY is not flagged =="
DROPIN="$WORK/dropin1"
mkdir -p "$DROPIN"
printf '[Service]\nEnvironment=FOO=bar\n' > "$DROPIN/plain.conf"
out=$(SYSTEMD_DROPIN_DIR="$DROPIN" check_secrets_permissions)
echo "$out" | grep -q "SECRET FILE PERMISSIONS" && fail "a file with no PRIVATE_KEY should not be flagged"
echo "$out" | grep -q "nothing to check" || fail "expected 'nothing to check' when no key is present"
echo "ok"

echo "== test 3: a .conf holding PRIVATE_KEY at the wrong mode warns loudly =="
DROPIN="$WORK/dropin2"
mkdir -p "$DROPIN"
printf '[Service]\nEnvironment=POLYMARKET_PRIVATE_KEY=0xdeadbeef\n' > "$DROPIN/key.conf"
chmod 644 "$DROPIN/key.conf"
out=$(SYSTEMD_DROPIN_DIR="$DROPIN" check_secrets_permissions)
echo "$out" | grep -q "!! SECRET FILE PERMISSIONS" || fail "expected a loud warning for a non-600 key file"
echo "ok"

echo "== test 4: check_secrets_permissions never returns nonzero, even on a bad file =="
SYSTEMD_DROPIN_DIR="$DROPIN" check_secrets_permissions >/dev/null
[ $? -eq 0 ] || fail "check_secrets_permissions must never fail the deploy"
echo "ok"

echo "== test 5: standalone invocation works (deploy_daemon.sh sources this same file) =="
bash "$HERE/check_secrets_perms.sh" >/dev/null || fail "standalone run should exit 0"
echo "ok"

echo "ALL PASS"
