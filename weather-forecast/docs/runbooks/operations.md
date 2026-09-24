# Operations runbook

Plain, command-first. Every command below is sourced from the repo or
`HANDOFF.md`; anything not directly verified in code is marked **UNVERIFIED**.

SSH is PuTTY plink, not OpenSSH, everywhere below:

```
"/c/Program Files/PuTTY/plink.exe" -batch -ssh -i "C:\Users\user\Downloads\multicityweatherbot.ppk" ubuntu@43.216.25.99 "<cmd>"
```

Box layout: code at `~/weather-forecast/weather-forecast/`, venv at
`~/weather-forecast/.venv/bin/python`.

---

## 1. Kill switch (stop live trading fast)

Flip `/etc/polyweather/mode.env` to paper and restart. This is HOST state,
not repo state — `deploy_daemon.sh` creates the file once and never
rewrites it, so editing it survives the next deploy (`HANDOFF.md`, gotchas).

```
sudo sed -i 's/^POLYWEATHER_MODE=.*/POLYWEATHER_MODE=paper/' /etc/polyweather/mode.env
sudo systemctl restart polyweather
systemctl is-active polyweather
```

Do **not** empty `config.LIVE_TRADING_STATIONS` to disarm live — 51 tests use
WSSS as their live fixture (`HANDOFF.md`). Use `mode.env`.

Keep a timestamped backup before editing, matching the pattern already used
on the box (`mode.env.bak-20260923`, per `HANDOFF.md`):

```
sudo cp /etc/polyweather/mode.env /etc/polyweather/mode.env.bak-$(date -u +%Y%m%d)
```

### Automatic live brakes (`executor._live_brake`, `executor.py:264`)

Checked on every live entry attempt (never on exits). Fails closed: if the
check itself errors, new live entries are refused. Alerts fire once per
`(code, day)` via `alerts.send(..., priority="high")`
(`executor._alert_brake_once`, `executor.py:314`):

| Alert code | Meaning | Config |
|---|---|---|
| `daily_loss` | realised live P&L over the trailing 24h is at or below `-LIVE_DAILY_LOSS_LIMIT_USD` ($4.00) | `config.py:3056` |
| `kill_criterion` | `cohort_monitor.kill_criterion()` fired (net price edge / window criteria) | `config.COHORT_KILL_*`, `config.py:1784-1808` |
| `stranded` | a live position is still open more than `LIVE_STRANDED_AFTER_DAYS` (2) past its target date | `config.py:3057` |
| `error` | the brake check itself raised — failing closed, new live entries refused until fixed | n/a |

A brake only blocks **new** live entries; it does not close existing
positions. If one fires, treat it as a signal to also consider the manual
kill switch above.

### Watchdog alerts (`watchdog.py`, cron every 30 min)

| Alert | Meaning |
|---|---|
| `polyweather watchdog: daemon silent` | no `forecasts.fetched_at` write in the last `STALE_AFTER` (3h) while at least one station group should have been open the whole span — the daemon is likely dead or stuck. Check: `systemctl status polyweather` |
| `polyweather watchdog: DB unreadable` | `storage.latest_cycle_write_ts()` raised — the DB file itself is the problem |
| `polyweather watchdog: clock drift` | `timedatectl`/`chronyc` reports the box is not NTP-synchronized, or offset exceeds 2s — added this branch (item 1). A log line of `clock unknown` (tools missing) is expected on a dev box and is not an alert. |

---

## 2. Re-arm live

**Gate, as of the 2026-09-24 gap-audit fix (`config.live_mode_is_permitted`,
`config.py:4207`):** live additionally requires
`config.REDEMPTION_PROVEN_TX` to be set (the tx hash of one real, successful
redemption) — it defaults to `None`, and `live_mode_is_permitted` returns
`False` for `execution_mode="live"` while it is unset, regardless of
`mode.env`. `MATURITY_OVERRIDE` is now an empty dict (`config.py:4693`); the
station must pass the measured maturity criteria on its own.

