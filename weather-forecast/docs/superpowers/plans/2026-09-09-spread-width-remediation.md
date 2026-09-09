# Spread-Width Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the model pricing a width it does not have — in both directions: `SPREAD_FLOOR_C` blinding accurate stations, and the error-width gate being unable to see 23 of 35 stations.

**Architecture:** Two independent defects, one shared cause (the priced spread and the measured residual are different numbers). Task 1 is a safety stop needing no new code. Task 2 makes the pricing path use the same lag-aware residual the gate already uses. Task 3 lets a well-determined measurement step past the floor. Task 4 corrects the reasoning in `config.py` that will otherwise re-cause this.

**Tech Stack:** Python 3, sqlite3, pytest. No new dependencies. **numpy/scipy are NOT on the EC2 box** — see the `p3-6-calibrated-sizing` memory.

**Spec:** No separate spec doc. The findings are in memory: `spread-floor-underconfidence.md` (primary), `rpll-stopped-error-spread.md`, `ensemble-spread-tier-defect.md`, `exit-rules-are-the-whole-loss.md` (2026-09-09 section). Every measurement below was taken off the live EC2 DB on 2026-09-09.

## Global Constraints

- **Nothing here may widen real-money blast radius.** WSSS is the only live-armed station still opening positions; RCSS is already stopped by the width gate.
- **Every trading-path change needs a replay before merge** (`backtest/compare.py`), and the replay is unpaired and weak — see the caveat in `exit-rules-are-the-whole-loss.md`. A replay that disagrees with the ledger blocks the merge.
- **Deploy with `deploy_daemon.sh`, never a bare `git pull`** (frozen dashboard copies) — see the `ec2-deployment` memory.
- **Do not run the test suite on the box** — `test_no_fd_leak.py` writes the real DB.
- `MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE = 15` and `MAX_ERROR_RMSE_PER_BUCKET = 1.0` are deliberate, documented values with out-of-sample evidence behind them. Tasks 1 and 3 work *around* them; neither may be edited without its own argument.

---

## Measured state, 2026-09-09 (the numbers every task argues from)

**The error-width gate can only see 12 of 35 stations.**

| gate verdict | stations |
|---|---|
| PASSING (ratio <= 1.0) | WSSS 0.59, ZSPD 0.76, WMKK 0.80, VHHH 0.82 |
| STOPPED (ratio > 1.0) | RPLL 1.49, RJTT 1.38, RCSS 1.32, ZGGG 1.32, RKSI 1.22, ZGSZ 1.20, ZBAA 1.11, RKPK 1.01 |
| **WAITING — gate is blind** | all 15 Americas (n_res 4-5), all 6 Europe (n_res 9), OPKC (0) |

