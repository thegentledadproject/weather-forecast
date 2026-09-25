# Handoff - 2026-09-25

## State
Branch `main`, pushed; only HANDOFF.md dirty. Last commit: `b67ac73` Dashboard Phase 1: attention panel, exact P&L sums, honest CLOB label.
- **Box (`ubuntu@43.216.25.99`) runs `c91ff66`** (= `72746e8` + a HANDOFF commit): the pre-live safety fixes (`244a065`) plus the CYYZ/KHOU release. **Plus a dashboard-only deploy (2026-09-25 05:44 UTC):** the 5 files of `b67ac73` are STAGED (not committed) on the box's `main`. Their contents match `b67ac73` exactly, so the 16:05 `git pull --ff-only` fast-forwards over them (simulated locally first). Generators in `/usr/local/bin` md5-match. Daemon was not restarted. **Daemon is PAPER** via `/etc/polyweather/mode.env` (since 2026-09-23 15:33 UTC). Wallet ~$0.26.
- **Main is ahead of the box by the whole audit remediation (`41a88cc`, 2130 tests, verified).** A one-time Claude scheduled task `deploy-audit-remediation` deploys it at **2026-09-25 16:05 UTC**. It runs only while the Claude app is open, checks the schema, contract gate and watchdog, then sends ntfy "polyweather deploy OK/FAILED".
- Live now needs measured maturity AND `config.REDEMPTION_PROVEN_TX` (None). `MATURITY_OVERRIDE` is empty, so restoring `mode.env` alone does not arm anything.
- Alerts: ntfy topic `poly-alex` (systemd drop-in `/etc/systemd/system/polyweather.service.d/ntfy.conf`). ubuntu crontab runs `watchdog.py` every 30 min, logging to `~/watchdog.log`.
- Windows task `PolyweatherPullBackup` runs daily at 01:30 local (17:30 UTC) → `C:\Users\user\polyweather-backups` (keeps 7). **First run 2026-09-26 01:30 local.** It needs `deploy/offbox_backup.sh`, which only reaches the box with tonight's deploy.

## Done this session (2026-09-24 → 2026-09-25)
- The Codex P0-P12 plan was audited against the repo, and all 5 gates FAIL: `weather-forecast/docs/validation/2026-09-24-plan-gap-audit.md` (`9161d6e`). Memory: `plan-gap-audit-2026-09-24.md` holds every result below.
- **Gaps 1/2/6, `244a065`, DEPLOYED 2026-09-24:**
  - Re-arm gate.
  - `redeem.py` sends a `[yes,no]` amounts array.
  - `storage.close_position` guards `status='open'`.
  - `SETTLEMENT_MISMATCH` flag.
  - `executor._live_brake`: $4 daily loss / kill_criterion / stranded >2d, and it fails closed.
  - `alerts.py` and `watchdog.py`.