Checklist before restoring `mode.env` to live:

1. **Promotion criteria pass.** `config.maturity_report(station_icao)` must
   return `mature: True`. Six criteria, all must pass (`config.py` around
   `4977-5016`): `observations`, `bias_pairs`, `bias_precision`,
   `bias_stability`, `order_path`, `beats_market`.
2. **Redemption proven.** `config.REDEMPTION_PROVEN_TX` must be a real tx
   hash, set by hand after one successful on-chain redemption — not by
   editing `mode.env`. As of `HANDOFF.md` (2026-09-24) this has never
   happened; `redeem.py`'s amounts-array shape against the contract ABI is
   still an open question (`redeem_abi_probe.py`).
3. **No stranded live positions.**
   ```
   .venv/bin/python -c "import storage; print([p.position_id for p in storage.load_open_positions(is_paper=False) if getattr(p, 'execution_mode', '') == 'live'])"
   ```
   Resolve or redeem anything listed before re-arming.
4. Restore the mode backup and restart:
   ```
   sudo cp /etc/polyweather/mode.env.bak-<stamp> /etc/polyweather/mode.env
   sudo systemctl restart polyweather
   ```
5. Watch the first live approval against the current calibration map before
   trusting it (per `HANDOFF.md` open item 1).

---

## 3. Deploy

**Window: 16:00–19:00 UTC only** — no region's entry window is open in that
gap (`HANDOFF.md`, gotchas; Wave 3 plan Task 8 step 2).

```
"/c/Program Files/PuTTY/plink.exe" -batch -ssh -i "C:\Users\user\Downloads\multicityweatherbot.ppk" ubuntu@43.216.25.99 "~/deploy.sh"
```

`~/deploy.sh` is a shim to `deploy/deploy_daemon.sh`. It stops the daemon,
takes an sqlite online backup, runs `storage.migrate()`, restarts, then
starts the dashboard timer. Expected output order: `== stop ==`, `== backup
==` (`backup written: /home/ubuntu/polyweather-pre-deploy-<stamp>.sqlite3`),
`== migrate ==` (`migrate: ok -- ... tables ...`), restart, `== dashboard
==`, then `active`.

### `!! DEPLOY ABORTED` recovery

(Wave 3 plan, Task 8 step 4 —
`weather-forecast/docs/superpowers/plans/2026-09-19-wave3-stop-the-corruption.md`)

The daemon and dashboard timer are stopped when this prints; a failing
`migrate()` will fail again on a straight re-run, and restarting the OLD
code against a half-migrated DB is worse than staying down
(`deploy_daemon.sh` comments, ~L201-210).

- If the failure was at `== migrate ==`: roll back the code and restart on
  the pre-merge commit (which applies the same DDL itself), then confirm:
  ```
  git -C ~/weather-forecast checkout <pre-merge sha>
  sudo systemctl start polyweather polyweather-dashboard.timer
  systemctl is-active polyweather
  ```
- Restore the `~/polyweather-pre-deploy-<stamp>.sqlite3` backup only if the
  DB itself is unreadable (see restore procedure below).

---

## 4. Restore (rehearse before you trust a backup)

Never restore directly into the live DB path. Rehearse first, into a throwaway
temp copy, using `deploy/restore_rehearsal.py` (item 2 of this hardening
pass):

```
python deploy/restore_rehearsal.py <backup_file.sqlite3>
```

It copies the backup to a fresh temp path, runs `PRAGMA integrity_check`,
runs `storage.migrate()` against the *copy only*, and prints row counts and
newest timestamps for the key tables (`forecasts`, `observations`,
`positions`, `entry_decisions`, `settled_buckets`). Exit 0 only if every
step succeeds. Measured locally against a 162MB production snapshot copy:
~4s total, integrity_check ok, migrate ok, all tables readable.

