# WSSS single-market review - 2026-09-28

## Re-check 2026-10-22, against main at `7323eec`

Seven commits have landed since the first review. The original review is
below, unchanged.

| # | Finding (2026-09-28) | Status now |
|---|---|---|
| 1 | `MATURITY_SNAPSHOT` comment wrong | **Fixed** in `10763eb`. The comment now calls it the test pin. |
| 2 | WSSS bucket window stale (28-38) | **Open.** It is still 28-38 at `config.py:312-313`. |
| 3 | NEA forecast stamped "today" | **Open, and now worse.** See A below. |
| 4 | `BIAS_HALF_LIFE_DAYS` defined twice | **Fixed** in `10763eb`. |

### A. The NEA date bug is the WMKK bug that was just fixed (medium)
`fd869f2` fixed this exact mistake for WMKK. Its client stamped whatever
forecast it got as `local_today`, so tomorrow's max was filed as today's. WSSS's
`clients/official/nea.py:55` still does the same thing. The day-ahead recorder
(`30f256d`) makes it bite:
- `day_ahead.py:61` runs at 20:00 SGT (`DAY_AHEAD_RECORD_HOUR_LOCAL = 20`).
  By then NEA's current 24-hour forecast is the evening issue, whose high
  belongs to TOMORROW.
- `nea.py` stamps that row as TODAY. `pipeline.gather_forecasts` then
  filters on `target_date == tomorrow` and drops it.
- So WSSS's day-ahead record leaves out the NEA forecast, while the live
  morning blend includes it. The two are not like-for-like for WSSS only.
  Every WWIS station already matches by date, and WMKK does now too.
- It also saves a `nea_24hr` row for today that carries tomorrow's high.
  Bias samples are safe while `ERROR_SAMPLE_FETCH_WINDOW_ENABLED = True`,
  because only 04:00-08:00 local fetches count. The flag's own stop rule says
  to set it False if the wave-2 falsifier fails. If that happens, the
  mislabelled rows flow into WSSS's bias pairs, and `wave2_falsifier.py`'s
  "window off" arm already reads them today.
- Fix: do what `fd869f2` did for WMKK. Read `items[0].valid_period`, and
  stamp the date whose afternoon (12:00-18:00 SGT) the period covers.
  Return None if it covers no afternoon. Keep the source string
  `nea_24hr` so the history stays joined. This is a code change, so it waits
  for the end of the freeze (after 11-02).
- Not verified here: whether the old `api.data.gov.sg/v1` endpoints still
  answer. This sandbox's proxy blocks the host. Check the box logs for
  `[NEAClient] ... failed`.

### B. LOCK_SHA was never committed (medium, not WSSS-only)
HANDOFF step 4 said to set `LOCK_SHA` on 2026-10-05, commit it and deploy.
`lock_score.py:41` is still `None` on main, and the freeze has since begun.
If the box was set by hand, commit that value so main matches the box.
If it was never set, the 11-05 `--locked-read` has nothing to score
against, and that needs a decision before 11-05.

### C. Stale comment in the backtest (low)
`backtest/engine.py:151-153` still says stored observations go back to
"`target_date.replace(day=1)`". The live path has used a rolling 30-day
lookback since the lookback fix. It is comment only, because the code uses
`OBSERVATION_WINDOW_DAYS = 30`.

### Checked and fine in the new code
- `fast_recorder.py:93-99` includes WSSS, because it takes every
  `metar_daily_max` station.
- The watchdog's daily kill-criterion alert covers WSSS through the cohort
  rule.
- WSSS is still locked out of live trading: `REDEMPTION_PROVEN_TX` is None,
  and `MATURITY_OVERRIDE` is empty.

Scope: one market, Singapore Changi (WSSS). The review followed one WSSS
market through the code, from the market rules to settlement, and checked
every WSSS-specific setting. It is a code and config review only. It used no
box data, so nothing here is a new measurement.

## Verdict

No money-losing bug was found on the WSSS path. Live trading is correctly
locked: `REDEMPTION_PROVEN_TX` is None, and `MATURITY_OVERRIDE` is empty, so
WSSS must pass the measured maturity checks (`config.py:4274-4282`). There
are four cleanup items and two watch items.

## Checked and correct

- **Settlement source.** The market settles on the NOAA timeseries for WSSS,
  with Wunderground as a fallback (`tests/fixtures/gamma_rules/WSSS_2026-09-25.json`).
  Both are the airport METAR record. The code saves that record as
  settlement-grade `metar_daily_max` (`clients/metar_client.py:189-193`).
  HANDOFF already records a 99.5% NOAA-vs-METAR match.
