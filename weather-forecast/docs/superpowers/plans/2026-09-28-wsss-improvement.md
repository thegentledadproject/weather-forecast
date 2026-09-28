# WSSS Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carry out steps 2–10 of the WSSS state-and-improvement plan: build the few missing replay/report tools, run the date-gated measurements, and ship only what passes.

**Architecture:** Most of the work is operator runs of tools that already exist (`cohort_monitor.py`, `backtest.cli`, `take_sweep.py`, `mm_replay.py`, `redeem.py`). There are three code changes (the source-mix tool already exists on `analysis/source-mix-replay`):
- the WSSS bucket window;
- the NEA date stamp;
- a small `day_ahead_report.py`.

Nothing here touches the trading path, except two config values: the source exclusion, which ships only if Task 3 passes, and `REDEMPTION_PROVEN_TX`.

**Tech Stack:** Python 3.11, sqlite3, pytest. Only the standard library: the box has no numpy.

**Spec:** `../2026-09-28-wsss-improvement-plan.md` (repo ROOT, one level above the code dir). Supporting evidence is in ROOT `2026-09-28-wsss-single-market-review.md`.

All paths are relative to the code dir `weather-forecast/` unless marked ROOT.

## Global Constraints

- **Freeze 10-06 → 11-02:** no scheduler or trading-path deploy. Offline study and the operator-run redemption are allowed.
- **Phase 2, from 11-03:** one change at a time, two weeks each. Input fixes (Tasks 3/4 winners) go first, then take-profit (Task 5), with the NEA + bucket-window fixes bundled into whichever deploy comes first.
- **Never run pytest on the box.** Run tests locally with `python -m pytest tests -q` from the code dir.
- **Offline replays run against a pulled copy of the prod DB,** never the live file. Use `deploy/pull_backup.sh`, then set `config.DB_PATH` in-process.
- **Score on multi-class Brier, never RMSE alone.** "Average miss" (MAE) is reported beside Brier because the spec's pass bar for step 3 names both.
- **Do not loosen the NO-side floor (`00c`) or the calibration shrink (`0a2`).** Do not add data sources. Do not add entry-price filters.
- **Moving money (Task 8) needs explicit operator approval,** given at the time.
- **Live re-arm (Task 11) needs every gate in spec step 11.** No `MATURITY_OVERRIDE`.

## Corrections to the spec, found while mapping the code

1. **Step 3's two alternative mixes are the same thing for WSSS.** WSSS stores exactly three sources (`open_meteo_ecmwf`, `open_meteo_gfs`, `nea_24hr`), so "the mix without GFS" *is* "ECMWF plus NEA". Task 3 therefore compares **all three / no GFS / ECMWF only**.
2. **Step 7's ABI fix is already done.** Commit 65c0b65 on main makes `redeem.py` send `[yes, no]` amounts. What remains is proving it on-chain, plus stale text in `redeem_abi_probe.py` and `docs/runbooks/operations.md:85-87`.
3. **The config bucket window is 28–38, but the live WSSS event lists "25°C or below", so the window is 25–35** (ROOT review §2). The replays in Tasks 3, 4 and 6 clamp to the config pair, so this fix moves to the **front** of the plan, on a branch. Deploying it still waits for Phase 2.
4. **Step 4 says "score it through November", but Phase 2 starts 11-03.** Task 4 runs at 11-02 as planned. If it passes, re-run it about 11-30 before Task 10 ships it.

## Review Focus

