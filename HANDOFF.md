# Handoff - 2026-10-22

## State
- **Branch `main`**, last commit `2bcf38f` (Merge fix/mm-replay-fill-rule: book-through fills only).
  - Local `main` is 13 ahead of `origin/main` (`7323eec`). **Not pushed: push only after the 2026-11-05 read.**
  - Dirty: `HANDOFF.md`.
  - Untracked: `2026-09-28-*.md` (3 files), `2026-10-22-revamp-implementation-plan-redline.md`, `skill_all.py`, `skill_all_20261017.txt`, `skill_report.py`, `vhhh_skill_report_20261017.txt`.
- **No code changed this session.** Every analysis was read-only on local copies of the databases.
- **Box** (`ubuntu@43.216.25.99`): **FROZEN until 2026-11-03.** It runs `7323eec`, paper only, and the lock stamp is intact. Do not touch it.
- **Unmerged local branches:** `feat/vhhh-fresh-bias` (deploy after 11-05), `fix/mm-replay-fill-rule` (already in main), `analysis/source-mix-replay`, `fix/observation-lookback-window`, `fix/settled-bucket-resolution-fallback`, `spec/recency-weighted-observed-term`.

## Done 2026-10-22
- **Reviewed @AlexL's revamp doc and implementation plan.** Findings:
  - The maker leg is already measured STOP (10-13 `mm_replay`).
  - M2 is dead (VHHH Phase 0).
  - The plan's fallback, the forecast product, was itself killed on 10-17.
  - The bot already prices from the market: 0 entries since 09-28.
- **Redlined the implementation plan:** `2026-10-22-revamp-implementation-plan-redline.md` (untracked, not sent to @AlexL).
- **Do other bots see readings first?** Yes. Script: scratchpad `C:\Users\user\AppData\Local\Temp\claude\C--Users-user-Downloads-weather-forecast\972b43ff-7517-4c93-a83b-5a2fd3f019c0\scratchpad\lead_lag.py`.
  - Over 729 new-high events, prices moved a median **154 s before aviationweather received the METAR**, and in 65% of events they moved before it arrived.
  - Our own delay after the feed receives a reading is only 24 s, so the lag is in the public feed, not our polling.
- **The RCSS "lag" isn't tradable.** Script: `killed_bucket.py` in the same scratchpad.
  - At RCSS, 0 of 58 just-ruled-out buckets still had NO on sale cheap enough for ≥1¢ after fee.
  - Across all stations, 5 of 1,992 did, worth about $1.34 in total.
- **Settled bucket vs our recorded daily high:** 21 of 2,008 station-days disagree (backup `polyweather-20261017T141136Z`).
  - **LFPB 10-07:** the 14:00Z METAR read 19 °C, YES-18 crashed to 2¢, recovered to 76¢ within about 2.5 hours, and settled 18. Someone was reading the cited NOAA timeseries page.
  - **Other groups:** ZGSZ 08-07..08-23 (12 days; an early data problem on our side); **09-20 at WSSS, RPLL and ZGGG, each settled 1 °C below our high**; RKSI 08-21; 4 US °F days.
- **Inputs used:** recorder copy `...\c7c99cfe*\scratchpad\fr\fr.sqlite3`; trading copy `...\c7c99cfe*\scratchpad\pw\pw.sqlite3`. Both are under `C:\Users\user\AppData\Local\Temp\claude\C--Users-user-Downloads-weather-forecast\`.

## Open / next step
1. **User to choose next:**
   - (a) Look into the 09-20 triple. If our data was wrong that day, the bot scored itself against false answers.
   - (b) Run a recorder on the PC for the cited settlement pages (NOAA timeseries for Europe, Wunderground for RCSS), so they can be compared with METAR as readings arrive. This doesn't touch the box, so the freeze holds.
2. **Not done:** check NOAA precision for M5. US observations do carry tenths of a degree (e.g. KBKF 32.7 °C), which counts against M5.
3. **Not done:** sending the redline to @AlexL. If the user wants it shared, publish it as a page.
4. **Carried from 10-13:**
   - **Before 11-03, local only:** fix `backtest/observed_half_life._forecasts_by_day` (ignore late-day fetches) and the KLAX/KSFO/KSEA config bounds.
   - **From 11-05, in order:** `lock_score.py --locked-read`, then push `main`, then merge and deploy `feat/vhhh-fresh-bias` (16:00–19:00 UTC, `~/deploy.sh`).
   - **About mid-November:** re-run `python -I vhhh_check.py <copy>`.
   - **Also open:** redemption proof needs POL and the user's OK; DST restarts after 10-25 and 11-01.
5. **NEA (WSSS official forecast): fixed locally 2026-10-23, uncommitted, not deployed.**
   - `clients/official/nea.py` now reads data.gov.sg **v2** (`api-open.data.gov.sg/v2/real-time/api/{twenty-four-hr-forecast,two-hr-forecast,air-temperature}`). Shapes taken from real captures, saved as `tests/fixtures/nea_v2_*.json`.
   - **Date stamp:** the 24h forecast is stamped with the SGT day whose whole 12:00-18:00 its `validPeriod` covers; no afternoon or an unreadable payload gives None. This replaces `abafd5b`'s start-hour rule, which fell back to `local_today`. The 20:00 SGT day-ahead row now carries NEA as tomorrow. Source string is still `nea_24hr`.
   - **API key:** not needed. Keyless v2 limit is 6 calls per 10 s; an `x-api-key` header raises it to 12/30.
   - **v1 is still alive** (HTTP 200 on 2026-10-23), so this waits for the freeze to lift. Deploy with the 11-05 batch unless v1 dies on the box first.
   - `get_live_reading` was ported but still has no callers. It reads NEA's Changi street station (S24), not the METAR that settles the market.
   - Tests: `tests/test_nea_target_date.py` (9). Full suite 2,168 passed.

## Gotchas
- **SSH:** `"/c/Program Files/PuTTY/plink.exe" -batch -ssh -i "C:\Users\user\Downloads\multicityweatherbot.ppk" ubuntu@43.216.25.99 "<cmd>"`. Never run pytest on the box.
- **On the box, a pull is a deploy.** No pulls and no box changes before 11-03: any change splits the lock.
- **The fast-recorder DB is not in `pull_backup.sh` output.** A durable copy is at `C:\Users\user\polyweather-backups\fast_recorder-20261013.sqlite3` (sha256 `f325b11d…a1ff7`). Its name carries no `T…Z` stamp, so `pull_backup.sh` never prunes it.
- **Analysis scripts need the repo code on their import path.** Run them with `python -I <script> <db...> /c/Users/user/Downloads/weather-forecast/weather-forecast`.
- **Re-running `mm_replay`:** only from `2bcf38f` or later. `7323eec` reproduces the fake GO.
- **Pushing to `origin/main` has been blocked for Claude before.** Expect the user to push.
