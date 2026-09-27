# Handoff - 2026-09-28

## State
Branch `main`, clean, level with `origin/main`. Last commit: `c18f061` HANDOFF: NOAA vs METAR truth check answered (99.5% match).

**Box** (`ubuntu@43.216.25.99`), checked 2026-09-27 16:15 UTC:
- **Code:** runs `b67ac73`. Main is ahead only by docs plus `9ceddbd`, which is `pull_backup.sh` and runs on this PC, so there is nothing to deploy.
- **Daemon:** active since 2026-09-27 15:34 UTC, NRestarts=0, 0 tracebacks in 24h.
- **Mode:** PAPER (`--mode paper --fallback-mode paper`), 3 open paper positions, 0 live.
- **Repo:** git is clean except untracked `weather-forecast/data/`. The 5 staged dashboard files are gone.
- **Watchdog:** `~/watchdog.log` says ok every 30 min.
- **Contract checks:** 23 stations VALID in the last day. That is fewer than 35 only because checks began at the 15:34 deploy.
- **Entries:** approvals ~1-10/day, falling as intended (`ADMIT_ON_ROBUST_EDGE`). Top refusals on 09-27: `collection_gate` 262, `0a2` 180, `00c` 51.
- **Backups:** `C:\Users\user\polyweather-backups\pull_backup.log` ends `pull_backup.sh ok` (2026-09-27 15:38 UTC, a manual run). No scheduled run is confirmed yet.
- **Re-checked 2026-09-27 16:24 UTC:**
  - Same code, and the daemon is healthy: 0 tracebacks in 24h, watchdog ok. Still PAPER, with 3 open paper positions.
  - Approvals per day since 09-21 were 48, 10, 9, 10, 3, 7, and 1 by 16:24 on 09-27.
  - `contract_checks` still showed 23 VALID.
  - 20 positions settled with target date 09-24..26: 1 won and 19 lost, −$79.44 on $103.35.
    - Mostly cheap YES tickets at a 0.04-0.25 ask, which should win ~1 in 5.
    - 1 or fewer wins out of ~17 at p≈0.2 happens about 12% of the time, so this is a watch item, not a defect.
  - The scheduled backup task last ran 23:37 local with result 0. Its next run is 01:30 local on 09-28.
- **Box disk:** 71% used, 2.0G free, after deleting the two 2026-09-24 pre-deploy backups (~310MB) on 2026-09-27.
  - The DB is 715MB. The only box-side backup left is `~/polyweather-pre-deploy-20260927T153454Z.sqlite3` (176MB).
  - Every deploy adds another ~180MB backup, and nothing prunes them. Delete old ones by hand after the freeze.

## Done this session (2026-09-28)
- **Memory index trimmed:**
  - `MEMORY.md` went from 25.7KB to 8.1KB; each entry is now one line under 200 chars.
  - Every old full index line was first appended to its topic file under `## Index summary (moved from MEMORY.md, 2026-09-28)`, so nothing was lost.
  - Old index backup: `scratchpad/MEMORY.md.bak` (session scratch, may vanish).
- **Worktrees:** removed all 9 merged agent worktrees plus an empty leftover dir. `.claude/worktrees/` is empty.
- **Branches:** deleted 44 local branches merged into `origin/main` (`git branch -d`, local only).

## Open / next step
1. **Confirm the scheduled backup ran.** Check `pull_backup.log` for a run at 01:30 local (17:30 UTC) after 2026-09-27, or run `Get-ScheduledTaskInfo PolyweatherPullBackup`.
2. **Re-check `contract_checks`** after every region's window has run. It should reach 35 VALID:
   `select status, count(distinct station_icao) from contract_checks where checked_at >= datetime('now','-1 day') group by 1`
3. **~2026-10-04 checkpoint:**
   - `wave2_falsifier.py` on the box. Stop rule: CI upper of (after − before) < 0 → set `ERROR_SAMPLE_FETCH_WINDOW_ENABLED` and `SPREAD_FLOOR_MEASURED_TIERS_EXEMPT` to False.
   - `cohort_monitor.py --by region --since 2026-09-03` and `--by station`.
   - Re-run `calibration_shrink_replay.py` and `calibration_side_replay.py`.
4. **2026-10-05:**
   - Set `LOCK_SHA` in `weather-forecast/lock_score.py` to the newest `ev_snapshots.config_sha` on the box, commit it, and deploy.
   - **No deploys 2026-10-06 → 11-02** (restarts OK).
   - On 2026-11-05 read `python lock_score.py --locked-read`.
5. **Redemption is unproven.** It needs one real winning live position plus POL gas; then set `config.REDEMPTION_PROVEN_TX`. Live also requires measured maturity, because `MATURITY_OVERRIDE` is empty.
6. **DST restarts:** after 2026-10-25 01:00 UTC and after 2026-11-01 06:00 UTC.
7. **Three unmerged local branches were kept.** Decide whether to merge or drop:
   - `fix/observation-lookback-window`
   - `fix/settled-bucket-resolution-fallback`
   - `spec/recency-weighted-observed-term`
8. **Not started:** dashboard Phases 2-4.

## Gotchas
- **SSH:** `"/c/Program Files/PuTTY/plink.exe" -batch -ssh -i "C:\Users\user\Downloads\multicityweatherbot.ppk" ubuntu@43.216.25.99 "<cmd>"`. pscp takes one remote source per call.
- **Box layout:** code is in `~/weather-forecast/weather-forecast/`, venv at `~/weather-forecast/.venv/bin/python`.
- **Reading the DB:** use `sqlite3.connect("file:data/polyweather.sqlite3?mode=ro", uri=True)`.
  - `positions` has no `pnl_usd` column; `contract_checks` uses `status`, not `verdict`.
- **Deploys:** only 16:00–19:00 UTC, with `~/deploy.sh`. `!! DEPLOY ABORTED` means the daemon is STOPPED.
- **Tests:** never run pytest on the box. Locally: `cd weather-forecast && timeout 540 python -m pytest -q -p no:cacheprovider`, in the foreground.
- **Shared checkout:** another session also commits to main here. Fetch and check `git log` before merging.
- **Stale memory note:** `dashboard-phase1-deploy.md` still says to run "checkout main" on the box. That's stale; the box is on main.