- **Merged to main, NOT yet deployed (`41a88cc`):**
  - Gap 3, `backtest/as_of.py`, `storage.set_as_of`, `config.pin_now_utc`: the replay runs production readers point-in-time.
  - Gap 4, `ADMIT_ON_ROBUST_EDGE`, `probability_calibration.shrink_for_day`:
    - P_robust = ask + λ_robust·(p − ask), with each side anchored on its own raw ask.
    - λ_robust = 0 every day since 09-08, so **paper entries go to ~0/day** (user's choice).
  - Gap 5: `lock_score.py` and `docs/validation/2026-09-24-forward-lock-prereg.md` (honor system).
  - Gap 7: `contract_rules.py` and the `contract_checks` table. Only VALID contracts may enter.
  - Gap 8: `*_history` trigger tables; entry_decisions gains `mu_c, sd_c, bias_c, spread_source, forecast_fetched_at`.
  - Honest fills: one `/book` per entry, stale >300s / crossed refused, paper VWAP price, unknown-fill reconciliation.
  - Ledger: `edge_ledger.py`, `config_fingerprint`, `live_order_attempts.client_order_key` UNIQUE.
  - Ops: `deploy/{offbox_backup,pull_backup}.sh`, `deploy/restore_rehearsal.py`, `weather-forecast/docs/runbooks/operations.md`.
- **Dashboard Phase 1** (plan: `C:\Users\user\Documents\Codex\weather-forecast\docs\superpowers\plans\2026-09-25-dashboard-improvement-plan.md`), `b67ac73`, 2135 tests pass, DEPLOYED dashboard-only:
  - "Needs attention" card: `calibration_panel.render_attention_html` / `attention_inputs`, which reads `executor._live_brake()` and `kill_criterion`. Anything unreadable shows UNKNOWN.
  - Summaries gain `paper_trading_report.summarize_positions` → `total_pnl_usd_exact`. The tile and calendar now agree: both -$257.05 live.
  - The kill criterion is ENFORCED (it blocks new live entries). Both cohort captions used to say "implies no action".
  - CLOB probe labelled reachability-only.
  - Live read, 05:44 UTC: kill criterion FIRED, -0.0019 vs 0 over 379 station-days → live entries REFUSED.
- **Findings** (snapshot, in-sample):
  - Brier: market 0.612 < model 0.733 < 30-day climatology 0.874.
  - Predicted edge does NOT predict realized edge (slope +0.07, CI ±0.6).
  - Asian markets (except RCSS and VHHH) now settle on NOAA timeseries, not Wunderground.
  - ZGSZ 2026-08-23 bucket 33 NO was booked 1.0 but lost.

## Open / next step
1. **After the 16:05 UTC deploy:**
   - Confirm the ntfy message arrived. If the task didn't run (app closed), deploy by hand with the same steps: `C:\Users\user\.claude\scheduled-tasks\deploy-audit-remediation\SKILL.md`.
   - Confirm `contract_checks` shows 35 VALID.
   - Paper entries ~0/day is expected, not a bug.
2. **2026-09-26:** check `C:\Users\user\polyweather-backups\pull_backup.log` shows `pull_backup.sh ok`, or run `Get-ScheduledTaskInfo PolyweatherPullBackup`.
3. **~2026-10-04 checkpoint:**
   - `wave2_falsifier.py` on the box. Its stop rule: CI upper of (after − before) < 0 → set `ERROR_SAMPLE_FETCH_WINDOW_ENABLED` and `SPREAD_FLOOR_MEASURED_TIERS_EXEMPT` to False.
   - Re-run `cohort_monitor.py --by region --since 2026-09-03` and `--by station`.
   - Judge on paper/live rows, not replay.
4. **2026-10-05:** set `LOCK_SHA` in `weather-forecast/lock_score.py` to the newest `ev_snapshots.config_sha` on the box (the full `config_fingerprint`), commit it, and deploy. **No deploys 2026-10-06 → 11-02** (restarts OK). Read the exam on 2026-11-05: `python lock_score.py --locked-read`.
5. **Redemption is still unproven.** The `[yes,no]` slot order is unverified on-chain. It needs a real winning live position plus POL for gas; then set `config.REDEMPTION_PROVEN_TX`.
6. **Unscored:** NOAA daily max vs our METAR daily max for the NOAA-settled stations (see `config.py` ~671 note).
7. **DST restarts** (not deploys): after 2026-10-25 01:00 UTC (EGLC) and after 2026-11-01 06:00 UTC (KLGA).
8. **Kept from the 2026-09-24 handoff:** EDDM/ZSPD/RKPK collection-only → answered NO. SBGR/KSEA/KMIA/MMMX stay force-collection-only.

9. **Dashboard Phases 2-4** (reorder the page, collapse history, phone layout) are not started. Also skipped from Phase 1: per-feed data age and a single-snapshot read, because no timestamps for them are stored yet.

## Gotchas
- **Box has 5 STAGED files** (see State). If a commit touching `deploy/generate_*dashboard.py`, `calibration_panel.py`, `paper_trading_report.py` or `tests/test_dashboard_attention.py` lands on main before the next box pull, the pull fails with "local changes would be overwritten". Fix on the box: `git reset -q HEAD -- . && git checkout -- . && rm weather-forecast/tests/test_dashboard_attention.py`, then pull.
- Memory note `dashboard-phase1-deploy.md` still says to run `git checkout main` on the box. That's stale but harmless (the box is already on main); the fix was blocked by a permission check.
- SSH is PuTTY plink: `"/c/Program Files/PuTTY/plink.exe" -batch -ssh -i "C:\Users\user\Downloads\multicityweatherbot.ppk" ubuntu@43.216.25.99 "<cmd>"`. pscp takes ONE remote source per call.
- Box: code in `~/weather-forecast/weather-forecast/`, venv `~/weather-forecast/.venv/bin/python`. Deploy only 16:00–19:00 UTC with `~/deploy.sh`; `!! DEPLOY ABORTED` = daemon STOPPED (recovery: Wave 3 plan Task 8 step 4).
- Never run pytest on the box. Locally: `cd weather-forecast && timeout 540 python -m pytest -q -p no:cacheprovider`, in the FOREGROUND (~5 min).
- A live token with an `outcome='unknown'` order stays blocked until an operator appends an `outcome='reconciled'` row to `live_order_attempts`. No script does this.
- Seven+ parallel Opus subagents hit the session rate limit twice; run ≤3 at a time.
- 9 agent worktrees remain under `.claude/worktrees/`; all are merged. Remove them with `git worktree remove` or the app's cleanup.
- Another session shares this checkout and commits to main. Fetch and check `git log` before merging.