1. **NEA payload with no `valid_period`, or a malformed one.** Expect a fall back to `local_today` (today's behaviour), not a crash or a dropped forecast. The test is in Task 2.
2. **An NEA evening issue whose period starts after 14:00 local.** Expect it stamped as *tomorrow*. The test is in Task 2.
3. **A source arm that leaves a day with zero forecasts.** Expect the day skipped for that arm, and the paired comparison to use only days both arms scored. Owned by the existing tool (a9a36c4); Task 3 only runs it.
4. **Source exclusion in the replay must also change the bias fit.** A bias fitted on three sources and applied to two is the error `blendable_forecasts` warns about. Owned by the existing tool (a9a36c4).
5. **Day-ahead rows with no settled truth yet.** Expect them skipped, not scored as "didn't happen". The test is in Task 6.

---

### Task 1: WSSS bucket window 28–38 → 25–35 (branch now, deploy in Phase 2)

**Files:**
- Modify: `config.py:305-313` (WSSS `bucket_min_c` / `bucket_max_c` and the comment above them)
- Test: `tests/test_bucket_bounds_live.py`

**Interfaces:**
- Produces: `config.get_station("WSSS").bucket_min_c == 25`, `.bucket_max_c == 35`. Tasks 3, 4 and 6 depend on this.

- [ ] **Step 1: Confirm the live window from the box (read-only)**

```bash
plink <host from memory ec2-deployment> "journalctl -u polyweather --since '8 days ago' | grep -oE 'BOUNDS DRIFT for WSSS: live event lists [0-9]+-[0-9]+' | sort | uniq -c"
```

Expected: the majority is `25-35`. If it is not 25-35, stop, put the majority window into Steps 2–4 instead, and tell the operator.

- [ ] **Step 2: Write the failing test** (append to `tests/test_bucket_bounds_live.py`)

```python
def test_wsss_config_window_matches_live_event():
    # Live WSSS event lists "25°C or below" .. "35°C or higher" (drift log, 2026-09-28).
    st = config.get_station("WSSS")
    assert (st.bucket_min_c, st.bucket_max_c) == (25, 35)
    assert st.bucket_max_c - st.bucket_min_c + 1 == config.EXPECTED_BUCKET_COUNT
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `python -m pytest tests/test_bucket_bounds_live.py::test_wsss_config_window_matches_live_event -v`
Expected: FAIL, `(28, 38) != (25, 35)`.

- [ ] **Step 4: Edit `config.py`**

Replace these lines:

```python
        # Reswept 2026-08-20 (was 27-37): 28-38 on 54 of 74 cycles sampled, and the current window;
        # 25-35 seen once (Aug 12).
        bucket_min_c=28,
        bucket_max_c=38,
```

with:

```python
        # Reswept 2026-08-20 (was 27-37): 28-38 on 54 of 74 cycles sampled.
        # Reswept 2026-09-28 (was 28-38): live event lists "25°C or below", 25-35.
        bucket_min_c=25,
        bucket_max_c=35,
```

- [ ] **Step 5: Run the test file, then the full suite**

Run: `python -m pytest tests/test_bucket_bounds_live.py -v`, then `python -m pytest tests -q`
Expected: all PASS. If a test pinned 28/38 for WSSS, update it to 25/35. Don't loosen it.

- [ ] **Step 6: Commit on branch `feat/wsss-phase1`**

```bash
git checkout -b feat/wsss-phase1 main
git add config.py tests/test_bucket_bounds_live.py
git commit -m "config: WSSS bucket window 28-38 -> 25-35 (live event re-centred)"
```

---

### Task 2: NEA forecast takes its date from `valid_period`

**Files:**
- Modify: `clients/official/nea.py:40-62` (`get_24hr_forecast`)
- Create: `tests/test_nea_target_date.py` (modelled on `tests/test_met_malaysia_day_selection.py`)

**Interfaces:**
- Produces: `nea._target_date(valid_period: dict, fallback: date) -> date`

- [ ] **Step 1: Write the failing tests**

```python
"""
nea.py stamps a forecast with the local day whose afternoon it covers.
The evening issue (period 18:00 -> 18:00 next day) is TOMORROW's high.
Without a usable valid_period it keeps the old behaviour: local_today.
"""
from datetime import date
from types import SimpleNamespace

import config
from clients.official import nea


def _payload(start, end, high=33):
    vp = None if start is None else {"start": start, "end": end}
    item = {"general": {"temperature": {"high": high}, "forecast": "Fair"}}
    if vp is not None:
        item["valid_period"] = vp
    return {"items": [item]}


def _run(monkeypatch, payload, today=date(2026, 9, 28)):
    monkeypatch.setattr(nea.requests, "get",
                        lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload))
    monkeypatch.setattr(config, "local_today", lambda st=None: today)
    return nea.NEAClient().get_24hr_forecast(config.get_station("WSSS"))


