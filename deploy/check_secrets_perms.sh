#!/usr/bin/env bash
# check_secrets_perms.sh -- gap audit 2026-09-24 item 4. The EOA private key
# (POLYMARKET_PRIVATE_KEY) lives in a systemd drop-in env file under
# /etc/systemd/system/polyweather.service.d/ (see deploy_daemon.sh's
# "execution mode" comments and generate_realmoney_dashboard.py's gate-2
# probe, ~L155). Nothing has ever checked that file's permissions.
#
# WARN LOUDLY, NEVER ABORT: a permissions problem on an already-deployed key
# is not a reason to refuse a deploy or leave the daemon down -- it is a
# reason to tell the operator so they fix it out of band. Meant to be
# sourced by deploy_daemon.sh; also runnable standalone for testing.
#
# SYSTEMD_DROPIN_DIR overrides the directory scanned (tests point this at a
# fake directory instead of /etc/systemd/system/polyweather.service.d).

check_secret_file_perm() {
    # check_secret_file_perm <path> -- prints an ok/warn line. Never fails.
    local f="$1"
    local mode owner
    mode=$(stat -c '%a' "$f" 2>/dev/null || stat -f '%Lp' "$f" 2>/dev/null || echo '?')
    owner=$(stat -c '%U' "$f" 2>/dev/null || stat -f '%Su' "$f" 2>/dev/null || echo '?')
    if [ "$mode" != "600" ] || [ "$owner" != "root" ]; then
        echo "!! SECRET FILE PERMISSIONS: $f is mode $mode owned by $owner (want 600 root:root) -- it may hold POLYMARKET_PRIVATE_KEY. Fix with: sudo chmod 600 '$f' && sudo chown root:root '$f'"
    else
        echo "-- secret file ok: $f (600 root)"
    fi
}

check_secrets_permissions() {
    local dropin_dir="${SYSTEMD_DROPIN_DIR:-/etc/systemd/system/polyweather.service.d}"
    if [ ! -d "$dropin_dir" ]; then
        echo "-- no drop-in dir at $dropin_dir -- nothing to check"
        return 0
    fi
    local f found=0
    for f in "$dropin_dir"/*.conf; do
        [ -f "$f" ] || continue
        if grep -q PRIVATE_KEY "$f" 2>/dev/null; then
            found=1
            check_secret_file_perm "$f"
        fi
        # A drop-in can point at a separate env file via EnvironmentFile=;
        # follow it if that file is the one actually holding the key.
        local env_file
        env_file=$(grep -o 'EnvironmentFile=.*' "$f" 2>/dev/null | head -n 1 | cut -d= -f2- | sed 's/^-//' || true)
        if [ -n "$env_file" ] && [ -f "$env_file" ] && grep -q PRIVATE_KEY "$env_file" 2>/dev/null; then
            found=1
            check_secret_file_perm "$env_file"
        fi
    done
    if [ "$found" -eq 0 ]; then
        echo "-- no PRIVATE_KEY found under $dropin_dir -- nothing to check (expected on a box with no live credentials, or off-box)"
    fi
    return 0
}

# Runnable standalone (not just sourced) so it can be exercised without
# running the rest of deploy_daemon.sh.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    check_secrets_permissions
fi