Only after a successful rehearsal, and only if you intend to actually
replace the live DB, copy the rehearsed file over
`~/weather-forecast/weather-forecast/data/polyweather.sqlite3` with the
daemon stopped (`sudo systemctl stop polyweather` first).

`deploy_daemon.sh` keeps the 3 most recent pre-deploy backups at
`$HOME/polyweather-pre-deploy-*.sqlite3` (its `== backup ==` step,
~L212-238). For off-box copies, see the next section.

---

## 5. Off-box backup (pulled to the operator's Windows PC)

The box has ~2.1GB free, so it cannot hold backup history — backups are
pulled to the operator's PC instead of pushed to cloud storage.

- `deploy/offbox_backup.sh` (runs on the box): online sqlite backup of
  `data/polyweather.sqlite3` and `data/market_data.sqlite3`, gzip, sha256
  sidecar, into `~/offbox/`. Keeps at most 1 set on the box.
- `deploy/pull_backup.sh` (runs from Windows Git Bash, via plink/pscp):
  triggers the above remotely, pulls the files down, verifies sha256
  locally, deletes the box's copies only after verification, keeps the
  newest 7 local sets. Logs to `<local_dir>/pull_backup.log`. Default local
  dir: `C:/Users/user/polyweather-backups`.

Manual run:
```
bash deploy/pull_backup.sh
```

**Scheduled run — UNVERIFIED / not installed by this commit.** To run it
daily at 01:30 local time (17:30 UTC) via Windows Task Scheduler, with
"run as soon as possible after a missed start" enabled:

```
schtasks /Create /TN "PolyweatherPullBackup" /TR "\"C:\Program Files\Git\bin\bash.exe\" -lc \"cd /c/Users/user/Downloads/weather-forecast && deploy/pull_backup.sh\"" /SC DAILY /ST 01:30 /RL LIMITED /Z
```

`/Z` (StartWhenAvailable) is the "run as soon as possible after a missed
start" flag for `schtasks /Create`; verify the Git Bash path
(`C:\Program Files\Git\bin\bash.exe`) matches the actual install before
using this. **This task is not created by this commit** — the operator
creates it, and runs the first `pull_backup.sh` against the real server,
by hand.

Offline test (no network, no server) for the fetch/verify/retention logic:
```
bash deploy/test_pull_backup.sh
```

---

## 6. DST restarts

The scheduler's station-offset groups are computed once, at boot
(`HANDOFF.md`; Wave 3 plan 3d). Restart the daemon after each of these,
or the affected station keeps trading on its pre-DST offset:

- **EGLC** (UK, BST ends): after **2026-10-25 01:00 UTC**
- **KLGA** (US, EDT ends): after **2026-11-01 06:00 UTC**

```
sudo systemctl restart polyweather
```

---

## 7. Watchdog + alerting

Watchdog runs from `ubuntu`'s crontab every 30 minutes (install line is in
`watchdog.py`'s module docstring, not installed automatically):

```
*/30 * * * * cd ~/weather-forecast/weather-forecast && NTFY_TOPIC=<topic> ~/weather-forecast/.venv/bin/python watchdog.py >> ~/watchdog.log 2>&1
```

Alerts go out via `ntfy.sh` (`alerts.py`); the topic comes from the
`NTFY_TOPIC` environment variable, and is a no-op (not an error) if unset.

**Topic and drop-in path — UNVERIFIED, not found in the repo; taken from
the task description as given:**
- ntfy topic: `poly-alex`
- daemon-side systemd drop-in:
  `/etc/systemd/system/polyweather.service.d/ntfy.conf`

Confirm the actual topic and drop-in contents on the box before relying on
this section — `alerts.py` only reads `NTFY_TOPIC` from the environment; it
does not hardcode a topic name, and nothing in this repo names
`poly-alex` or `ntfy.conf`.

See section 1 for what each alert code means.