def test_morning_issue_is_today(monkeypatch):
    f = _run(monkeypatch, _payload("2026-09-28T06:00:00+08:00", "2026-09-29T06:00:00+08:00"))
    assert f.target_date == date(2026, 9, 28)


def test_evening_issue_is_tomorrow(monkeypatch):
    f = _run(monkeypatch, _payload("2026-09-28T18:00:00+08:00", "2026-09-29T18:00:00+08:00"))
    assert f.target_date == date(2026, 9, 29)


def test_missing_valid_period_falls_back_to_local_today(monkeypatch):
    f = _run(monkeypatch, _payload(None, None))
    assert f.target_date == date(2026, 9, 28) and f.max_temp_c == 33.0


def test_malformed_valid_period_falls_back(monkeypatch):
    f = _run(monkeypatch, _payload("not-a-time", "x"))
    assert f.target_date == date(2026, 9, 28)
```

Check the client class name: it is `NEAClient` (see the `[NEAClient]` log prefix). Confirm it with `grep -n "^class" clients/official/nea.py`, and fix the tests if it differs.

- [ ] **Step 2: Run them and confirm the evening test fails**

Run: `python -m pytest tests/test_nea_target_date.py -v`
Expected: `test_evening_issue_is_tomorrow` FAILS (got 09-28). The other three pass.

- [ ] **Step 3: Implement** (in `clients/official/nea.py`; add `timedelta` to the datetime import)

```python
# The daytime high falls in the local afternoon. A 24-hour period is stamped
# with the first 14:00 local at or after its start: the 06:00 issue is today,
# the 18:00 evening issue is tomorrow.
AFTERNOON_HOUR_LOCAL = 14


def _target_date(valid_period, fallback):
    try:
        start = datetime.fromisoformat(valid_period["start"])
    except (TypeError, KeyError, ValueError):
        return fallback
    day = start.date()
    if start.hour > AFTERNOON_HOUR_LOCAL:
        day += timedelta(days=1)
    return day
