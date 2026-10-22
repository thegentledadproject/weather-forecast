# Handoff - 2026-10-22

## State
- **Main:** `origin/main` is at `7323eec`, "Merge feat/fast-recorder: METAR + book recorder (records only) and offline mm_replay". It has had no new commits since 2026-09-28.
- **This session's branch:** `claude/wsss-single-market-review-okl9dh`, at main plus one review doc. There is no PR yet.
- **Box:** NOT checked this session, because the cloud sandbox has no SSH. The last known state is below, from 2026-09-27. Re-check it before trusting it.
- **Freeze:** we are inside the no-deploy freeze, 2026-10-06 → 11-02. Restarts are OK.

**Box, last checked 2026-09-27 16:24 UTC:**
- **Code and daemon:** the box runs `b67ac73`, the daemon is healthy, and the watchdog says ok.
- **Mode:** PAPER, with 3 open paper positions and 0 live.
- **Approvals:** about 1-10 a day, falling as intended.
- **Settled positions:** 20 settled for target dates 09-24..26. 1 won, 19 lost, −$79.44 on $103.35. This is a watch item, not a defect.
- **Box disk:** 71% used, 2.0G free. The DB is 715MB, and every deploy adds an unpruned backup of about 180MB.
- **Code on main since then, not on the box:**
  - `fd869f2`: the WMKK forecast-date fix.
  - `10763eb`: a config tidy.
  - `d56ccf6`: the watchdog kill alert.
  - `7323eec`: the fast recorder and `mm_replay`.
  - The dashboard cleanup.
  - Check whether any of these were deployed before 10-06.

## Done this session (2026-09-28 → 10-22)
- **WSSS single-market review:** `weather-forecast/docs/validation/2026-09-28-wsss-single-market-review.md`, on the branch above.
  - **09-28 pass:** no bug that loses money. Live is correctly locked, because `REDEMPTION_PROVEN_TX` is None and `MATURITY_OVERRIDE` is empty.
  - **10-22 re-check:** the `MATURITY_SNAPSHOT` comment and the duplicate `BIAS_HALF_LIFE_DAYS` are fixed (`10763eb`). The two items below are still open.
- **Suggested task queued:** "Fix NEA forecast day-stamp for WSSS". It is a card in the Claude app and has not been started.

## Open / next step
1. **LOCK_SHA is still `None` on main** (`weather-forecast/lock_score.py:41`). HANDOFF step 4 said to set it on 10-05, commit and deploy.
   - If it was set on the box by hand, commit the same value.
   - If it was never set, the 11-05 `--locked-read` has nothing to score against. Decide what to do before 11-05.
2. **NEA forecast is stamped "today" whatever it covers** (`clients/official/nea.py:55`). This is the same bug `fd869f2` fixed for WMKK.
   - The day-ahead recorder runs at 20:00 SGT, when NEA's high is tomorrow's. So WSSS's day-ahead record leaves out the NEA forecast, and a row with tomorrow's high is saved as today.
   - Bias is safe only while `ERROR_SAMPLE_FETCH_WINDOW_ENABLED = True`.
   - Fix it after the freeze: stamp from `valid_period`. The full spec is in the review doc, section A.
   - In the same change, move `nea.py` from the old `api.data.gov.sg/v1` address to the new `api-open.data.gov.sg/v2/real-time/api/...` one (forecast: `twenty-four-hr-forecast`). Both touch the same function. Check the v2 response shape from a live call before coding, because it is not the same as v1.
   - The queued task card covers both.
3. **Wave-2 falsifier result (~10-04): unknown here.**
   - If it said to set `ERROR_SAMPLE_FETCH_WINDOW_ENABLED = False`, do item 2 first. Otherwise the mislabelled NEA rows enter WSSS bias pairs.
   - On main both flags are still True.
4. **WSSS bucket bounds are stale.** `config.py:312-313` says 28-38, but the live 09-25 event was 25-35. This affects backtests only. Re-sweep from the drift log, or build the bucket-window self-heal.
5. **Check the NEA v1 API is alive.** `nea.py` still calls `api.data.gov.sg/v1`, and the sandbox proxy blocks both that host and the v2 one. On the box, grep the journal for `[NEAClient] ... failed`, and run:
   ```
   curl -s -o /dev/null -w '%{http_code}\n' https://api.data.gov.sg/v1/environment/24-hour-weather-forecast
   curl -s -o /dev/null -w '%{http_code}\n' https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast
   ```
   - If v1 fails and v2 answers, WSSS has silently lost its official forecast. Then the v2 move in item 2 can't wait for the end of the freeze.
   - `get_live_reading` (NEA air temperature) is never called. It reads NEA street thermometers, not the airport METAR, so it can only ever be a hint, never settlement truth.
6. **Carried over from the 09-28 handoff:**
   - Confirm the scheduled backup runs.
   - Re-check `contract_checks` reaches 35 VALID.
   - Read the 10-04 checkpoint outputs: `cohort_monitor`, `calibration_*_replay`.
   - Do the DST restarts after 2026-10-25 01:00 UTC and after 2026-11-01 06:00 UTC.
   - On 2026-11-05, read `python lock_score.py --locked-read`. This depends on item 1.
   - Redemption is still unproven.
   - Three unmerged local branches on the PC still need a merge or drop decision: `fix/observation-lookback-window`, `fix/settled-bucket-resolution-fallback` and `spec/recency-weighted-observed-term`.
   - Decide whether recency weighting ships after 11-02. WSSS puts 60% of its estimate on a flat 30-day observed mean going into the NE monsoon.
   - Dashboard Phases 2-4 are not started.
7. **Low priority:** the comment at `backtest/engine.py:151-153` still says "`target_date.replace(day=1)`". Only the comment is wrong.

## Gotchas
- **SSH:** `"/c/Program Files/PuTTY/plink.exe" -batch -ssh -i "C:\Users\user\Downloads\multicityweatherbot.ppk" ubuntu@43.216.25.99 "<cmd>"`. pscp takes one remote source per call.
- **Box layout:** code is in `~/weather-forecast/weather-forecast/`, venv at `~/weather-forecast/.venv/bin/python`.
- **Reading the DB:** use `sqlite3.connect("file:data/polyweather.sqlite3?mode=ro", uri=True)`.
  - `positions` has no `pnl_usd` column; `contract_checks` uses `status`, not `verdict`.
- **Deploys:** only 16:00–19:00 UTC, with `~/deploy.sh`. `!! DEPLOY ABORTED` means the daemon is STOPPED. Not before 11-02.
- **Tests:** never run pytest on the box. Locally: `cd weather-forecast && timeout 540 python -m pytest -q -p no:cacheprovider`, in the foreground.
- **Shared checkout:** another session also commits to main. Fetch and check `git log` before merging.
- **Cloud sessions:** no SSH to the box, and data.gov.sg is blocked by the proxy. Box checks must run from the PC.