- **Local day.** SGT is UTC+8 with no DST. The daily max is taken over the
  local day (`metar_client.py:115-151`), and the lookahead bound matches it
  (`config.py:135-159`).
- **Whole-degree buckets.** Buckets use `floor(x + 0.5)`, not Python's
  `round()`, so values ending in .5 always round up (`bucket_axis.py:111-143`).
  This matches the market's whole-degree rule.
- **Forecast day filter.** Only today's forecast rows reach the blend
  (`pipeline.py:80-82`).
- **Observed-term window.** It is a rolling 30 days, not "since the 1st", so
  the observed term no longer disappears on the 1st of the month
  (`pipeline.py:111-117`).
- **Bias correction.** The bias is subtracted from the forecast term only,
  never from the observed term (`calibration.py:229-230`).
- **Blend weight.** WSSS uses 0.40, so 60% of the estimate comes from
  observations. The comment's evidence supports 0.40 over 0.50
  (`config.py:3533-3567`).
- **Entry window.** It is 05:00-08:00 local, which is 21:00-24:00 UTC
  (`config.py:2502-2503`).

## Findings

Ranked, highest first. All were confirmed by reading the code.

### 1. MATURITY_SNAPSHOT is dead, and its comment is wrong (low-med)
`config.py:4758-4779` says backtest replays read this frozen dict, and it
still lists WSSS as `"mature"`. That value came from the override that was
removed on 2026-09-24. But `backtest/entry_sim.py:154-165` actually reads
`config.STATION_MATURITY`. That is the live, storage-derived value, which the
same comment says replays must not read. Nothing in the code reads
`MATURITY_SNAPSHOT`.
- Risk: anyone who reads the comment will believe WSSS replays as "mature"
  and that replays are pure. Neither is true.
- Action: delete `MATURITY_SNAPSHOT` or wire it in. Either way, fix the
  comment and the docstring at `config.py:5062-5063`.

### 2. WSSS bucket window in config is stale again (low)
`config.py:312-313` says 28-38. The 2026-09-25 event starts at
"25°C or below", so its window is 25-35. Trading is not affected, because
live bounds come from the token map. Backtest replays are affected: they clamp
to these bounds, so a replayed tail can settle in the wrong bucket.
- Action: re-sweep the bounds from the drift log (the command is at
  `config.py:284-285`). Or build the self-heal from
  `docs/superpowers/specs/2026-09-02-bucket-window-self-heal-design.md`.

### 3. NEA forecast is stamped "today" whatever it covers (low)
`clients/official/nea.py:52-58` labels NEA's 24-hour forecast as
`local_today` and ignores the payload's `valid_period`. During trading hours
this is right. After NEA's evening issue, though, the "high" belongs to
tomorrow and gets stored as today's. The bias sample is protected because
`ERROR_SAMPLE_FETCH_WINDOW_LOCAL = (4, 8)` keeps only 04:00-08:00 local
fetches. Any other reader of stored `nea_24hr` rows is not protected.
- Action: take the target date from `valid_period`, or drop rows whose
  period does not cover the local afternoon.
- Also worth a one-time check: `nea.py` still calls the old `api.data.gov.sg/v1`
  endpoints. Confirm on the box that `[NEAClient] ... failed` is not in the
  logs. If it is, WSSS has silently lost its official-forecast input.

### 4. BIAS_HALF_LIFE_DAYS is defined twice (low)
`config.py:3672-3702` and `3704-3734` hold the same comment and the same
value, 14.0. Today it is harmless. If someone later edits only the first
copy, the second copy silently wins.
- Action: delete one copy.

## Watch items (not bugs)

- **The observed term is a flat mean over 30 days.** It carries 60% of the
  WSSS estimate, and `OBSERVED_HALF_LIFE_DAYS = None` means no recency
  weighting. From Oct to Nov Singapore moves into the inter-monsoon and then
  the NE monsoon, when daily highs drift down, and a flat 30-day mean trails
  that drift by about two weeks. The recency-weighted spec is still on an
  unmerged branch. The deploy freeze starts 2026-10-06, so decide before then
  whether it ships or waits until after 11-02.
- **Model vs market.** The removed override's own record shows the model
  losing to the market on Brier at WSSS in every window scored. Brier is an
  accuracy score for probabilities where lower is better, and in every window
  the market's prices scored better than the model. The ~10-04 checkpoint
  (`cohort_monitor.py --by station`) is where to re-read this for WSSS.