```

In `get_24hr_forecast`, change `target_date=config.local_today(station),` to:

```python
            target_date=_target_date(record.get("valid_period"), config.local_today(station)),
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python -m pytest tests/test_nea_target_date.py tests/test_local_today.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add clients/official/nea.py tests/test_nea_target_date.py
git commit -m "nea: stamp the 24h forecast with the day its afternoon falls in"
```

---

### Task 3: Source-mix replay for WSSS (spec step 3). Run the existing tool, no new code

`weather-forecast/source_mix_replay.py` already exists on branch `analysis/source-mix-replay` (a9a36c4). It was built for WMKK and isn't merged. It scores every source subset × blend weight (0.40/0.60/0.85/1.00) against the current mix, through the production blend with leave-one-out bias. It scores in halves, on Brier and MAE, with a Brier CI. Its SOURCE PASS is stricter than spec step 3, because it adds CI95 below 0 (six mixes are tried), so use it as is.

- [ ] **Step 1: Pull a copy on or after 10-06 and pick the width**

The tool scores every arm at one fixed spread (`--width`, default 0.82, which is WMKK's). Use WSSS's median recorded spread. The width is common to every arm, so it can't reorder them.

```bash
bash ../deploy/pull_backup.sh
python -c "import gzip,shutil,glob;f=sorted(glob.glob(r'C:/Users/user/polyweather-backups/polyweather-*.sqlite3.gz'))[-1];shutil.copyfileobj(gzip.open(f),open(r'C:/Users/user/polyweather-backups/replay.sqlite3','wb'))"
python -c "import sqlite3,statistics;c=sqlite3.connect('file:C:/Users/user/polyweather-backups/replay.sqlite3?mode=ro',uri=True);print(round(statistics.median(r[0] for r in c.execute(\"SELECT DISTINCT std_dev_c FROM ev_snapshots_day_ahead WHERE station_icao='WSSS' AND std_dev_c IS NOT NULL\")),2))"
```

- [ ] **Step 2: Run it from that branch**

```bash
git switch analysis/source-mix-replay
python source_mix_replay.py --db C:/Users/user/polyweather-backups/replay.sqlite3 --station WSSS --width <step 1 value>
```

- [ ] **Step 3: Decide.** Only a mix the tool marks SOURCE PASS counts: Brier and MAE lower in both halves, plus Brier CI95 below 0. If one passes, its Phase 2 change (Task 10) is one entry, `"WSSS": <excluded tuple>`, in `config.FORECAST_SOURCES_EXCLUDED_BY_STATION`. Record the table in ROOT `HANDOFF.md`. For WSSS, "no GFS" and "ECMWF + NEA" are the same arm (Correction 1).

---

### Task 4: Observed-term half-life replay in halves (spec step 4, run 11-02)

No code: `backtest/observed_half_life.py` already sweeps half-lives. It has no date filter, so split the halves by scored day in Python.

- [ ] **Step 1: Run the full-window sweep on the prod copy (the same `replay.sqlite3` as Task 3, re-pulled on 11-02)**

```bash
python -c "import config,pathlib;config.DB_PATH=pathlib.Path(r'C:/Users/user/polyweather-backups/replay.sqlite3');import runpy,sys;sys.argv=['x','--stations','WSSS','--half-lives','none,14,7,5,3'];runpy.run_module('backtest.observed_half_life',run_name='__main__')"
```

- [ ] **Step 2: Score each half**

```bash
python -c "
import config,pathlib,statistics
config.DB_PATH=pathlib.Path(r'C:/Users/user/polyweather-backups/replay.sqlite3')
from backtest import observed_half_life as o
res={hl:{d:b for d,b,*_ in o.score_station('WSSS',hl)} for hl in (None,14,7,5,3)}
for name,days in zip(('1st','2nd'),(lambda d:(d[:len(d)//2],d[len(d)//2:]))(sorted(res[None]))):
    for hl in (14,7,5,3):
        b={d:res[None][d] for d in days if d in res[hl]}; c={d:res[hl][d] for d in b}
        print(name,hl,len(b),round(statistics.fmean(c.values())-statistics.fmean(b.values()),4),o._paired_bootstrap(b,c)[0])
"
```

- [ ] **Step 3: Decide**

A half-life **passes** only if all three hold:
- it beats `none` in both halves;
- full-window P ≥ 0.95;
- the gain is not carried by reversal days. Recompute without the day after any 1.5 °C+ settle change.

Otherwise `OBSERVED_HALF_LIFE_DAYS` stays `None`. Write the result to ROOT `HANDOFF.md`. If it passes, re-run it about 11-30 (Correction 4) before Task 10 ships it.

---

### Task 5: Take-profit vs hold, re-tested on fresh data (spec step 5)

No code. The 09-07 run found engine mode and stored mode disagree, so run **both modes on both halves**. Split the window at its midpoint, from the Task 3 halves or the calendar midpoint.

- [ ] **Step 1: Engine mode, each half** (from the code dir, market DB pulled alongside)

```bash
python -m backtest.take_sweep --from 2026-09-08 --to 2026-10-05 --stations WSSS --takes 0.50,1,2,none
python -m backtest.take_sweep --from 2026-10-06 --to 2026-11-02 --stations WSSS --takes 0.50,1,2,none
```

- [ ] **Step 2: Stored mode, each half, against the prod copy**

```bash
python -m backtest.take_sweep --stored --live-db C:/Users/user/polyweather-backups/replay.sqlite3 --from 2026-09-08 --to 2026-10-05 --stations WSSS --takes 0.50,1,2,none
python -m backtest.take_sweep --stored --live-db C:/Users/user/polyweather-backups/replay.sqlite3 --from 2026-10-06 --to 2026-11-02 --stations WSSS --takes 0.50,1,2,none
```

- [ ] **Step 3: Decide**

A take value **passes** only if it beats the shipped `0.50` on LOTTERY-cohort return in all 4 cells (2 modes × 2 halves), and no cell carries a censoring warning. Otherwise `LOTTERY_PROFIT_TAKE_PCT` stays untouched. Record the 4 tables in ROOT `HANDOFF.md`.

---

### Task 6: Day-ahead report (spec step 6a, run ~10-12)

**Files:**
- Modify: `storage.py`: add `load_day_ahead_rows` after `save_day_ahead_rows` (L1432)
- Create: `day_ahead_report.py`
- Test: `tests/test_day_ahead_report.py`

**Interfaces:**
- Consumes: `storage.save_day_ahead_rows(icao, target_date, generated_at, results, central_c, std_dev_c)`, `resolution.bucket_for_temp(t, lo, hi, edge_mode=...)`, `observed_half_life._paired_bootstrap`.
- Produces:
  - `storage.load_day_ahead_rows(station_icao) -> [(target_date: date, bucket_c: int, generated_at: str, model_prob: float, market_price: float)]` (YES side only);
  - `day_ahead_report.score(icao) -> {day: (model_brier, market_brier)}`.

- [ ] **Step 1: Write the failing test**

```python
"""
day_ahead_report scores the evening-before book (YES side) against the bucket
the day settled into. Pinned: the LATEST recording per (day, bucket) is used;
days without settled truth are skipped, never scored as "no".
"""
from datetime import date
from types import SimpleNamespace

import config
import day_ahead_report
import storage
from models import ObservedReading


def _rec(day, gen, probs):
    rows = [SimpleNamespace(bucket_c=b, side="YES", model_prob=m, market_price=p, market_bid=None)
            for b, (m, p) in probs.items()]
    storage.save_day_ahead_rows("WSSS", day, gen, rows, 32.0, 0.8)


def test_scores_latest_recording_and_skips_unsettled(tmp_db):
    src = config.get_station("WSSS").resolution_grade_source
    storage.save_observation(ObservedReading("WSSS", date(2026, 10, 1), 32.2, src))
    _rec(date(2026, 10, 1), "2026-09-30T12:00:00Z", {32: (0.1, 0.1), 33: (0.9, 0.9)})   # superseded
    _rec(date(2026, 10, 1), "2026-09-30T13:00:00Z", {32: (0.8, 0.5), 33: (0.2, 0.5)})
    _rec(date(2026, 10, 2), "2026-10-01T12:00:00Z", {32: (0.5, 0.5)})                   # no truth yet
    scored = day_ahead_report.score("WSSS")
    assert list(scored) == [date(2026, 10, 1)]
    model, market = scored[date(2026, 10, 1)]
    assert round(model, 3) == round(((0.8 - 1) ** 2 + 0.2 ** 2) / 2, 3)
    assert round(market, 3) == 0.25
```

Check how 32.2 °C maps to a bucket with `resolution.bucket_for_temp(32.2, 25, 35, edge_mode=...)`. The test assumes bucket 32. If the station's edge mode rounds differently, change the observation value, not the assertion.

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_day_ahead_report.py -v`
Expected: FAIL, because `day_ahead_report` doesn't exist.

- [ ] **Step 3: Add the loader in `storage.py`**

```python
def load_day_ahead_rows(station_icao: str) -> List[Tuple[date, int, str, float, float]]:
    """YES-side day-ahead rows as (target_date, bucket_c, generated_at, model_prob, market_price)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT target_date, bucket_c, generated_at, model_prob, market_price "
            "FROM ev_snapshots_day_ahead WHERE station_icao = ? AND side = 'YES' "
            "AND model_prob IS NOT NULL AND market_price IS NOT NULL",
            (station_icao,),
        ).fetchall()
    return [(date.fromisoformat(r[0]), int(r[1]), r[2], float(r[3]), float(r[4])) for r in rows]
```

- [ ] **Step 4: Create `day_ahead_report.py`**

```python
"""
day_ahead_report.py -- WSSS plan step 6a: is a station priced wrong the evening
before? Binary Brier per bucket (YES side), model vs market, on the LATEST
evening recording per (day, bucket), against the bucket the day settled into.

Answer is YES only if model Brier < market Brier in BOTH halves and paired
bootstrap P >= 0.95 on the full window. Read-only.

Run: python day_ahead_report.py --station WSSS
"""
import argparse
import statistics
from datetime import date

import config
import storage
from backtest import resolution
from backtest.observed_half_life import _paired_bootstrap


def halves(days):
    days = sorted(days)
    return days[:len(days) // 2], days[len(days) // 2:]


def score(icao):
    st = config.get_station(icao)
    truth = {o.target_date: o.max_temp_c
             for o in storage.load_observations_since(icao, date(2000, 1, 1))
             if o.source == st.resolution_grade_source}
    latest = {}
    for day, bucket, gen, model, market in storage.load_day_ahead_rows(icao):
        key = (day, bucket)
        if key not in latest or gen > latest[key][0]:
            latest[key] = (gen, model, market)
    per_day = {}
    for (day, bucket), (_, model, market) in latest.items():
        if day not in truth:
            continue
        settled = resolution.bucket_for_temp(truth[day], st.bucket_min_c, st.bucket_max_c,
                                             edge_mode=st.bucket_edge_mode)
        hit = 1.0 if bucket == settled else 0.0
        per_day.setdefault(day, []).append(((model - hit) ** 2, (market - hit) ** 2))
    return {d: (statistics.fmean(m for m, _ in v), statistics.fmean(k for _, k in v))
            for d, v in sorted(per_day.items())}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--station", default="WSSS")
    icao = ap.parse_args().station.upper()
    scored = score(icao)
    first, second = halves(scored)
    for label, days in (("full", list(scored)), ("1st", first), ("2nd", second)):
        if not days:
            print(f"{label}: no settled days")
            continue
        model = {d: scored[d][0] for d in days}
        market = {d: scored[d][1] for d in days}
        p, n = _paired_bootstrap(market, model)
        print(f"{icao} {label}: n={n} model {statistics.fmean(model.values()):.4f} "
              f"market {statistics.fmean(market.values()):.4f} P(model better)={p}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `python -m pytest tests/test_day_ahead_report.py tests/test_day_ahead.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add storage.py day_ahead_report.py tests/test_day_ahead_report.py
git commit -m "day_ahead_report: evening-before model vs market Brier, in halves"
```

- [ ] **Step 7: Operator run, about 10-12, on a pulled prod copy.** Use the same `DB_PATH` one-liner as Task 3 Step 7, with `runpy.run_path('day_ahead_report.py', run_name='__main__')`. Record the output in ROOT `HANDOFF.md`.

---

### Task 7: Fast-recorder market-maker read (spec step 6b, ~10-13)

No code. `mm_replay.py` exists on local branch `feat/fast-recorder` (commits 4054581, db86b05), which is not on main.

- [ ] **Step 1: Pull the recorder DB off the box and run the replay from that branch**

```bash
git worktree add ../../wf-fast-recorder feat/fast-recorder
cd ../../wf-fast-recorder/weather-forecast
python mm_replay.py --recorder-db <pulled fast_recorder.sqlite3> --trading-db C:/Users/user/polyweather-backups/replay.sqlite3
```

- [ ] **Step 2:** Record the WSSS rows in ROOT `HANDOFF.md`. Don't re-test the late-day METAR idea, because it already answered no (memory `late-day-observation-edge`). This read feeds the Task 11 decision in spec step 12.

---

### Task 8: Prove redemption (spec step 7, operator, any time with approval)

**Files:**
- Modify: `redeem_abi_probe.py` (docstring), `docs/runbooks/operations.md:85-87`. Both still describe the one-element amount array that 65c0b65 already fixed.
- Modify later: `config.py:4724` (`REDEMPTION_PROVEN_TX`), `tests/test_live_rearm_gate.py` (the shipped-None assertion).

- [ ] **Step 1: Fix the stale text**

Change both passages to say that `redeemPositions(bytes32,uint256[])` takes `[yesAmount, noAmount]` and that `redeem.py` sends `[n, 0]` for YES and `[0, n]` for NO (65c0b65). Commit: `docs: redeem ABI text matches 65c0b65`.

- [ ] **Step 2: Operator decision, required.** On 2026-09-03 the operator decided not to fund the EOA with POL, and without gas `redeem.py --execute` exits 2. Ask the operator in chat to reverse that decision with a stated amount. Stop here without a clear yes.

- [ ] **Step 3: The operator funds the wallet, then does a dry run on the box**

```bash
python redeem.py --list --station WSSS
```

Expected: at least one row under `redeemable`. If there is none, the proof waits for the next winning position.

- [ ] **Step 4: The operator redeems exactly one position**

```bash
POLYMARKET_LIVE_TRADING=true python redeem.py --execute --token <one redeemable token>
```

Expected: the simulation passes, a tx hash is printed, and the USDC balance rises by about `size_shares`. If the simulation fails, stop. That is the unverified ABI or EIP-712 layout, and it needs a separate debugging session. Don't retry blindly.

- [ ] **Step 5: Record the proof in config** (merge now, deploy with Phase 2)

Set `REDEMPTION_PROVEN_TX = "<0x hash>"` at `config.py:4724`, with a comment giving the date and token. In `tests/test_live_rearm_gate.py`, change the shipped-value assertion from `is None` to `== "<0x hash>"`. Leave the `MATURITY_OVERRIDE == {}` assertion alone. Run `python -m pytest tests/test_live_rearm_gate.py -v`, which should PASS. Commit: `config: REDEMPTION_PROVEN_TX recorded (<date>)`.

This alone does not re-arm anything. The box still runs `--mode paper`, and maturity still gates.

---

### Task 9: ~10-04 checkpoint and beats-market re-run (spec step 2)

No code. Run it between 10-04 and 10-05, before the freeze.

- [ ] **Step 1: Checkpoint on the box** (read-only)

```bash
python cohort_monitor.py --station WSSS
python cohort_monitor.py --by station
```

Record the trailing-30 net price edge, the station-days count, and the kill verdict (FIRED / holding / NO VERDICT) in ROOT `HANDOFF.md`.

- [ ] **Step 2: Re-run beats-market on current code, on the box** (its HEAD must equal deployed code)

```bash
python -m backtest.cli run --station WSSS --from 2026-08-06 --to 2026-10-04
python -c "import config; config.print_maturity_report('WSSS')"
```

Expected: `beats_market` no longer says "run was produced by 16a0949f". It is expected to FAIL on model ≥ market, which is fine. If it fails for fewer than 20 scored entries (`brier_n`), record that instead. Keep the default `--out`, or the checker won't find the run.

---

### Task 10: Phase 2 rollout, from 11-03, one change per two weeks (spec steps 8–10)

Each sub-step is one deploy followed by a two-week watch with `cohort_monitor.py --station WSSS`.

- [ ] **Step 1 (11-03):** Merge `feat/wsss-phase1` (Tasks 1, 2, 6, 8 text) to main. The only config effects are the bucket window (non-trading) and the NEA date. If Task 3 passed, add the winning `FORECAST_SOURCES_EXCLUDED_BY_STATION["WSSS"]` entry in this same deploy, because it is the first input fix and the other two are the spec's "bundle with the first deploy". Deploy with `deploy/deploy_daemon.sh`.
- [ ] **Step 2 (~11-17):** If Task 4 passed and was re-confirmed about 11-30, set `OBSERVED_HALF_LIFE_DAYS` to the winning value. This slot moves to about 12-01 because of the re-confirmation.
- [ ] **Step 3 (after the input fixes have had two weeks):** Only if Task 5 passed, change `LOTTERY_PROFIT_TAKE_PCT` for paper. Live already holds to settlement once redemption is proven.
- [ ] **Step 4:** If any change fails its two-week watch, revert it alone and go on to the next.

---

### Task 11: Live decision point, mid-December (spec steps 11–13)

A checklist. Re-arm only if every box is ticked, and the operator gives the go-ahead in chat.

- [ ] `REDEMPTION_PROVEN_TX` is set (Task 8).
- [ ] The 09-24 audit gates are closed: settlement reconciliation and the watchdog.
- [ ] `config.print_maturity_report('WSSS')` passes with `MATURITY_OVERRIDE == {}`, including beats-market on ≥ 20 scored entries.
- [ ] `cohort_monitor.py --station WSSS`: trailing-30 net price edge > 0 with ≥ 30 station-days.
- [ ] If every gate closes except beats-market, WSSS stays paper, and the Task 6/7 recorder results decide on market-making.
- [ ] If the edge stays < 0 for 30 trading days after Task 10, add WSSS to `FORCE_COLLECTION_ONLY_STATIONS` (the WMKK stop rule).