**Six of the blind stations already measure wider than their own bucket** (naive sd, so weaker than the gate's own statistic, but it is what exists today):

| station | naive sd | bucket (C) | sd / bucket | n pairs |
|---|---|---|---|---|
| SBGR | 2.282 | 1.000 | **2.282** | 10 |
| KSEA | 1.256 | 1.111 | 1.131 | 9 |
| KMIA | 1.218 | 1.111 | 1.096 | 10 |
| MMMX | 1.160 | 1.000 | 1.160 | 10 |
| KHOU | 1.113 | 1.111 | **1.002** — marginal | 10 |
| CYYZ | 1.010 | 1.000 | **1.010** — marginal | 10 |

Re-verified 2026-09-09 04:59 UTC. SBGR and CYYZ trade a 1.0C axis, not the 2F axis most
Americas stations use, so the ratio is the raw sd. KHOU (1.002) and CYYZ (1.010) sit
within sampling noise of the 1.0 threshold at n=10 and are the first two Task 5 should
re-check.

Europe crosses `n_res = 15` in roughly 6 days (~2026-09-15), the Americas in roughly 10 (~2026-09-19). **Until then these stations trade unguarded.**

**The floor binds on 8 of 34 scored stations** (naive sd vs `SPREAD_FLOOR_C = 0.7`):

| station | naive sd | inflation | lag-aware RMSE |
|---|---|---|---|
| EDDM | 0.452 | +55% | not yet computable (n_res 9) |
| EPWA | 0.508 | +38% | not yet computable (n_res 9) |
| KSFO | 0.547 | +28% | not yet computable (n_res 5) |
| LEMD | 0.604 | +16% | not yet computable (n_res 9) |
| **WSSS** | 0.624 | **+12%** | **0.589 -> +19%, n_res 34** |
| KATL | 0.637 | +10% | not yet computable (n_res 5) |
| KDAL | 0.643 | +9% | not yet computable (n_res 5) |
| ZSPD | 0.696 | +1% | 0.758 — floor does **not** bind |

**ZSPD is the warning.** Its lag-aware residual (0.758) is WIDER than its naive sd (0.696): correction lag adds width. So the naive sd is not a safe basis for standing the floor down — only the lag-aware number is. **WSSS is the only station where that number exists today**, and it says the floor inflates a 0.589 residual to 0.700.

**Evidence the inflation costs money** (EDDM + MMMX, target dates 09-02..09-08): on the bucket that actually settled, model_prob was below market price on **10 of 10 days**, mean −0.34. All 7 `NO` bets were against the bucket that hit. EDDM is 0-for-11 on `NO` all time (23 trades, 9% win rate, −$85.09). EDDM's priced sd pins to **exactly 0.700** by least squares on multi-leg scan cycles (residual 0.000000), and on 09-02 its centre landed **exactly** on the settled bucket and both legs still lost.

---

### Task 1: Stop the six unguarded wide stations by name

**Rationale:** The gate already encodes the decision ("error wider than the bucket means open nothing"). These six satisfy it on the only statistic available and cannot be caught for another 6-10 days. `FORCE_COLLECTION_ONLY_STATIONS` exists for exactly this: RPLL was named because a measurement was obvious before a gate existed. **This is an operator decision, not a proof** — n is 9-10, and a naive sd at n=10 carries roughly +/-24% relative error. Say so in the commit message.

**Files:**
- Modify: `config.py` — `FORCE_COLLECTION_ONLY_STATIONS` (currently `{"RPLL"}`)
- Test: `tests/test_error_width_gate.py`

**Interfaces:**
- Consumes: `config.force_collection_only(station_icao) -> bool` (exists)
- Produces: nothing new

- [ ] **Step 1: Write the failing test**

```python
def test_unguarded_wide_stations_are_named_until_the_gate_can_see_them():
    """
    Six stations measured wider than their own bucket on 2026-09-09 while
    sitting under MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE, so the gate returns
    None for them and cannot stop them. They are named until it can.
    Remove a name only when calibration.error_width_ratio() returns a
    number for it that passes.
    """
    for icao in ("SBGR", "KSEA", "KMIA", "MMMX", "KHOU", "CYYZ"):
        assert config.force_collection_only(icao), icao
    assert config.force_collection_only("RPLL"), "RPLL must not be dropped"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_error_width_gate.py -k unguarded -v`
Expected: FAIL with `assert False` on `SBGR`

- [ ] **Step 3: Make the change**

```python
FORCE_COLLECTION_ONLY_STATIONS = {
    "RPLL",
    # 2026-09-09. Measured wider than their own bucket (naive sd, n=9-10)
    # while under MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE, so the error-width
    # gate returns None for them and cannot stop them for another 6-10
    # days. Named on the SAME rule the gate encodes, not a new one.
    # sd/bucket ratios: SBGR 2.282, MMMX 1.160, KSEA 1.131, KMIA 1.096,
    # CYYZ 1.010, KHOU 1.002 -- the last two marginal at n=10.
    # THIS IS AN OPERATOR DECISION ON A THIN SAMPLE. A naive sd at n=10
    # carries ~24% relative error and is not the lag-aware statistic the
    # gate uses. Re-check each name once error_width_ratio() returns a
    # number for it; drop any that passes. See Task 5 of
    # docs/superpowers/plans/2026-09-09-spread-width-remediation.md.
    "SBGR", "KSEA", "KMIA", "MMMX", "KHOU", "CYYZ",
}
```

- [ ] **Step 4: Run the test and the gate suite**

Run: `python -m pytest tests/test_error_width_gate.py tests/test_collection_gate_override.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_error_width_gate.py
git commit -m "Name six stations the error-width gate cannot yet see"
```

Commit body:

```
MMMX went 0-for-5 in the week to 09-08 with sd 1.160 against a 1.0C
bucket. The gate added 2026-09-03 would stop it, but returns None below
15 scored pairs and MMMX has 5. Five more stations sit in the same
position, SBGR at 2.282 worst. Named on the gate's own rule until the
gate can see them. Thin sample, operator decision, re-check each.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 2: Price on the lag-aware residual, not the naive sd

**Rationale:** `calibration.measured_error_spread()` — feeding the priced spread at `calibration.py:527` — takes the sd about the sample's own mean, i.e. what would be left if the bias correction were perfect and instantaneous. `calibration.corrected_error_rmse()` replays the correction the entry path really had. They disagree in **both** directions (WSSS 0.624 -> 0.589; ZSPD 0.696 -> **0.758**). The gate already trusts the second one. The pricing path should use the same number, so a station's priced width and its gate verdict cannot be computed from different statistics.

**This task changes no station's behaviour where the floor binds** — it only changes which number the floor is applied to. It is a prerequisite for Task 3 and must ship and be observed first.

**Files:**
- Modify: `calibration.py:520-530` (the `measured_error` tier in `estimate_std_dev`)
- Test: `tests/test_cycle_calibration.py`

**Interfaces:**
- Consumes: `calibration.corrected_error_rmse(station_icao) -> (rmse_c | None, n_scored)` (exists)
- Produces: `estimate_std_dev(...)` gains source string `"corrected_error"`; `"measured_error"` remains as the fallback below 15 pairs. **`config.LOW_CONFIDENCE_SPREAD_SOURCES` must NOT gain `"corrected_error"`** — it is station-specific, and marking it low-confidence would make every entry clear a doubled edge bar.

- [ ] **Step 1: Write the failing tests**

```python
def test_priced_spread_prefers_the_lag_aware_residual(monkeypatch):
    """
    The gate scores a station on corrected_error_rmse(). Pricing must use
    the same statistic, or a station is gated on one number and traded on
    another. ZSPD is the case that matters: its lag-aware residual is
    WIDER (0.758) than its naive sd (0.696), so this is not a licence to
    narrow -- it is a licence to be right.
    """
    monkeypatch.setattr(calibration, "corrected_error_rmse",
                        lambda icao: (0.758, 29))
    monkeypatch.setattr(calibration, "measured_error_spread",
                        lambda icao: (0.696, 29))
    sd, source = calibration.estimate_std_dev(
        [], [], station_icao="ZSPD", allow_measured=True)
    assert source == "corrected_error"
    assert sd == 0.76


def test_priced_spread_falls_back_to_naive_sd_below_the_pair_floor(monkeypatch):
    """
    corrected_error_rmse() returns None under 15 scored pairs. That must
    read as "use the older tier", never as "no measurement" -- Europe and
    the Americas are all below the floor today and must keep pricing.
    """
    monkeypatch.setattr(calibration, "corrected_error_rmse",
                        lambda icao: (None, 9))
    monkeypatch.setattr(calibration, "measured_error_spread",
                        lambda icao: (0.452, 14))
    sd, source = calibration.estimate_std_dev(
        [], [], station_icao="EDDM", allow_measured=True)
    assert source == "measured_error"
    assert sd == 0.7  # SPREAD_FLOOR_C still binds here; Task 3 changes that
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cycle_calibration.py -k lag_aware -v`
Expected: FAIL with `assert 'measured_error' == 'corrected_error'`

- [ ] **Step 3: Implement**

Replace the `measured_error` tier at `calibration.py:525-528`:

```python
    if station_icao:
        # THE LAG-AWARE RESIDUAL FIRST. corrected_error_rmse() replays the
        # bias correction the entry path really had on each day;
        # measured_error_spread() takes the sd about the sample's own mean,
        # i.e. what would be left if the correction were perfect and
        # instant. The error-width GATE already scores stations on the
        # former, and a station gated on one statistic while priced on the
        # other is two different claims about one forecast. Measured
        # 2026-09-09: WSSS 0.624 -> 0.589 but ZSPD 0.696 -> 0.758, so this
        # is not a narrowing change -- it corrects in both directions.
        corrected, _ = corrected_error_rmse(station_icao)
        if corrected is not None:
            return _clamp_spread(corrected, station_icao), "corrected_error"

        measured, _ = measured_error_spread(station_icao)
        if measured is not None:
            return _clamp_spread(measured, station_icao), "measured_error"
```

- [ ] **Step 4: Verify the new source is not treated as low-confidence**

Run: `python -c "import config; assert 'corrected_error' not in config.LOW_CONFIDENCE_SPREAD_SOURCES; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS with no regressions. Record the test count.

- [ ] **Step 6: Replay before committing**

Run: `python -m backtest.compare --since 2026-08-17`
Expected: record exits-off and armed totals for both arms. **The only four stations whose priced number changes today are WSSS, ZSPD, WMKK and VHHH** — every other station is under 15 pairs and unchanged. A materially worse replay blocks the merge.

- [ ] **Step 7: Commit**

```bash
git add calibration.py tests/test_cycle_calibration.py
git commit -m "Price the spread on the same residual the gate scores"
```

Commit body:

```
estimate_std_dev's measured tier used the sd about the sample mean while
the error-width gate uses corrected_error_rmse, which replays the bias
correction each day actually had. The two disagree in both directions
(WSSS 0.624->0.589, ZSPD 0.696->0.758). Gating a station on one and
trading it on the other is two claims about one forecast.

Falls back to the old tier under 15 scored pairs, so Europe and the
Americas are unchanged today.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 3: Let a well-determined residual stand the floor down

**Rationale:** `SPREAD_FLOOR_C = 0.7` exists because a too-narrow spread makes the model look certain and inflates edge. That is real. But the floor is unconditional, so it also fires on a station that has *measured* itself narrower — and on a bucket market with a `NO` side the cost is not "missed trades", it is manufactured `NO` bets against the favourite. WSSS is the one station where the lag-aware residual exists and sits under the floor (0.589, n_res 34).

**Ship this ONLY after Task 2 is deployed and observed for a full settlement cycle.** This is the task that actually changes what the book buys.

**Files:**
- Modify: `config.py` — add `SPREAD_FLOOR_MIN_PAIRS`, extend the `SPREAD_FLOOR_C` comment
- Modify: `calibration.py:250-265` — `_clamp_spread` gains an opt-out
- Test: `tests/test_cycle_calibration.py`

**Interfaces:**
- Consumes: `config.SPREAD_FLOOR_C`, `config.SPREAD_FLOOR_MIN_PAIRS`, `calibration.corrected_error_rmse`
- Produces: `_clamp_spread(value, station_icao=None, well_measured=False)` — the third argument defaults False so **every existing caller keeps today's behaviour**, including `spread_tier_brier.py`'s four call sites.

- [ ] **Step 1: Write the failing tests**

```python
def test_floor_stands_down_for_a_well_measured_station():
    """
    WSSS measures 0.589 over 34 scored pairs. Flooring that to 0.700 is a
    19% claim of uncertainty the station does not have, and on a bucket
    market that claim is what buys NO against the favourite.
    """
    assert calibration._clamp_spread(0.589, "WSSS", well_measured=True) == 0.59


def test_floor_still_binds_without_a_well_measured_flag():
    """The default is unchanged. Every legacy caller keeps the floor."""
    assert calibration._clamp_spread(0.589, "WSSS") == 0.7
    assert calibration._clamp_spread(0.589, "WSSS", well_measured=False) == 0.7


def test_floor_still_binds_below_the_pair_threshold(monkeypatch):
    """
    EDDM measures 0.452 but on 9 scored pairs, under
    SPREAD_FLOOR_MIN_PAIRS. A thin sd is exactly what the floor is for.
    """
    monkeypatch.setattr(calibration, "corrected_error_rmse",
                        lambda icao: (None, 9))
    sd, _ = calibration.estimate_std_dev(
        [], [], station_icao="EDDM", allow_measured=True)
    assert sd == 0.7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cycle_calibration.py -k floor -v`
Expected: FAIL with `_clamp_spread() got an unexpected keyword argument 'well_measured'`

- [ ] **Step 3: Add the threshold to config.py**

```python
# How many SCORED residuals a station needs before its own measurement is
# allowed past SPREAD_FLOOR_C. Same value as
# MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE and for the same reason: standing a
# safety floor down is the same class of act as stopping a station, and
# both should need the same evidence. A station under this keeps the floor.
SPREAD_FLOOR_MIN_PAIRS = 15
```

- [ ] **Step 4: Implement the opt-out**

```python
def _clamp_spread(value: float, station_icao: str = None,
                  well_measured: bool = False) -> float:
    """
    Hold a spread inside its REGION's band -- see SPREAD_FLOOR_C and
    config.REGION_SPREAD_CEILING_C.

    `well_measured` is the floor's ONLY opt-out and defaults False, so
    every caller written before 2026-09-09 keeps the floor. It is set only
    where the station's own lag-aware residual cleared
    config.SPREAD_FLOOR_MIN_PAIRS -- see estimate_std_dev.
    """
    floored = value if well_measured else max(value, config.SPREAD_FLOOR_C)
    if station_icao is None:
        return round(min(floored, config.SPREAD_CEILING_C), 2)
    ceiling = config.region_spread_ceiling_c(station_icao)
    if ceiling is None:
        return round(floored, 2)
    return round(min(floored, ceiling), 2)
```

- [ ] **Step 5: Pass the flag from the corrected tier**

In `estimate_std_dev`, replace the `corrected_error` return added in Task 2:

```python
        corrected, n_scored = corrected_error_rmse(station_icao)
        if corrected is not None:
            return (
                _clamp_spread(corrected, station_icao,
                              well_measured=n_scored >= config.SPREAD_FLOOR_MIN_PAIRS),
                "corrected_error",
            )
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS. `spread_tier_brier.py`'s call sites do not pass the flag and must be unchanged.

- [ ] **Step 7: Measure the blast radius before replaying**

```bash
python -c "
import config, calibration
for s in sorted(config.STATIONS):
    r, n = calibration.corrected_error_rmse(s)
    if r is not None and n >= config.SPREAD_FLOOR_MIN_PAIRS and r < config.SPREAD_FLOOR_C:
        print(f'{s}: {r:.3f} was being floored to {config.SPREAD_FLOOR_C}')
"
```

Expected on 2026-09-09 data: **WSSS only.** If more than two stations appear, stop and re-read — the change is wider than this plan argued.

- [ ] **Step 8: Replay**

Run: `python -m backtest.compare --since 2026-08-17`
Expected: WSSS is the station that moves. **WSSS carries the book** (see the `station-performance-divergence` memory), so a worse WSSS replay blocks this outright.

- [ ] **Step 9: Commit**

```bash
git add config.py calibration.py tests/test_cycle_calibration.py
git commit -m "Let a well-measured station price its own width"
```

Commit body:

```
SPREAD_FLOOR_C is unconditional, so it fires on stations that have
measured themselves narrower than it. On a bucket market with a NO side
that is not a missed trade, it is a manufactured NO against the
favourite: EDDM is 0-for-11 on NO all time, and on 09-02 its centre
landed exactly on the settled bucket and both legs still lost.

The opt-out needs 15 scored residuals, the same evidence the error-width
gate needs to stop a station. Defaults off, so every legacy caller keeps
the floor. Blast radius today: WSSS only.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 4: Correct the reasoning in config.py

**Rationale:** `config.py:4012` says *"A too-wide spread only costs missed trades."* That sentence is why the floor is unconditional, and it is false on a two-sided bucket market. Leaving it in place means the next person re-derives the same design.

**Files:**
- Modify: `config.py:4008-4014`

- [ ] **Step 1: Replace the comment**

```python
# A too-narrow spread makes the model look certain, which inflates the gap
# against market price, which the entry gates will happily size into.
#
# CORRECTED 2026-09-09: the old note here said "a too-wide spread only
# costs missed trades". THAT IS FALSE ON A TWO-SIDED BUCKET MARKET. A
# too-wide spread does not skip trades, it MANUFACTURES them -- it
# underprices the market's favourite bucket, which the entry path reads as
# a NO-side edge on the bucket most likely to win. Measured over
# 2026-09-02..08 at EDDM and MMMX: the model priced the bucket that
# actually settled BELOW the market on 10 of 10 days (mean -0.34), all 7
# NO bets were against the bucket that hit, and EDDM is 0-for-11 on NO all
# time. The floor is wrong in BOTH directions, not conservative in one.
# See SPREAD_FLOOR_MIN_PAIRS for the opt-out that bounds it.
SPREAD_FLOOR_C = 0.7
```

- [ ] **Step 2: Verify nothing reads the comment text**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "Correct the SPREAD_FLOOR_C rationale: too-wide is not free"
```

---

### Task 5: Re-check the named stations once the gate can see them

**Rationale:** Task 1's names are an operator decision on a thin sample and must not become permanent by inertia. Europe crosses 15 scored pairs around 2026-09-15, the Americas around 2026-09-19.

**Files:** `config.py`, `tests/test_error_width_gate.py`

- [ ] **Step 1: On or after 2026-09-19, run the gate for every named station**

```bash
python -c "
import config, calibration
for s in sorted(config.FORCE_COLLECTION_ONLY_STATIONS):
    r = calibration.error_width_ratio(s)
    rmse, n = calibration.corrected_error_rmse(s)
    if r is None:
        verdict = 'still blind'
    elif r <= config.MAX_ERROR_RMSE_PER_BUCKET:
        verdict = 'PASSES - drop the name'
    else:
        verdict = 'confirmed wide'
    print(f'{s:6} ratio={r} n={n} -> {verdict}')
"
```

- [ ] **Step 2: Drop any name whose measured ratio passes**, keeping RPLL (a standing operator decision independent of the measurement, per its own note in `config.py`). Update the Task 1 test to match.

- [ ] **Step 3: Commit** with the measured ratios in the message.

---

## Explicitly out of scope

- **Re-testing the exit rules.** `HOLD_TO_SETTLEMENT_MODES` ended paper exits after 2026-09-02, so no amount of waiting produces exit data. Re-testing needs arming live, the replay arm, or temporarily emptying the tuple on a subset — a separate decision with real-money blast radius. See the 2026-09-09 section of the `exit-rules-are-the-whole-loss` memory.
- **Touching `MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE` or `MAX_ERROR_RMSE_PER_BUCKET`.** Both are documented decisions with out-of-sample evidence behind them. Task 1 works around the first by naming stations; it does not weaken it.
- **KLAX's +2.482C bias.** Spotted while measuring (n=10, by far the largest bias in the registry) and not investigated. Worth its own look.
- **The entry-selection defect.** `entry-edge-decay-2026-09-03` says raw_edge does not rank and net_ev ranks worst-first. Nothing here touches that; a sharper spread feeds the same ranker.
