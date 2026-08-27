# Americas Market Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register 15 American cities as a capital-isolated `americas` region, and give the codebase a per-station bucket axis so it can price and settle the Fahrenheit, two-degree-wide markets 11 of them use.

**Architecture:** A new stdlib-only `bucket_axis.py` owns every conversion between a bucket key and a temperature. Six existing functions gain an optional `axis` argument defaulting to today's Celsius/step-1 axis, so all 20 existing stations are byte-for-byte unchanged. One invariant governs everything: **every temperature in this codebase is Celsius; only the bucket key and its bounds live in the market's unit.** The region half is a straight clone of the Europe mechanism, which is already generic.

**Tech Stack:** Python 3.12, stdlib only for the new module (`math`, `dataclasses`, `typing`). SQLite via `storage.py`. pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-americas-market-isolation-design.md` — read it before Task 1. The plan argues from the spec; where this plan and the spec disagree, the spec wins and the disagreement is a bug in this plan.

## Global Constraints

- **Run tests from the code directory:** `cd weather-forecast && python -m pytest tests -q`. There is no pytest.ini; `tests/conftest.py` does the `sys.path` insert.
- **NEVER run the test suite on the EC2 box.** `tests/test_no_fd_leak.py` writes to the real production database. Local only.
- **Byte-for-byte constraint:** no change may alter behaviour for any station whose axis is `("C", 1)`. That is all 13 Asia + 7 Europe stations. Task 7 is the proof; every task before it must keep that proof reachable.
- **New module is stdlib-only.** `bucket_axis.py` may import `math`, `dataclasses`, `typing` and nothing from this project. It is imported by `probability.py`, `backtest/resolution.py`, `bucket_bias.py` and `market_discovery.py`; any project import creates a cycle.
- **Every new `axis` parameter is keyword-only and defaults to `None` or `AXIS_C1`.** Positional insertion would silently reorder existing call sites.
- **Line numbers in this repo are not stable.** Anchor edits to symbol names (`def bucket_for_temp`), not to line numbers. Cited numbers are as of 2026-08-27 and may drift.
- **Commit after every task.** Never squash phases together — the phase boundaries are the revert points the spec's build order depends on.
- **Branch:** `feat/americas-market-isolation`, already created, spec already committed as `6a89aba`.
- **Units in identifiers:** a `_c` suffix on a return value is a promise that the value is Celsius. Any function this plan touches keeps that promise or is renamed. `bucket_c` is exempt — it is a key, not a temperature; see spec §5.
- **Where this plan writes `...` in a registry entry, that is research-gated, not an omission.** Polymarket and `gamma-api.polymarket.com` are network-blocked from this environment, so no ICAO, slug, lat/lon, bucket window or settlement source can be known at planning time. Tasks 14 and 17 each begin with a research step whose named output document supplies every one of those values. The placeholder ICAOs in the test constants (`AMERICAS_CELSIUS`, `AMERICAS_FAHRENHEIT`) are the plan's best guesses and **must** be replaced from research — do not assume `KLGA` is the station NYC's market actually names.

---

## File Structure

**Created:**
- `weather-forecast/bucket_axis.py` — the axis value object. Sole owner of key↔temperature arithmetic.
- `weather-forecast/tests/test_bucket_axis.py` — axis unit tests + the cross-station property sweep.
- `weather-forecast/tests/test_americas_region.py` — region isolation tests for `americas`.
- `docs/superpowers/research/2026-08-27-americas-station-facts.md` — per-station research output (Tasks 13, 15).

**Modified:**
- `models.py` — two `StationConfig` fields + docstring correction.
- `probability.py` — `_bucket_interval`, `bucket_probabilities` (axis-aware, fail-closed).
- `backtest/resolution.py` — `bucket_for_temp` (axis-aware). Note: this is the **live** settlement path too; `position_manager.py` imports it as `settlement`.
- `bucket_bias.py` — `bucket_midpoint_c` (returns Celsius), `quantization_stderr_c` caller, `derived_bias_stats` unit assertion, `load_settled_buckets` unpack.
- `market_discovery.py` — Fahrenheit parse branch, sign capture, per-unit plausibility band, `derive_bucket_bounds(step=)`.
- `storage.py` — `settled_buckets` gains `bucket_unit` / `bucket_step`.
- `calibration.py` — `_clamp_spread` becomes region-aware.
- `clients/metar_client.py` — DST-correct local-day window.
- `config.py` — five `REGION_*` dicts gain `americas`, per-region spread ceiling, 15 station entries.
- `ev_engine.py`, `pipeline.py`, `position_manager.py`, `executor.py`, `stop_loss_audit.py`, `promotion_dossier.py`, `backtest/engine.py`, `backtest/report.py` — pass an axis; render `axis.label(...)`.
- `deploy/generate_dashboard.py`, `deploy/generate_realmoney_dashboard.py`, `deploy/setup_dashboard.sh` — axis-aware rendering, `americas.html`.
- `tests/test_station_registry.py`, `tests/test_region_isolation.py` — two assertions each.

**Deviation from the spec's build order, deliberate:** spec §"Build order" puts defects (b) sign capture and (c) plausibility band in commit 2. This plan folds them into Task 5, because they are edits to the same function (`parse_bucket_label`) that Task 5 already rewrites, and splitting one function across two commits would mean writing it twice. Everything else follows the spec's order.

---

# PHASE 1 — The axis, provably a no-op

No new station and no new region in this phase. At the end of it the test suite must be green with zero behaviour change.

---

### Task 1: The `BucketAxis` value object

**Files:**
- Create: `weather-forecast/bucket_axis.py`
- Test: `weather-forecast/tests/test_bucket_axis.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `BucketAxis(unit, step, edge_mode)` with methods `to_axis(temp_c) -> float`, `to_celsius(axis_value) -> float`, `width_c() -> float`, `interval_c(key) -> Tuple[float, float]`, `key_for_temp_c(t_c, lo, hi) -> int`, `keys(lo, hi) -> List[int]`, `label(key, lo, hi) -> str`, property `is_default -> bool`. Module constants `UNIT_C = "C"`, `UNIT_F = "F"`, `AXIS_C1 = BucketAxis()`. Function `for_station(station) -> BucketAxis`.

- [ ] **Step 1: Write the failing test**

Create `weather-forecast/tests/test_bucket_axis.py`:

```python
"""
tests/test_bucket_axis.py

The bucket axis: what a bucket KEY means, in the market's own unit.

The governing invariant under test is that every temperature crossing
this module's boundary is Celsius, and only the key and its bounds live
in the market's unit.
"""
import math

import pytest

import bucket_axis
from bucket_axis import BucketAxis, AXIS_C1


class TestCelsiusAxisIsTodaysBehaviour:
    """The default axis must reproduce probability.py's historical formulas."""

    def test_half_up_interval_is_plus_minus_half(self):
        assert AXIS_C1.interval_c(31) == (30.5, 31.5)

    def test_floor_interval_is_b_to_b_plus_one(self):
        axis = BucketAxis(edge_mode="floor")
        assert axis.interval_c(33) == (33.0, 34.0)

    def test_key_for_temp_half_up_rounds_half_up_not_bankers(self):
        # round(30.5) is 30 under banker's rounding; the market says 31.
        assert AXIS_C1.key_for_temp_c(30.5, 25, 35) == 31
        assert AXIS_C1.key_for_temp_c(31.5, 25, 35) == 32

    def test_key_for_temp_floor_truncates(self):
        axis = BucketAxis(edge_mode="floor")
        assert axis.key_for_temp_c(33.9, 27, 37) == 33

    def test_key_is_clamped_into_the_catch_alls(self):
        assert AXIS_C1.key_for_temp_c(-50.0, 27, 37) == 27
        assert AXIS_C1.key_for_temp_c(500.0, 27, 37) == 37

    def test_width_is_one_degree(self):
        assert AXIS_C1.width_c() == 1.0

    def test_is_default(self):
        assert AXIS_C1.is_default
        assert BucketAxis(edge_mode="floor").is_default
        assert not BucketAxis(unit="F", step=2).is_default


class TestFahrenheitAxis:
    """NYC: 69F or below | 70-71F | ... | 86-87F | 88F or higher."""

    AXIS = BucketAxis(unit="F", step=2, edge_mode="half_up")
    LO, HI = 68, 88

    def test_eleven_keys_on_a_uniform_step_two_grid(self):
        assert self.AXIS.keys(self.LO, self.HI) == [
            68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88
        ]

    def test_interval_is_returned_in_celsius(self):
        lo_c, hi_c = self.AXIS.interval_c(70)
        # 69.5F .. 71.5F
        assert lo_c == pytest.approx((69.5 - 32) * 5 / 9)
        assert hi_c == pytest.approx((71.5 - 32) * 5 / 9)

    def test_interval_width_is_two_fahrenheit_degrees(self):
        assert self.AXIS.width_c() == pytest.approx(2 * 5 / 9)

    def test_key_for_a_celsius_reading(self):
        # 26.1C -> 78.98F -> displays 79F -> bucket "78-79"
        assert self.AXIS.key_for_temp_c(26.1, self.LO, self.HI) == 78

    def test_half_up_not_bankers_at_the_reachable_half_degrees(self):
        # 22.5C -> exactly 72.5F. floor(72.5+0.5)=73 -> bucket 72.
        # round(72.5)=72 under banker's -> also bucket 72 on THIS window,
        # which is why the bug hides here; the displayed-degree test below
        # is what actually pins it.
        assert self.AXIS.key_for_temp_c(22.5, self.LO, self.HI) == 72

    def test_labels_match_what_polymarket_prints(self):
        assert self.AXIS.label(68, self.LO, self.HI) == "69°F or below"
        assert self.AXIS.label(70, self.LO, self.HI) == "70-71°F"
        assert self.AXIS.label(86, self.LO, self.HI) == "86-87°F"
        assert self.AXIS.label(88, self.LO, self.HI) == "88°F or higher"


class TestCelsiusLabels:
    def test_labels_match_todays_markets(self):
        assert AXIS_C1.label(27, 27, 37) == "27°C or below"
        assert AXIS_C1.label(30, 27, 37) == "30°C"
        assert AXIS_C1.label(37, 27, 37) == "37°C or higher"


class TestValidation:
    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="unit"):
            BucketAxis(unit="K")

    def test_zero_step_raises(self):
        with pytest.raises(ValueError, match="step"):
            BucketAxis(step=0)

    def test_unknown_edge_mode_raises(self):
        with pytest.raises(ValueError, match="edge_mode"):
            BucketAxis(edge_mode="nearest")


class TestForStation:
    def test_a_station_without_the_new_fields_gets_the_default_axis(self):
        class Legacy:
            bucket_edge_mode = "half_up"

        assert bucket_axis.for_station(Legacy()) == AXIS_C1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'bucket_axis'`.

- [ ] **Step 3: Write `bucket_axis.py`**

Create `weather-forecast/bucket_axis.py`:

```python
"""
bucket_axis.py

PURPOSE
-------
What a bucket KEY means, for one market.

THE GOVERNING INVARIANT
-----------------------
    Every temperature in this codebase is Celsius.
    Only the bucket KEY and its BOUNDS live in the market's own unit.

Forecasts, std_dev, observations, midpoints, bias -- all Celsius, always.
This module is the ONE boundary where the market's unit is converted, and
every function here that returns a temperature returns Celsius.

WHY THIS EXISTS
---------------
Every market registered before 2026-08 was Celsius with whole-degree
buckets, and that assumption was spelled into an identifier (`bucket_c`).
Eleven of the fifteen American cities list Fahrenheit in two-degree
buckets. This is the same shape of latent assumption as
StationConfig.utc_offset_hours being a static int, and it is repaired the
same way: leave the field carrying the market's own datum alone, add a
descriptor carrying the general truth, route every semantic use through it.

DEPENDENCIES
------------
math, dataclasses, typing (standard library ONLY -- this module is
imported by probability.py, market_discovery.py, bucket_bias.py and
backtest/resolution.py, so any project import would create a cycle).
"""

import math
from dataclasses import dataclass
from typing import List, Tuple

UNIT_C = "C"
UNIT_F = "F"
_UNITS = (UNIT_C, UNIT_F)
_EDGE_MODES = ("half_up", "floor")


@dataclass(frozen=True)
class BucketAxis:
    """
    unit      -- the unit of the market's bucket LABELS, and therefore of
                 every bucket key and of bucket_min_c/bucket_max_c. NOT the
                 unit of any temperature.
    step      -- width of one listed bucket, in `unit` degrees.
    edge_mode -- how a raw reading maps onto a listed bucket:
                 "half_up" the source reports whole degrees, so the bucket
                           wins for any reading that rounds to a degree
                           inside it;
                 "floor"   the source reports 0.1 precision and the market
                           resolves to the range CONTAINING the reading.
    """

    unit: str = UNIT_C
    step: int = 1
    edge_mode: str = "half_up"

    def __post_init__(self):
        if self.unit not in _UNITS:
            raise ValueError(
                f"unknown bucket unit {self.unit!r} -- expected one of {_UNITS}. "
                f"Refusing to guess: a wrong unit mis-prices every bucket."
            )
        if not isinstance(self.step, int) or self.step < 1:
            raise ValueError(
                f"bucket step must be a positive int, got {self.step!r}."
            )
        if self.edge_mode not in _EDGE_MODES:
            raise ValueError(
                f"unknown bucket edge_mode {self.edge_mode!r} -- expected one of "
                f"{_EDGE_MODES} (see models.StationConfig.bucket_edge_mode)."
            )

    # --- unit conversion -------------------------------------------------

    def to_axis(self, temp_c: float) -> float:
        """Celsius -> this axis's unit."""
        if self.unit == UNIT_C:
            return temp_c
        return temp_c * 9 / 5 + 32

    def to_celsius(self, axis_value: float) -> float:
        """This axis's unit -> Celsius."""
        if self.unit == UNIT_C:
            return axis_value
        return (axis_value - 32) * 5 / 9

    def width_c(self) -> float:
        """How wide one listed bucket is, in DEGREES CELSIUS."""
        if self.unit == UNIT_C:
            return float(self.step)
        return self.step * 5 / 9

    @property
    def is_default(self) -> bool:
        """True for the Celsius whole-degree axis every pre-2026-08 market uses."""
        return self.unit == UNIT_C and self.step == 1

    # --- key <-> temperature ---------------------------------------------

    def interval_c(self, key: int) -> Tuple[float, float]:
        """
        The temperature interval bucket `key` covers, IN CELSIUS.

        A key is the bucket's LOWER EDGE in the axis unit, so:
            half_up  [key - 0.5, key - 0.5 + step)
            floor    [key,       key + step)
        Both reduce to probability.py's historical formulas at step == 1.
        """
        lower_axis = key - 0.5 if self.edge_mode == "half_up" else float(key)
        upper_axis = lower_axis + self.step
        return self.to_celsius(lower_axis), self.to_celsius(upper_axis)

    def key_for_temp_c(self, t_c: float, lo: int, hi: int) -> int:
        """
        The bucket key a Celsius reading falls in, clamped into [lo, hi]
        because the edge buckets are catch-alls.
        """
        if self.is_default:
            # SHORT-CIRCUIT, deliberately literal. The general branch below is
            # algebraically identical here, but keeping the original
            # expressions makes "unchanged for every existing station" a
            # property of the code rather than of an algebra argument.
            bucket = (
                math.floor(t_c)
                if self.edge_mode == "floor"
                else math.floor(t_c + 0.5)
            )
            return max(lo, min(hi, bucket))

        axis_value = self.to_axis(t_c)
        # The settlement source displays a whole degree in the axis unit.
        # floor(x + 0.5), never round(): round() is banker's rounding and
        # disagrees on exactly the half-degree values the bucket edges sit on.
        displayed = (
            math.floor(axis_value)
            if self.edge_mode == "floor"
            else math.floor(axis_value + 0.5)
        )
        key = lo + self.step * math.floor((displayed - lo) / self.step)
        return max(lo, min(hi, key))

    def keys(self, lo: int, hi: int) -> List[int]:
        """Every listed bucket key from lo to hi inclusive, on this axis's grid."""
        return list(range(lo, hi + 1, self.step))

    def label(self, key: int, lo: int, hi: int) -> str:
        """
        The label the market itself prints for this bucket.

        REQUIRED at every human-facing site. A key is the bucket's lower
        edge, so on a step-2 axis the bottom catch-all's key (68) is a
        number the market never prints ("69F or below"). Rendering the raw
        key with a hardcoded degree suffix is how a human ends up told to
        buy the wrong contract.
        """
        suffix = "°C" if self.unit == UNIT_C else "°F"
        if key <= lo:
            return f"{key + self.step - 1}{suffix} or below"
        if key >= hi:
            return f"{key}{suffix} or higher"
        if self.step == 1:
            return f"{key}{suffix}"
        return f"{key}-{key + self.step - 1}{suffix}"


AXIS_C1 = BucketAxis()
"""The axis every market registered before 2026-08 uses. The default everywhere."""


def for_station(station) -> BucketAxis:
    """
    The axis for a StationConfig. getattr with defaults so this works on
    any station-shaped object, including test doubles predating the fields.
    """
    return BucketAxis(
        unit=getattr(station, "bucket_unit", UNIT_C),
        step=getattr(station, "bucket_step", 1),
        edge_mode=getattr(station, "bucket_edge_mode", "half_up"),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add weather-forecast/bucket_axis.py weather-forecast/tests/test_bucket_axis.py
git commit -m "Add the bucket axis: what a key means, in the market's own unit"
```

---

### Task 2: `StationConfig.bucket_unit` and `bucket_step`

**Files:**
- Modify: `weather-forecast/models.py` (`class StationConfig`, after `bucket_edge_mode`)
- Test: `weather-forecast/tests/test_bucket_axis.py` (append)

**Interfaces:**
- Consumes: `bucket_axis.for_station` (Task 1).
- Produces: `StationConfig.bucket_unit: str = "C"`, `StationConfig.bucket_step: int = 1`. Every later task reads these.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_bucket_axis.py`:

```python
class TestStationConfigCarriesTheAxis:
    def test_defaults_are_the_celsius_whole_degree_axis(self):
        from models import StationConfig

        st = StationConfig(
            icao="TEST", display_name="Test", country="Testland",
            lat=0.0, lon=0.0, wunderground_slug="x/y/TEST",
            long_term_normal_max_c=30.0, official_client_key="wwis",
            polymarket_city_slug="test",
        )
        assert st.bucket_unit == "C"
        assert st.bucket_step == 1
        assert bucket_axis.for_station(st) == AXIS_C1

    def test_every_registered_station_is_on_the_default_axis_today(self):
        # Phase 1 registers no new station. This test is the tripwire that
        # says so, and Task 16 is where it is deliberately narrowed.
        import config

        for icao, st in config.STATIONS.items():
            assert bucket_axis.for_station(st).is_default, icao

    def test_a_fahrenheit_station_declares_it(self):
        from models import StationConfig

        st = StationConfig(
            icao="KLGA", display_name="LaGuardia", country="United States",
            lat=40.777, lon=-73.872, wunderground_slug="us/new-york/KLGA",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="nyc",
            bucket_unit="F", bucket_step=2, bucket_min_c=68, bucket_max_c=88,
        )
        axis = bucket_axis.for_station(st)
        assert axis == BucketAxis(unit="F", step=2, edge_mode="half_up")
        assert not axis.is_default
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q -k StationConfigCarries`
Expected: FAIL — `AttributeError: 'StationConfig' object has no attribute 'bucket_unit'`, and `TypeError: __init__() got an unexpected keyword argument 'bucket_unit'`.

- [ ] **Step 3: Add the fields**

In `weather-forecast/models.py`, inside `class StationConfig`, immediately after the `bucket_edge_mode: str = "half_up"` declaration and its comment block, insert:

```python
    # The MARKET's bucket axis. See bucket_axis.BucketAxis.
    #
    # bucket_unit is the unit of the market's bucket LABELS -- and therefore
    # of bucket_min_c/bucket_max_c above and of every bucket_c key. It is NOT
    # the unit of any temperature: forecasts, std_dev, observations, midpoints
    # and bias are Celsius everywhere, always.
    #
    # Defaults reproduce every market registered before 2026-08. The American
    # cities are the first exception: 11 of the 15 list Fahrenheit in
    # two-degree buckets ("70-71°F"), so they set ("F", 2).
    bucket_unit: str = "C"
    bucket_step: int = 1
```

Then correct the docstring on `bucket_min_c` / `bucket_max_c` — replace the existing comment block above them with:

```python
    # Sanity CROSS-CHECK bounds for this station's Polymarket bucket range,
    # EXPRESSED IN bucket_unit (below) -- the `_c` suffix is historical and
    # is NOT a claim that these are Celsius. bucket_axis.for_station() is
    # authoritative for what a bucket number means; the field name never is.
    #
    # NOT the source of truth on the trading path: Polymarket shifts a city's
    # 11-bucket window seasonally (Singapore moved 25-35 -> 27-37 between July
    # and August 2026), so live trading derives the real bounds from the
    # discovered token map each cycle and only logs when these drift.
    bucket_min_c: int = 25
    bucket_max_c: int = 35
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py tests/test_station_registry.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add weather-forecast/models.py weather-forecast/tests/test_bucket_axis.py
git commit -m "Give StationConfig a bucket axis; say plainly that _c is historical"
```

---

### Task 3: `probability.py` — axis-aware and fail-closed

**Files:**
- Modify: `weather-forecast/probability.py` (`_bucket_interval`, `bucket_probabilities`)
- Modify: `weather-forecast/ev_engine.py` (two `bucket_probabilities(` call sites)
- Modify: `weather-forecast/pipeline.py` (one call site)
- Modify: `weather-forecast/backtest/engine.py` (one call site)
- Test: `weather-forecast/tests/test_bucket_axis.py` (append)

**Interfaces:**
- Consumes: `bucket_axis.BucketAxis`, `AXIS_C1`, `for_station` (Task 1); `StationConfig.bucket_unit`/`bucket_step` (Task 2).
- Produces: `probability.bucket_probabilities(estimate, bucket_min, bucket_max, edge_mode="half_up", *, axis=None)`. When `axis is None` it resolves the estimate's own station and RAISES if that station is not on the default axis.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_bucket_axis.py`:

```python
class TestProbabilityIsAxisAware:
    """
    The highest-risk failure in this design is a DEFAULTED axis, not a wrong
    one. A missed call site prices a Fahrenheit market on a Celsius grid:
    all 11 buckets sit ~40 degrees above the distribution, the tail fold puts
    ~1.0 on the lowest and ~0.0 on the other ten, and ten model_prob-0.0
    buckets are ten NO sides at ~0.20 raw edge -- under MAX_PLAUSIBLE_RAW_EDGE,
    through every gate. It would size ten trades per cycle per station.
    """

    def _estimate(self, icao, mean=26.1, sd=1.0):
        from datetime import date
        from models import CalibratedEstimate

        return CalibratedEstimate(
            station_icao=icao, target_date=date(2026, 8, 27),
            central_estimate_c=mean, std_dev_c=sd, monsoon_phase="unknown",
        )

    def test_celsius_station_is_unchanged_when_no_axis_is_passed(self):
        import probability

        est = self._estimate("WSSS")
        got = probability.bucket_probabilities(est, 27, 37)
        assert [b.bucket_c for b in got] == list(range(27, 38))

    def test_fahrenheit_probabilities_are_computed_on_the_f_grid(self):
        import probability

        axis = BucketAxis(unit="F", step=2)
        est = self._estimate("KLGA", mean=26.1, sd=1.0)
        got = probability.bucket_probabilities(est, 68, 88, axis=axis)

        assert [b.bucket_c for b in got] == [
            68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88
        ]
        assert sum(b.probability for b in got) == pytest.approx(1.0, abs=1e-3)
        # 26.1C is 78.98F, so the mode must be the "78-79F" bucket.
        assert max(got, key=lambda b: b.probability).bucket_c == 78

    def test_it_raises_rather_than_pricing_an_f_market_on_a_c_grid(self, monkeypatch):
        import config
        import probability
        from models import StationConfig

        st = StationConfig(
            icao="KLGA", display_name="LaGuardia", country="United States",
            lat=40.777, lon=-73.872, wunderground_slug="us/new-york/KLGA",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="nyc", bucket_unit="F", bucket_step=2,
        )
        monkeypatch.setitem(config.STATIONS, "KLGA", st)

        with pytest.raises(ValueError, match="axis"):
            probability.bucket_probabilities(self._estimate("KLGA"), 68, 88)

    def test_an_unregistered_station_still_defaults(self):
        # Station-agnostic callers and old tests pass estimates for stations
        # that may not be registered. Those keep the legacy default.
        import probability

        got = probability.bucket_probabilities(self._estimate("NOPE"), 27, 37)
        assert len(got) == 11
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q -k ProbabilityIsAxisAware`
Expected: FAIL — `TypeError: bucket_probabilities() got an unexpected keyword argument 'axis'`.

- [ ] **Step 3: Rewrite the two functions**

In `weather-forecast/probability.py`, add to the imports:

```python
import bucket_axis
from bucket_axis import BucketAxis
```

Replace `_bucket_interval` entirely with:

```python
def _bucket_interval(bucket: int, axis: BucketAxis) -> tuple:
    """
    The temperature interval one bucket covers, IN CELSIUS, per the
    settlement source's own precision and the market's own bucket width.

      "half_up" -- the source reports WHOLE degrees (METAR/Wunderground),
                   so the market's "31°C" outcome wins for any reading
                   that rounds to 31: [30.5, 31.5).
      "floor"   -- the source reports 0.1°C and the market resolves to
                   the range CONTAINING the reading (Hong Kong
                   Observatory's climate extract), so "33°C" wins for
                   [33.0, 34.0) and 33.9°C is bucket 33, never 34.

    Getting this wrong is a half-degree shift of the entire distribution
    against the book -- a systematic mispricing of the two buckets either
    side of the mode, on every cycle, in the same direction.

    The axis owns both the width and the unit; see bucket_axis.py.
    """
    return axis.interval_c(bucket)
```

Replace the `bucket_probabilities` signature and body prologue. The signature becomes:

```python
def bucket_probabilities(
    estimate: CalibratedEstimate,
    bucket_min: int = config.BUCKET_MIN_C,
    bucket_max: int = config.BUCKET_MAX_C,
    edge_mode: str = "half_up",
    *,
    axis: BucketAxis = None,
) -> List[BucketProbability]:
```

Add to its docstring, after the existing `edge_mode` paragraph:

```
    axis is the market's bucket axis (unit + step + edge mode). Passing it
    supersedes edge_mode. Omitting it is only legal for a station on the
    default Celsius whole-degree axis: this function RAISES otherwise,
    rather than silently pricing a Fahrenheit market on a Celsius grid.
    That is not defensiveness -- a defaulted axis puts model_prob 0.0 on
    ten of eleven buckets, and a 0.0 model probability is a ~0.20 raw edge
    on the NO side that clears every risk gate. Fail closed.
```

Then replace the body's first three lines with:

```python
    if axis is None:
        station = config.STATIONS.get(estimate.station_icao)
        axis = (
            bucket_axis.for_station(station)
            if station is not None
            else BucketAxis(edge_mode=edge_mode)
        )
        if not axis.is_default:
            raise ValueError(
                f"{estimate.station_icao} is on a {axis.unit}/step-{axis.step} "
                f"bucket axis but bucket_probabilities() was called with no "
                f"axis. Refusing to price it on the Celsius whole-degree grid: "
                f"that puts model_prob 0.0 on ten of eleven buckets, and each "
                f"of those is a ~0.20 phantom edge on the NO side. Pass "
                f"axis=bucket_axis.for_station(station)."
            )

    mean = estimate.central_estimate_c
    sd = estimate.std_dev_c

    results = []
    for b in axis.keys(bucket_min, bucket_max):
        lower, upper = _bucket_interval(b, axis)
```

Leave the rest of the loop body unchanged.

Note the deliberate asymmetry: a REGISTERED station's axis wins over
`edge_mode`; an UNREGISTERED one falls back to `BucketAxis(edge_mode=edge_mode)`,
which is exactly today's behaviour for station-agnostic callers and old tests.

- [ ] **Step 4: Update the four call sites**

In `weather-forecast/ev_engine.py`, the bare call inside `evaluate_bucket_evs` (currently `model_probs = {b.bucket_c: b.probability for b in bucket_probabilities(estimate)}`) becomes:

```python
        model_probs = {
            b.bucket_c: b.probability
            for b in bucket_probabilities(
                estimate, axis=bucket_axis.for_station(config.get_station(estimate.station_icao))
            )
        }
```

and the second `ev_engine.py` call site becomes:

```python
    model_probs = {
        b.bucket_c: b.probability
        for b in bucket_probabilities(
            estimate, bucket_min, bucket_max, axis=bucket_axis.for_station(station)
        )
    }
```

In `weather-forecast/pipeline.py`:

```python
    buckets = bucket_probabilities(
        estimate,
        station.bucket_min_c,
        station.bucket_max_c,
        axis=bucket_axis.for_station(station),
    )
```

In `weather-forecast/backtest/engine.py`, find the `probability.bucket_probabilities(` call and pass `axis=bucket_axis.for_station(station)` in place of its `edge_mode=` argument, using whatever local name holds the StationConfig at that point.

Add `import bucket_axis` to each of the four files.

- [ ] **Step 5: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS. `tests/test_bucket_bounds_live.py` and `tests/test_resolution_rounding.py` exercise the legacy positional signature — if either fails, the default path has changed and the fix belongs in `probability.py`, not in the test.

- [ ] **Step 6: Commit**

```bash
git add weather-forecast/probability.py weather-forecast/ev_engine.py weather-forecast/pipeline.py weather-forecast/backtest/engine.py weather-forecast/tests/test_bucket_axis.py
git commit -m "Price on the market's own axis, and refuse to guess when it is unknown"
```

---

### Task 4: `bucket_for_temp` — axis-aware settlement

**Files:**
- Modify: `weather-forecast/backtest/resolution.py` (`bucket_for_temp`)
- Modify: `weather-forecast/position_manager.py` (the `settlement.bucket_for_temp(` call)
- Modify: `weather-forecast/backtest/report.py`, `weather-forecast/backtest/engine.py`, `weather-forecast/stop_loss_audit.py` (one call each)
- Test: `weather-forecast/tests/test_bucket_axis.py` (append)

**Interfaces:**
- Consumes: `bucket_axis.for_station`, `BucketAxis` (Task 1).
- Produces: `resolution.bucket_for_temp(t, bucket_min=None, bucket_max=None, edge_mode="half_up", *, axis=None) -> int`. `t` is and stays **Celsius**.

**This is the live settlement path, not just the backtest.** `position_manager.py` imports `backtest.resolution` as `settlement`. A wrong key here mis-settles a real position.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_bucket_axis.py`:

```python
class TestSettlementOnAFahrenheitAxis:
    AXIS = BucketAxis(unit="F", step=2)
    LO, HI = 68, 88

    def test_celsius_reading_settles_into_the_right_f_bucket(self):
        from backtest import resolution

        # 26.1C -> 78.98F -> 79F -> "78-79F"
        assert resolution.bucket_for_temp(
            26.1, self.LO, self.HI, axis=self.AXIS
        ) == 78

    def test_it_never_returns_an_off_grid_key(self):
        from backtest import resolution

        grid = set(self.AXIS.keys(self.LO, self.HI))
        t = -10.0
        while t <= 45.0:
            key = resolution.bucket_for_temp(t, self.LO, self.HI, axis=self.AXIS)
            assert key in grid, f"{t}C produced off-grid key {key}"
            t = round(t + 0.1, 1)

    def test_bankers_rounding_would_disagree_on_the_displayed_degree(self):
        # 22.5C is exactly 72.5F. floor(x+0.5) displays 73; round() displays
        # 72. Pinned on the DISPLAYED degree because on the 68..88 window both
        # land in the same bucket -- shift the window two degrees and they
        # do not.
        assert math.floor(72.5 + 0.5) == 73
        assert round(72.5) == 72
        assert self.AXIS.key_for_temp_c(22.5, self.LO, self.HI) == 72

    def test_celsius_stations_are_untouched(self):
        from backtest import resolution

        assert resolution.bucket_for_temp(33.9, 27, 37, "floor") == 33
        assert resolution.bucket_for_temp(33.9, 27, 37, "half_up") == 34
        assert resolution.bucket_for_temp(-50.0, 27, 37, "half_up") == 27
        assert resolution.bucket_for_temp(500.0, 27, 37, "half_up") == 37
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q -k SettlementOnAFahrenheit`
Expected: FAIL — `TypeError: bucket_for_temp() got an unexpected keyword argument 'axis'`.

- [ ] **Step 3: Rewrite `bucket_for_temp`**

In `weather-forecast/backtest/resolution.py`, add `from bucket_axis import BucketAxis` to the imports. Change the signature to:

```python
def bucket_for_temp(
    t: float,
    bucket_min: int = None,
    bucket_max: int = None,
    edge_mode: str = "half_up",
    *,
    axis: BucketAxis = None,
) -> int:
```

Append to its docstring:

```
    axis is the market's bucket axis; passing it supersedes edge_mode and
    is REQUIRED for any market that is not Celsius whole-degree. `t` is
    Celsius in every case -- the conversion into the market's unit happens
    inside the axis, so no caller ever handles a Fahrenheit temperature.
```

Replace the body's last four lines with:

```python
    lo = config.BUCKET_MIN_C if bucket_min is None else bucket_min
    hi = config.BUCKET_MAX_C if bucket_max is None else bucket_max
    resolved = BucketAxis(edge_mode=edge_mode) if axis is None else axis
    return resolved.key_for_temp_c(t, lo, hi)
```

- [ ] **Step 4: Update the four call sites**

Each already has a `StationConfig` in scope. Add `import bucket_axis` and pass `axis=bucket_axis.for_station(station)` in place of the `edge_mode=` argument at:
- `weather-forecast/position_manager.py` — the `settlement.bucket_for_temp(` call
- `weather-forecast/backtest/engine.py` — the `resolution.bucket_for_temp(` call
- `weather-forecast/backtest/report.py` — the `resolution.bucket_for_temp(` call
- `weather-forecast/stop_loss_audit.py` — the `bucket_for_temp(temp, station.bucket_min_c, station.bucket_max_c,` call

- [ ] **Step 5: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS, including `tests/test_resolution_rounding.py` and `tests/test_settlement_fallback.py`, which pin the legacy behaviour.

- [ ] **Step 6: Commit**

```bash
git add weather-forecast/backtest/resolution.py weather-forecast/position_manager.py weather-forecast/backtest/engine.py weather-forecast/backtest/report.py weather-forecast/stop_loss_audit.py weather-forecast/tests/test_bucket_axis.py
git commit -m "Settle on the market's own axis; the reading stays Celsius"
```

---

### Task 5: `market_discovery` — Fahrenheit parsing, sign capture, step-aware bounds

**Files:**
- Modify: `weather-forecast/market_discovery.py` (`_BUCKET_NUM_RE`, `_degree_numbers`, `parse_bucket_label`, `derive_bucket_bounds`, `get_market_state`, `discover_token_map`)
- Test: `weather-forecast/tests/test_bucket_axis.py` (append)

**Interfaces:**
- Consumes: `bucket_axis.BucketAxis`, `AXIS_C1`, `for_station` (Task 1).
- Produces: `parse_bucket_label(market, bucket_min=None, bucket_max=None, *, axis=AXIS_C1) -> Optional[int]`; `derive_bucket_bounds(token_map, step=1) -> Optional[Tuple[int, int]]`.

Folds in spec defects **(b)** sign capture and **(c)** the plausibility band, because both are edits to this same function.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_bucket_axis.py`:

```python
class TestDiscoveryParsesTheMarketsAxis:
    F_AXIS = BucketAxis(unit="F", step=2)
    NYC_LABELS = [
        "69°F or below", "70-71°F", "72-73°F", "74-75°F",
        "76-77°F", "78-79°F", "80-81°F", "82-83°F",
        "84-85°F", "86-87°F", "88°F or higher",
    ]

    def test_every_real_nyc_label_parses_onto_the_grid(self):
        import market_discovery as md

        got = [
            md.parse_bucket_label({"groupItemTitle": lab}, axis=self.F_AXIS)
            for lab in self.NYC_LABELS
        ]
        assert got == [68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88]

    def test_a_non_consecutive_pair_is_rejected_not_guessed(self):
        import market_discovery as md

        assert md.parse_bucket_label(
            {"groupItemTitle": "70-73°F"}, axis=self.F_AXIS
        ) is None

    def test_a_celsius_label_is_not_parsed_by_the_f_branch(self):
        import market_discovery as md

        assert md.parse_bucket_label(
            {"groupItemTitle": "31°C"}, axis=self.F_AXIS
        ) is None

    def test_the_date_in_a_question_is_still_thrown_out(self):
        import market_discovery as md

        q = ("Will the highest temperature in NYC on August 27, 2026 be "
             "80-81°F?")
        assert md.parse_bucket_label({"question": q}, axis=self.F_AXIS) == 80

    def test_sub_zero_celsius_keeps_its_sign(self):
        # Toronto and Buenos Aires. Today "-2C" parses as 2.
        import market_discovery as md

        assert md.parse_bucket_label({"groupItemTitle": "-2°C"}) == -2

    def test_celsius_parsing_is_otherwise_unchanged(self):
        import market_discovery as md

        assert md.parse_bucket_label({"groupItemTitle": "31°C"}) == 31
        assert md.parse_bucket_label(
            {"groupItemTitle": "27°C or below"}
        ) == 27


class TestDeriveBucketBoundsIsStepAware:
    def test_a_step_two_grid_is_accepted(self):
        import market_discovery as md

        tm = {k: {} for k in [68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88]}
        assert md.derive_bucket_bounds(tm, step=2) == (68, 88)

    def test_a_step_one_map_at_a_step_two_station_is_rejected(self):
        import market_discovery as md

        tm = {k: {} for k in range(78, 89)}
        assert md.derive_bucket_bounds(tm, step=2) is None

    def test_a_uniformly_shifted_odd_grid_is_rejected(self):
        import market_discovery as md

        tm = {k: {} for k in [69, 71, 73, 75, 77, 79, 81, 83, 85, 87, 89]}
        assert md.derive_bucket_bounds(tm, step=2) is None

    def test_a_short_map_is_still_rejected(self):
        import market_discovery as md

        tm = {k: {} for k in [68, 70, 72, 74, 76, 78, 80, 82, 84]}
        assert md.derive_bucket_bounds(tm, step=2) is None

    def test_celsius_behaviour_is_unchanged(self):
        import market_discovery as md

        assert md.derive_bucket_bounds({k: {} for k in range(27, 38)}) == (27, 37)
        assert md.derive_bucket_bounds({k: {} for k in range(27, 37)}) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q -k "DiscoveryParses or DeriveBucketBounds"`
Expected: FAIL — unexpected keyword `axis`, and the sign test returns `2`.

- [ ] **Step 3: Rewrite the parsing layer**

In `weather-forecast/market_discovery.py`, add `import bucket_axis` and `from bucket_axis import AXIS_C1, BucketAxis, UNIT_F`.

Replace the regex and band block with:

```python
# Celsius: unchanged. Requiring "°" throws out dates, years and stray
# digits; taking the LAST match throws out anything that still sneaks past.
# The sign IS captured -- Toronto and Buenos Aires have sub-zero windows,
# and "-2°C" parsed as 2 is a wrong key, not a missed one.
_BUCKET_NUM_RE = re.compile(r"(-?\d+)\s*°")

# Fahrenheit: the unit letter is REQUIRED, and the range form must be
# matched as a range. This is not fussiness -- the Celsius plausibility band
# below cannot police an F axis at all (a real "9°F or below" bucket
# overlaps the day-of-month range 1-31), so the "°F" letter is what replaces
# the band as the guard against parsing a date.
_F_RANGE_RE = re.compile(r"(-?\d+)\s*-\s*(\d+)\s*°\s*F", re.IGNORECASE)
_F_SINGLE_RE = re.compile(r"(-?\d+)\s*°\s*F", re.IGNORECASE)
_C_UNIT_RE = re.compile(r"°\s*C", re.IGNORECASE)

_OR_BELOW_RE = re.compile(r"or\s+(below|lower|less)", re.IGNORECASE)
_OR_ABOVE_RE = re.compile(r"or\s+(above|higher|more)", re.IGNORECASE)

# Plausibility band per unit. The Celsius floor drops to -30 because Toronto
# and Buenos Aires both run below 5°C, and the old justification ("every
# registered city's live window sits inside 25..40") stopped being true when
# Europe was registered.
_PLAUSIBLE_BAND = {"C": (-30, 55), "F": (-20, 130)}

# Kept as module-level names because tests and other modules read them.
MIN_PLAUSIBLE_BUCKET_C, MAX_PLAUSIBLE_BUCKET_C = _PLAUSIBLE_BAND["C"]
```

Replace `_degree_numbers` with:

```python
def _degree_numbers(label: str, unit: str = "C") -> list:
    """Every plausible degree-marked number in a label, in order of appearance."""
    lo, hi = _PLAUSIBLE_BAND[unit]
    return [
        n for n in (int(m) for m in _BUCKET_NUM_RE.findall(label))
        if lo <= n <= hi
    ]
```

Change `parse_bucket_label`'s signature to:

```python
def parse_bucket_label(
    market: dict,
    bucket_min: Optional[int] = None,
    bucket_max: Optional[int] = None,
    *,
    axis: BucketAxis = AXIS_C1,
) -> Optional[int]:
```

Keep the whole existing docstring and append:

```
    RETURNS THE BUCKET'S LOWER EDGE, in the axis's own unit. On the Celsius
    whole-degree axis that is the printed number, unchanged. On a step-2
    Fahrenheit axis, "70-71°F" is key 70 and "69°F or below" is key 68
    (= printed_top + 1 - step), so the keys form a uniform grid. See
    bucket_axis.BucketAxis.label() for the inverse.
```

Insert the Fahrenheit branch at the top of the body, before the existing
Celsius logic:

```python
    labels = [market.get("groupItemTitle") or "", market.get("question") or ""]

    if axis.unit == UNIT_F:
        for label in labels:
            if _C_UNIT_RE.search(label):
                continue  # a Celsius label on an F station is not ours to guess
            m = _F_RANGE_RE.search(label)
            if m:
                low, high = int(m.group(1)), int(m.group(2))
                if high - low != axis.step - 1:
                    continue  # not this axis's width -- reject, never guess
                return low
            m = _F_SINGLE_RE.search(label)
            if not m:
                continue
            n = int(m.group(1))
            lo_band, hi_band = _PLAUSIBLE_BAND[UNIT_F]
            if not lo_band <= n <= hi_band:
                continue
            if _OR_BELOW_RE.search(label):
                return n + 1 - axis.step
            return n
        return None
```

Leave the rest of the function (the Celsius path) exactly as it is.

Replace the shape test inside `derive_bucket_bounds`, changing its signature
to `def derive_bucket_bounds(token_map: Dict[int, dict], step: int = 1) -> Optional[Tuple[int, int]]:`
and its checks to:

```python
    keys = sorted(token_map)
    if len(keys) != config.EXPECTED_BUCKET_COUNT:
        return None  # short or long: not the 11-outcome event we know how to price
    lo, hi = keys[0], keys[-1]
    if keys != list(range(lo, hi + step, step)):
        return None  # gap, off-grid key, or the wrong step for this station
    return lo, hi
```

Append to its docstring:

```
    step is the market's bucket width in its own unit. At step=1 this is
    provably the same predicate as the contiguity test it replaces. At
    step=2 it additionally rejects the failure today's regex actually
    produces on an American event: "70-71°F" yields only 71, giving a
    step-1 map whose span is 10 rather than 20.
```

Finally, in `get_market_state` and `discover_token_map`, resolve the axis
once from the station already in scope and pass it to both calls:

```python
    axis = bucket_axis.for_station(station)
    ...
    bucket = parse_bucket_label(market, bucket_min, bucket_max, axis=axis)
    ...
    bounds = derive_bucket_bounds(token_map, step=axis.step)
```

- [ ] **Step 4: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add weather-forecast/market_discovery.py weather-forecast/tests/test_bucket_axis.py
git commit -m "Discover on the market's own axis; stop dropping the minus sign"
```

---

### Task 6: `bucket_bias` — honour the `_c` suffix

**Files:**
- Modify: `weather-forecast/bucket_bias.py` (`bucket_midpoint_c`, its caller in `bucket_bias_samples`, `derived_bias_stats`, the `quantization_stderr_c(n)` call, `print_report`)
- Test: `weather-forecast/tests/test_bucket_axis.py` (append)

**Interfaces:**
- Consumes: `bucket_axis.for_station`, `BucketAxis` (Task 1).
- Produces: `bucket_midpoint_c(bucket_c, bounds, edge_mode, *, axis=None) -> Optional[float]`, returning **Celsius**.

**This is the live-money landmine.** `bucket_midpoint_c` → `bucket_bias_samples` (`errors.append(forecast_mean - midpoint)`) → `derived_bias_stats` → `entry_manager.forecast_bias_stats` → `calibration.blend_central_estimate`, which subtracts it from a Celsius forecast mean. A Fahrenheit midpoint makes that "bias" ≈ `0.8·T + 32` — order 60 — and it fires only on stations with no error record yet, which is exactly the ones being added.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_bucket_axis.py`:

```python
class TestBiasMidpointStaysCelsius:
    """
    The _c suffix on a RETURN VALUE is a promise. bucket_bias_samples
    subtracts this from a Celsius forecast mean and the result reaches
    calibration.blend_central_estimate, so a Fahrenheit number here is a
    live mispricing, not a display bug.
    """

    def test_a_fahrenheit_midpoint_is_returned_in_celsius(self):
        import bucket_bias

        axis = BucketAxis(unit="F", step=2)
        # Bucket "78-79F" spans 77.5F..79.5F, midpoint 78.5F = 25.833C
        got = bucket_bias.bucket_midpoint_c(78, (68, 88), "half_up", axis=axis)
        assert got == pytest.approx((78.5 - 32) * 5 / 9, abs=1e-6)

    def test_a_fahrenheit_midpoint_is_a_plausible_celsius_temperature(self):
        import bucket_bias

        axis = BucketAxis(unit="F", step=2)
        for key in axis.keys(70, 86):
            got = bucket_bias.bucket_midpoint_c(key, (68, 88), "half_up", axis=axis)
            assert -60.0 < got < 60.0, f"bucket {key} midpoint {got} is not Celsius"

    def test_celsius_midpoints_are_unchanged(self):
        import bucket_bias

        assert bucket_bias.bucket_midpoint_c(31, (27, 37), "half_up") == 31.0
        assert bucket_bias.bucket_midpoint_c(31, (27, 37), "floor") == 31.5

    def test_edge_buckets_are_still_censored(self):
        import bucket_bias

        axis = BucketAxis(unit="F", step=2)
        assert bucket_bias.bucket_midpoint_c(68, (68, 88), "half_up", axis=axis) is None
        assert bucket_bias.bucket_midpoint_c(88, (68, 88), "half_up", axis=axis) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q -k BiasMidpointStaysCelsius`
Expected: FAIL — unexpected keyword `axis`.

- [ ] **Step 3: Rewrite `bucket_midpoint_c` and its neighbours**

In `weather-forecast/bucket_bias.py`, add `import bucket_axis` and `from bucket_axis import BucketAxis`.

Replace `bucket_midpoint_c` with:

```python
def bucket_midpoint_c(
    bucket_c: int,
    bounds: Tuple[int, int],
    edge_mode: str,
    *,
    axis: BucketAxis = None,
) -> Optional[float]:
    """
    The midpoint temperature of a settled bucket IN DEGREES CELSIUS, or None
    if the bucket is one of the event's CENSORED edge catch-alls.

    THE RETURN UNIT IS LOAD-BEARING. bucket_bias_samples() subtracts this
    from a Celsius forecast mean, and the result flows to
    calibration.blend_central_estimate() via the entry_manager bias gate. A
    Fahrenheit midpoint here makes the "bias" roughly 0.8*T + 32 -- order 60
    -- subtracted from a Celsius forecast, on a station with no error record
    of its own. That is why this converts rather than returning the key.

    Interval semantics come from the axis, and mirror
    backtest.resolution.bucket_for_temp exactly, because a midpoint taken
    under the other convention is off by half a bucket -- half the effect
    being measured.

    None for the edges is the honest answer, not a limitation to work
    around. "37 or above" has no midpoint; substituting 37.5 would invent a
    ceiling the market does not have, and on a genuinely hot day that
    invention runs one direction only.
    """
    bucket_min, bucket_max = bounds
    if bucket_c <= bucket_min or bucket_c >= bucket_max:
        return None
    resolved = BucketAxis(edge_mode=edge_mode) if axis is None else axis
    lower_c, upper_c = resolved.interval_c(bucket_c)
    return (lower_c + upper_c) / 2
```

Verify by hand that this is a no-op for Celsius: `half_up` gives
`((b-0.5) + (b+0.5)) / 2 == b`, and `floor` gives `(b + (b+1)) / 2 == b + 0.5`
— exactly the two values the old expression returned.

In `bucket_bias_samples`, resolve the axis once from the station it already
looks up and pass it:

```python
        midpoint = bucket_midpoint_c(
            bucket_c, (bucket_min, bucket_max), edge_mode,
            axis=bucket_axis.for_station(station),
        )
```

Change the `quantization_stderr_c(n)` call in `print_report` to pass the real
width:

```python
        q = quantization_stderr_c(n, bucket_axis.for_station(station).width_c())
```

and update `quantization_stderr_c`'s docstring to say `bucket_width_c` is in
degrees Celsius and comes from `BucketAxis.width_c()`.

In `print_report`, replace the `{bucket_c:>7}` column with the axis label so
the report cannot print a Fahrenheit key under a Celsius heading:

```python
        axis = bucket_axis.for_station(station)
        for target_date, bucket_c, midpoint, forecast_mean in rows:
            print(f"    {target_date.isoformat():<12}"
                  f"{axis.label(bucket_c, station.bucket_min_c, station.bucket_max_c):>14}"
                  f"{midpoint:>10.1f}{forecast_mean:>10.2f}"
                  f"{forecast_mean - midpoint:>+9.2f}")
```

- [ ] **Step 4: Add the fail-loud assertion to `derived_bias_stats`**

At the end of `derived_bias_stats`, before it returns, insert the following.
Read the function first: it returns a tuple built from
`calibration.bias_stats(errors)`, and the local holding the mean may not be
named `bias` — use whatever name it actually uses rather than introducing a
second one.

```python
    # A bias is a Celsius forecast error. Anything outside this band means a
    # unit leaked in -- most likely a bucket key that never got converted --
    # and a silent order-60 "bias" would graduate a station through the bias
    # gate and then be subtracted from every forecast. Fail loudly.
    if bias is not None and abs(bias) > 10.0:
        raise ValueError(
            f"{station_icao}: derived bias {bias:.2f} is not a plausible "
            f"Celsius forecast error. This almost certainly means a bucket "
            f"key reached the bias path without unit conversion -- see "
            f"bucket_bias.bucket_midpoint_c."
        )
```

Add a test for it:

```python
    def test_an_implausible_bias_raises_rather_than_graduating_a_station(self, monkeypatch):
        import bucket_bias

        monkeypatch.setattr(
            bucket_bias, "bucket_bias_samples",
            lambda icao, dates=None: ([59.5, 60.5, 61.0], [], []),
        )
        with pytest.raises(ValueError, match="plausible"):
            bucket_bias.derived_bias_stats("WSSS")
```

- [ ] **Step 5: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS, including `tests/test_bucket_bias.py` and `tests/test_forecast_bias.py`.

- [ ] **Step 6: Commit**

```bash
git add weather-forecast/bucket_bias.py weather-forecast/tests/test_bucket_axis.py
git commit -m "Keep the bias in Celsius, and fail loudly if a unit ever leaks in"
```

---

### Task 7: `settled_buckets` records its own units

**Files:**
- Modify: `weather-forecast/storage.py` (`settled_buckets` DDL, `save_settled_bucket`, `load_settled_buckets`)
- Modify: `weather-forecast/bucket_bias.py`, `weather-forecast/promotion_dossier.py` (the two unpack sites)
- Test: `weather-forecast/tests/test_bucket_axis.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `load_settled_buckets(station_icao) -> Dict[date, Tuple[int, int, int, str, int]]` — `(bucket_c, bucket_min_c, bucket_max_c, bucket_unit, bucket_step)`. `save_settled_bucket(..., source, bucket_unit="C", bucket_step=1)`.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_bucket_axis.py`:

```python
class TestSettledBucketsAreSelfDescribing:
    def test_a_saved_row_round_trips_its_units(self, tmp_path, monkeypatch):
        from datetime import date
        import config
        import storage

        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
        storage.init_db()
        storage.save_settled_bucket(
            "KLGA", date(2026, 8, 27), 78, 68, 88, "metar_daily_max",
            bucket_unit="F", bucket_step=2,
        )
        got = storage.load_settled_buckets("KLGA")
        assert got[date(2026, 8, 27)] == (78, 68, 88, "F", 2)

    def test_legacy_rows_default_to_celsius_whole_degree(self, tmp_path, monkeypatch):
        from datetime import date
        import config
        import storage

        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
        storage.init_db()
        storage.save_settled_bucket(
            "WSSS", date(2026, 8, 27), 31, 27, 37, "metar_daily_max",
        )
        assert storage.load_settled_buckets("WSSS")[date(2026, 8, 27)] == (
            31, 27, 37, "C", 1
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q -k SettledBucketsAreSelfDescribing`
Expected: FAIL — unexpected keyword `bucket_unit`, and the tuple has 3 elements.

- [ ] **Step 3: Migrate the table**

In `weather-forecast/storage.py`, add the two columns to the `CREATE TABLE IF NOT EXISTS settled_buckets` DDL:

```sql
            bucket_unit TEXT NOT NULL DEFAULT 'C',
            bucket_step INTEGER NOT NULL DEFAULT 1,
```

and add an idempotent migration alongside the existing `PRAGMA table_info` / `ALTER TABLE ADD COLUMN` block, following its exact pattern:

```python
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(settled_buckets)")}
        if "bucket_unit" not in _cols:
            conn.execute(
                "ALTER TABLE settled_buckets ADD COLUMN "
                "bucket_unit TEXT NOT NULL DEFAULT 'C'"
            )
        if "bucket_step" not in _cols:
            conn.execute(
                "ALTER TABLE settled_buckets ADD COLUMN "
                "bucket_step INTEGER NOT NULL DEFAULT 1"
            )
```

The `DEFAULT 'C'` / `DEFAULT 1` is what makes every existing row correct
without a backfill: every settlement recorded before today WAS on the
Celsius whole-degree axis.

Widen `save_settled_bucket` with `bucket_unit: str = "C", bucket_step: int = 1`
after `source`, and add both to the INSERT's column list and value tuple.

Widen `load_settled_buckets`:

```python
def load_settled_buckets(station_icao: str) -> Dict[date, Tuple[int, int, int, str, int]]:
    """
    {target_date: (bucket_c, bucket_min_c, bucket_max_c, bucket_unit, bucket_step)}
    for one station.

    The unit and step are stored per ROW, not read from the registry,
    because a settlement is immutable history: if a market's axis ever
    changes, the old rows must keep describing themselves.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT target_date, bucket_c, bucket_min_c, bucket_max_c, "
            "bucket_unit, bucket_step "
            "FROM settled_buckets WHERE station_icao = ?",
            (station_icao,),
        ).fetchall()
    return {
        date.fromisoformat(str(r[0])): (
            int(r[1]), int(r[2]), int(r[3]), str(r[4]), int(r[5])
        )
        for r in rows
    }
```

- [ ] **Step 4: Update the two unpack sites**

In `weather-forecast/bucket_bias.py`, the `bucket_c, bucket_min, bucket_max = record` unpack becomes:

```python
        bucket_c, bucket_min, bucket_max, row_unit, row_step = record
```

and the axis passed to `bucket_midpoint_c` is built from the ROW, not the
registry, so a re-scored historical day is scored on the axis it settled on:

```python
        midpoint = bucket_midpoint_c(
            bucket_c, (bucket_min, bucket_max), edge_mode,
            axis=BucketAxis(unit=row_unit, step=row_step, edge_mode=edge_mode),
        )
```

Find the corresponding unpack in `weather-forecast/promotion_dossier.py` and
widen it the same way, discarding the two new values if unused (`*_`).

Also update the `save_settled_bucket(` call in `bucket_bias.ingest_settled_buckets`
to pass the station's current axis:

```python
            _axis = bucket_axis.for_station(station)
            storage.save_settled_bucket(
                icao, target_date, bucket, bounds[0], bounds[1], source,
                bucket_unit=_axis.unit, bucket_step=_axis.step,
            )
```

- [ ] **Step 5: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add weather-forecast/storage.py weather-forecast/bucket_bias.py weather-forecast/promotion_dossier.py weather-forecast/tests/test_bucket_axis.py
git commit -m "Make settled buckets say which axis they settled on"
```

---

### Task 8: The cross-station property sweep — the no-op proof

**Files:**
- Test: `weather-forecast/tests/test_bucket_axis.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: nothing. This task adds no production code; it is the gate that says Phase 1 is safe.

This is the single test that carries most of the plan's weight. It catches an omitted axis, a wrong step, a wrong edge mode, an off-by-`step` key convention, and the banker's-rounding flip — in one sweep — and it re-asserts today's exact behaviour for the existing 20 stations.

- [ ] **Step 1: Write the sweep**

Append to `weather-forecast/tests/test_bucket_axis.py`:

```python
def _all_axes_under_test():
    """Every registered station, plus the two axes no station has YET."""
    import config

    cases = [
        (icao, bucket_axis.for_station(st), st.bucket_min_c, st.bucket_max_c)
        for icao, st in config.STATIONS.items()
    ]
    cases.append(("SYNTH-F2", BucketAxis(unit="F", step=2), 68, 88))
    cases.append(("SYNTH-F2-COLD", BucketAxis(unit="F", step=2), 8, 28))
    return cases


@pytest.mark.parametrize("icao,axis,lo,hi", _all_axes_under_test())
class TestAxisPropertiesHoldForEveryStation:

    def test_the_key_a_reading_settles_into_contains_that_reading(
        self, icao, axis, lo, hi
    ):
        from backtest import resolution

        lo_c, _ = axis.interval_c(lo)
        _, hi_c = axis.interval_c(hi)
        t = round(lo_c - 3.0, 1)
        while t <= hi_c + 3.0:
            key = resolution.bucket_for_temp(t, lo, hi, axis=axis)
            k_lo, k_hi = axis.interval_c(key)
            if key == lo:
                assert t < k_hi, f"{icao}: {t}C clamped to {key}, above its top edge"
            elif key == hi:
                assert t >= k_lo, f"{icao}: {t}C clamped to {key}, below its low edge"
            else:
                assert k_lo <= t < k_hi, (
                    f"{icao}: {t}C settled into bucket {key} = [{k_lo}, {k_hi})"
                )
            t = round(t + 0.1, 1)

    def test_the_listed_buckets_tile_the_line(self, icao, axis, lo, hi):
        keys = axis.keys(lo, hi)
        assert len(keys) == 11, f"{icao}: {len(keys)} buckets, expected 11"
        for left, right in zip(keys, keys[1:]):
            _, left_top = axis.interval_c(left)
            right_bottom, _ = axis.interval_c(right)
            assert left_top == pytest.approx(right_bottom, abs=1e-9), (
                f"{icao}: gap or overlap between bucket {left} and {right}"
            )

    def test_the_probabilities_sum_to_one_and_the_mode_is_where_it_should_be(
        self, icao, axis, lo, hi
    ):
        from datetime import date

        import probability
        from models import CalibratedEstimate

        lo_c, _ = axis.interval_c(lo)
        _, hi_c = axis.interval_c(hi)
        centre = (lo_c + hi_c) / 2
        est = CalibratedEstimate(
            station_icao=icao, target_date=date(2026, 8, 27),
            central_estimate_c=centre, std_dev_c=1.0, monsoon_phase="unknown",
        )
        got = probability.bucket_probabilities(est, lo, hi, axis=axis)

        assert sum(b.probability for b in got) == pytest.approx(1.0, abs=1e-3)
        mode = max(got, key=lambda b: b.probability)
        m_lo, m_hi = axis.interval_c(mode.bucket_c)
        assert m_lo <= centre < m_hi, (
            f"{icao}: mode bucket {mode.bucket_c} = [{m_lo}, {m_hi}) "
            f"does not contain the central estimate {centre}"
        )


class TestPhaseOneChangedNothing:
    """
    The byte-for-byte constraint, asserted directly. Every existing station
    is on the default axis, and on the default axis the new code path must
    reproduce the old formulas exactly.
    """

    def test_every_registered_station_is_still_on_the_default_axis(self):
        import config

        for icao, st in config.STATIONS.items():
            assert bucket_axis.for_station(st).is_default, icao

    def test_the_default_axis_reproduces_the_historical_interval_formulas(self):
        for b in range(-30, 56):
            assert AXIS_C1.interval_c(b) == (b - 0.5, b + 0.5)
            assert BucketAxis(edge_mode="floor").interval_c(b) == (
                float(b), float(b + 1)
            )

    def test_the_default_axis_reproduces_the_historical_rounding(self):
        t = -20.0
        while t <= 60.0:
            assert AXIS_C1.key_for_temp_c(t, -100, 100) == math.floor(t + 0.5), t
            assert BucketAxis(edge_mode="floor").key_for_temp_c(
                t, -100, 100
            ) == math.floor(t), t
            t = round(t + 0.1, 1)
```

- [ ] **Step 2: Run the sweep**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q`
Expected: all PASS. A containment failure on a `SYNTH-F2` case means the key convention or the step is wrong; a failure on a real ICAO means Phase 1 changed behaviour and must be fixed before proceeding.

- [ ] **Step 3: Run the whole suite one more time**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS. **This is the Phase 1 gate. Do not start Phase 2 until it is green.**

- [ ] **Step 4: Commit**

```bash
git add weather-forecast/tests/test_bucket_axis.py
git commit -m "Prove the axis is a no-op for every station that exists today"
```

---

### Task 9: Render the axis, not a hardcoded degree suffix

**Files:**
- Modify: `weather-forecast/executor.py`, `weather-forecast/ev_engine.py`, `weather-forecast/pipeline.py`
- Modify: `deploy/generate_dashboard.py`, `deploy/generate_realmoney_dashboard.py`
- Test: `weather-forecast/tests/test_bucket_axis.py` (append)

**Interfaces:**
- Consumes: `BucketAxis.label` (Task 1).
- Produces: nothing new.

The executor line is what a human reads before placing a real order. The real-money dashboard is a frozen copy in `/usr/local/bin` that `git pull` does not update (memory: EC2 deployment) — it will keep rendering the old way until it is explicitly reinstalled, which Task 15 covers.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_bucket_axis.py`:

```python
class TestNothingRendersAHardcodedDegreeSuffix:
    """
    A key rendered with a hardcoded suffix is how a human ends up told to
    buy the wrong contract: "KLGA 78°C (YES)" for a bucket the market
    prints as "78-79°F".
    """

    PRODUCTION_FILES = [
        "executor.py", "ev_engine.py", "pipeline.py",
        "../deploy/generate_dashboard.py",
        "../deploy/generate_realmoney_dashboard.py",
    ]

    def test_no_bucket_value_is_formatted_with_a_literal_degree_c(self):
        import pathlib
        import re

        here = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        # An f-string interpolation of anything bucket-shaped immediately
        # followed by a literal degree suffix.
        pat = re.compile(r"\{[^{}]*bucket[^{}]*\}\s*(°C|&deg;C)", re.IGNORECASE)
        for rel in self.PRODUCTION_FILES:
            path = (here / rel).resolve()
            if not path.exists():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        assert not offenders, (
            "these render a bucket key with a hardcoded unit; use "
            "bucket_axis.for_station(station).label(key, lo, hi):\n  "
            + "\n  ".join(offenders)
        )
```

- [ ] **Step 2: Run it to see the current offenders**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q -k NothingRendersAHardcoded`
Expected: FAIL, listing every site. Record that list — it is the work for Step 3.

- [ ] **Step 3: Replace each offender**

For every file the test named, resolve the axis once near the top of the
function (`axis = bucket_axis.for_station(station)`) and replace the
interpolation with `axis.label(bucket_c, station.bucket_min_c, station.bucket_max_c)`.
Add `import bucket_axis` where missing.

**The regex in Step 1 is a tripwire, not an exhaustive search**, and saying
so matters: it only matches an interpolation whose variable name contains
"bucket" immediately followed by a literal suffix. It will NOT catch
`f"{lo}-{hi}°C"` in `deploy/generate_realmoney_dashboard.py`'s
`bounds_drift()`, where the variables are named `lo`/`hi`. Fix that one by
hand (next paragraph) and grep each named file for `°C` and `&deg;C`
yourself before calling this task done.

In `deploy/generate_realmoney_dashboard.py`'s `bounds_drift()`, the
`f"{lo}-{hi}°C"` rendering becomes:

```python
    axis = bucket_axis.for_station(station)
    discovered = f"{axis.label(lo, lo, hi)} .. {axis.label(hi, lo, hi)}"
```

- [ ] **Step 4: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS, including `tests/test_realmoney_dashboard.py`.

- [ ] **Step 5: Commit**

```bash
git add weather-forecast/executor.py weather-forecast/ev_engine.py weather-forecast/pipeline.py deploy/generate_dashboard.py deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_bucket_axis.py
git commit -m "Render the bucket the market prints, not the key with a guessed unit"
```

---

# PHASE 2 — The region shell, the four Celsius cities, the six defects

---

### Task 10: `region="americas"` in all five pools

**Files:**
- Modify: `weather-forecast/config.py` (five `REGION_*` dicts)
- Modify: `weather-forecast/tests/test_region_isolation.py` (the exact-set assertion)
- Test: `weather-forecast/tests/test_americas_region.py` (create)

**Interfaces:**
- Consumes: the existing region mechanism.
- Produces: `"americas"` as a valid region name everywhere `region_of()` can return it.

- [ ] **Step 1: Write the failing test**

Create `weather-forecast/tests/test_americas_region.py`:

```python
"""
tests/test_americas_region.py

The americas region draws on its own capital and its own real-money blast
radius. Mirrors tests/test_region_isolation.py, which does the same job for
europe -- see docs/superpowers/specs/2026-08-27-americas-market-isolation-design.md.
"""
import pytest

import config


class TestAmericasHasEveryPool:
    def test_it_is_present_in_all_five_region_dicts(self):
        for name in (
            "REGION_BANKROLL_USD",
            "REGION_MAX_DAILY_EXPOSURE_USD",
            "REGION_LIVE_MAX_CONCURRENT_POSITIONS",
            "REGION_LIVE_MAX_TOTAL_EXPOSURE_USD",
            "REGION_LIVE_MAX_ORDERS_PER_DAY",
        ):
            assert "americas" in getattr(config, name), name

    def test_its_paper_pools_are_funded_like_europes(self):
        assert config.REGION_BANKROLL_USD["americas"] == config.BANKROLL_USD
        assert (config.REGION_MAX_DAILY_EXPOSURE_USD["americas"]
                == config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD)

    def test_its_live_blast_radius_is_locked_at_zero(self):
        assert config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["americas"] == 0
        assert config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD["americas"] == 0.0
        assert config.REGION_LIVE_MAX_ORDERS_PER_DAY["americas"] == 0

    def test_it_authorises_no_live_orders(self):
        assert not config.region_authorises_live_orders("americas")

    def test_asia_and_europe_are_untouched(self):
        assert config.REGION_BANKROLL_USD["asia"] == config.BANKROLL_USD
        assert config.REGION_BANKROLL_USD["europe"] == config.BANKROLL_USD
        assert config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["europe"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_americas_region.py -q`
Expected: FAIL — `assert 'americas' in {...}`.

- [ ] **Step 3: Add the five entries**

In `weather-forecast/config.py`, add `"americas"` to each of the five dicts,
referencing the same constants Europe does rather than restating literals:

```python
REGION_LIVE_MAX_CONCURRENT_POSITIONS = {
    "asia": LIVE_MAX_CONCURRENT_POSITIONS,
    "europe": 0,
    "americas": 0,
}
REGION_LIVE_MAX_TOTAL_EXPOSURE_USD = {
    "asia": LIVE_MAX_TOTAL_EXPOSURE_USD,
    "europe": 0.0,
    "americas": 0.0,
}
REGION_LIVE_MAX_ORDERS_PER_DAY = {
    "asia": LIVE_MAX_ORDERS_PER_DAY,
    "europe": 0,
    "americas": 0,
}
...
REGION_BANKROLL_USD = {
    "asia": BANKROLL_USD,
    "europe": BANKROLL_USD,
    "americas": BANKROLL_USD,
}
REGION_MAX_DAILY_EXPOSURE_USD = {
    "asia": MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD,
    "europe": MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD,
    "americas": MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD,
}
```

Note the comment block above `REGION_LIVE_MAX_CONCURRENT_POSITIONS` names
Europe explicitly ("no European station...") — widen it to say the same of
any region whose entries are 0/0.0/0.

- [ ] **Step 4: Fix the exact-set assertion**

In `weather-forecast/tests/test_region_isolation.py`, the assertion
`set(config.REGION_BANKROLL_USD) == {"asia", "europe"}` becomes
`{"asia", "europe", "americas"}`. **This failure is expected, not a
regression** — it is an exact-set tripwire doing its job.

- [ ] **Step 5: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add weather-forecast/config.py weather-forecast/tests/test_americas_region.py weather-forecast/tests/test_region_isolation.py
git commit -m "Give americas its own capital and its own zeroed blast radius"
```

---

### Task 11: Per-region spread ceiling

**Files:**
- Modify: `weather-forecast/config.py` (`SPREAD_CEILING_C` → per-region dict)
- Modify: `weather-forecast/calibration.py` (`_clamp_spread` and its five call sites)
- Test: `weather-forecast/tests/test_americas_region.py` (append)

**Interfaces:**
- Consumes: `config.region_of` (existing).
- Produces: `config.REGION_SPREAD_CEILING_C: Dict[str, Optional[float]]`, `config.region_spread_ceiling_c(station_icao) -> Optional[float]`, `calibration._clamp_spread(value, station_icao=None) -> float`.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_americas_region.py`:

```python
class TestSpreadCeilingIsPerRegion:
    """
    config.py's own comment: "a too-NARROW spread is the dangerous
    direction: it makes the model look certain, which inflates the gap
    between model probability and market price, which is an edge the entry
    gates will happily size into." A 2.0C ceiling tuned on tropical
    stations would clamp continental and winter spreads in exactly that
    direction.
    """

    def test_asia_and_europe_keep_two_point_zero_verbatim(self):
        assert config.REGION_SPREAD_CEILING_C["asia"] == 2.0
        assert config.REGION_SPREAD_CEILING_C["europe"] == 2.0

    def test_americas_is_none_meaning_no_clamp_not_a_guessed_number(self):
        assert config.REGION_SPREAD_CEILING_C["americas"] is None

    def test_a_wide_spread_is_clamped_for_asia(self):
        import calibration

        assert calibration._clamp_spread(5.0, "WSSS") == 2.0

    def test_a_wide_spread_is_left_alone_for_americas(self, monkeypatch):
        import calibration
        from models import StationConfig

        st = StationConfig(
            icao="KORD", display_name="O'Hare", country="United States",
            lat=41.98, lon=-87.90, wunderground_slug="us/chicago/KORD",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="chicago", region="americas",
        )
        monkeypatch.setitem(config.STATIONS, "KORD", st)
        assert calibration._clamp_spread(5.0, "KORD") == 5.0

    def test_the_floor_still_applies_everywhere(self, monkeypatch):
        import calibration
        from models import StationConfig

        st = StationConfig(
            icao="KORD", display_name="O'Hare", country="United States",
            lat=41.98, lon=-87.90, wunderground_slug="us/chicago/KORD",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="chicago", region="americas",
        )
        monkeypatch.setitem(config.STATIONS, "KORD", st)
        assert calibration._clamp_spread(0.1, "KORD") == config.SPREAD_FLOOR_C

    def test_an_unknown_station_keeps_the_legacy_global_ceiling(self):
        import calibration

        assert calibration._clamp_spread(5.0) == 2.0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_americas_region.py -q -k SpreadCeiling`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'REGION_SPREAD_CEILING_C'`.

- [ ] **Step 3: Add the per-region ceiling**

In `weather-forecast/config.py`, immediately below the existing
`SPREAD_CEILING_C = 2.0` (keep that constant — it stays the legacy global
and the source of truth for the two funded regions), add:

```python
# The ceiling is a REGIONAL fact, not a global one. 2.0 was measured on
# tropical stations whose day-to-day maximum barely moves. Continental North
# America and Southern-hemisphere winter routinely run wider, and clamping
# them runs in the direction this file already calls the dangerous one (see
# SPREAD_FLOOR_C): a too-narrow spread makes the model look certain, which
# inflates the gap against market price, which the entry gates size into.
#
# None means NO CLAMP -- the measured spread is used as-is. That is
# deliberately the conservative direction: an unclamped spread is wider,
# which makes the model LESS certain, which SHRINKS the gap. A guessed
# ceiling would run the other way. americas stops being None only when a
# number is derived from its OWN measured spread, which cannot happen before
# it has observations. Do not copy 2.0 across.
REGION_SPREAD_CEILING_C = {
    "asia": SPREAD_CEILING_C,
    "europe": SPREAD_CEILING_C,
    "americas": None,
}


def region_spread_ceiling_c(station_icao: str):
    """
    This station's region's spread ceiling, or None for no clamp.

    Raises on a region with no entry rather than falling back, for the same
    reason region_bankroll_usd() does: a typo'd region must not quietly
    inherit another region's risk posture.
    """
    region = region_of(station_icao)
    if region not in REGION_SPREAD_CEILING_C:
        raise KeyError(
            f"{station_icao} names region {region!r}, which has no entry in "
            f"config.REGION_SPREAD_CEILING_C "
            f"(known: {list(REGION_SPREAD_CEILING_C)})."
        )
    return REGION_SPREAD_CEILING_C[region]
```

Place `region_spread_ceiling_c` next to `region_bankroll_usd` so the three
lookup helpers read together.

- [ ] **Step 4: Make `_clamp_spread` region-aware**

In `weather-forecast/calibration.py`:

```python
def _clamp_spread(value: float, station_icao: str = None) -> float:
    """
    Hold a spread inside its REGION's band -- see SPREAD_FLOOR_C and
    config.REGION_SPREAD_CEILING_C.

    The floor is global: a spread below it is the dangerous direction
    everywhere. The ceiling is regional, and a region whose ceiling is None
    is not clamped at all. station_icao defaults to None for
    station-agnostic callers, which keeps the legacy global ceiling.
    """
    floored = max(value, config.SPREAD_FLOOR_C)
    if station_icao is None:
        return round(min(floored, config.SPREAD_CEILING_C), 2)
    ceiling = config.region_spread_ceiling_c(station_icao)
    if ceiling is None:
        return round(floored, 2)
    return round(min(floored, ceiling), 2)
```

Then pass `station_icao` at all five call sites inside `estimate_std_dev`
(which already has it as a parameter):

```python
        return _clamp_spread(statistics.stdev(ensemble_members), station_icao), "ensemble"
        ...
        return _clamp_spread(config.POOLED_SPREAD_FALLBACK_C, station_icao), "replay_constant"
        ...
            return _clamp_spread(measured, station_icao), "measured_error"
        ...
        return _clamp_spread(pooled, station_icao), "pooled_error"
        ...
    return _clamp_spread(config.POOLED_SPREAD_FALLBACK_C, station_icao), "fallback_default"
```

Leave the tier ORDER exactly as it is. There is a known open defect there
(the ensemble tier fires before the measured tier — memory:
`ensemble-spread-tier-defect`); it is out of scope for this plan and must
not be silently "fixed" here.

- [ ] **Step 5: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS, including `tests/test_spread_estimator.py`.

- [ ] **Step 6: Commit**

```bash
git add weather-forecast/config.py weather-forecast/calibration.py weather-forecast/tests/test_americas_region.py
git commit -m "Make the spread ceiling regional; americas gets no clamp, not a guess"
```

---

### Task 12: The METAR day window follows DST

**Files:**
- Modify: `weather-forecast/clients/metar_client.py` (the two `station.utc_offset_hours` reads in `ingest_missing_days`)
- Test: `weather-forecast/tests/test_americas_region.py` (append)

**Interfaces:**
- Consumes: `config.current_utc_offset_hours` (existing).
- Produces: nothing new.

Pre-existing defect. Europe already carries it: a DST-observing station's day window is shifted an hour for most of the year, so 23:00–00:00 local on day D−1 is attributed to day D. Fixed here because the Americas add twelve more DST observers and this is the file the work is already in.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_americas_region.py`:

```python
class TestMetarDayWindowFollowsDst:
    def test_the_window_uses_the_live_offset_not_the_static_field(self, monkeypatch):
        """
        EGLC is utc_offset_hours=0 (GMT, the STANDARD-time field) but
        Europe/London in August is BST, +1. A day window built on 0 is
        shifted an hour and mis-attributes the last hour of the previous
        local day.
        """
        import config
        from clients import metar_client

        st = config.get_station("EGLC")
        assert st.utc_offset_hours == 0
        assert st.iana_timezone == "Europe/London"

        seen = []
        monkeypatch.setattr(
            metar_client, "_local_day_window_utc",
            lambda day, offset: (seen.append(offset) or (0, 0)),
        )
        monkeypatch.setattr(metar_client, "fetch_metars", lambda *a, **k: [])
        metar_client.ingest_missing_days("EGLC", max_lookback_days=1)

        assert seen, "the day window was never built"
        assert all(
            o == config.current_utc_offset_hours("EGLC") for o in seen
        ), f"day window built on {seen}, expected the live DST-aware offset"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_americas_region.py -q -k MetarDayWindow`
Expected: FAIL — the window is built on `0`, not on the live offset.

- [ ] **Step 3: Use the live offset**

In `weather-forecast/clients/metar_client.py`, replace both
`station.utc_offset_hours` reads inside `ingest_missing_days` with
`config.current_utc_offset_hours(station)`, and add above the first:

```python
    # The LIVE offset, not the static winter field. station.utc_offset_hours
    # is STANDARD time by design (see StationConfig.iana_timezone); building
    # a local-day window on it shifts the window an hour for every
    # DST-observing station for most of the year, which attributes
    # 23:00-00:00 local on D-1 to day D. That is a whole observation in the
    # wrong day, and the daily MAX is what settles the market.
    offset = config.current_utc_offset_hours(station)
```

then use `offset` at both sites.

- [ ] **Step 4: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS. If `tests/test_hko_ingest.py` or `tests/test_resolution_source.py` fail, check whether they pinned the static offset deliberately — if so, update the test to the live offset and say why in the commit.

- [ ] **Step 5: Commit**

```bash
git add weather-forecast/clients/metar_client.py weather-forecast/tests/test_americas_region.py
git commit -m "Build the METAR day window on the live offset, not the winter one"
```

---

### Task 13: Registry tests admit a wider world

**Files:**
- Modify: `weather-forecast/tests/test_station_registry.py` (two assertions)

**Interfaces:**
- Consumes: `bucket_axis.for_station` (Task 1).
- Produces: nothing.

- [ ] **Step 1: Fix the bucket-span assertion**

`test_bucket_span_is_eleven_for_every_station` counts VALUES, which is 21 for
a step-2 station. Replace its body with:

```python
def test_bucket_span_is_eleven_for_every_station():
    import bucket_axis

    for icao, st in config.STATIONS.items():
        axis = bucket_axis.for_station(st)
        span = (st.bucket_max_c - st.bucket_min_c) // axis.step + 1
        assert span == config.EXPECTED_BUCKET_COUNT, (
            f"{icao}: bucket window {st.bucket_min_c}-{st.bucket_max_c} at step "
            f"{axis.step} is {span} buckets, expected {config.EXPECTED_BUCKET_COUNT}"
        )
        assert (st.bucket_max_c - st.bucket_min_c) % axis.step == 0, (
            f"{icao}: window {st.bucket_min_c}-{st.bucket_max_c} is not a whole "
            f"number of step-{axis.step} buckets"
        )
```

- [ ] **Step 2: Fix the offset assertion**

Replace `test_utc_offset_hours_in_registered_timezones` with:

```python
def test_utc_offset_hours_in_registered_timezones():
    # 5/8/9 are the Asian registry. 0/1 are European STANDARD-time offsets.
    # -3..-8 are the Americas, also STANDARD time (see
    # StationConfig.iana_timezone -- the live path resolves DST via
    # config.current_utc_offset_hours(); this static field is what the
    # backtest reads).
    allowed = (-8, -7, -6, -5, -3, 0, 1, 5, 8, 9)
    for icao, st in config.STATIONS.items():
        assert st.utc_offset_hours in allowed, (
            f"{icao}: unexpected utc_offset_hours {st.utc_offset_hours}"
        )
```

- [ ] **Step 3: Run the suite**

Run: `cd weather-forecast && python -m pytest tests/test_station_registry.py -q`
Expected: all PASS (still 20 stations, all Celsius, all positive offsets — the
widened assertions are a no-op today and the tripwire for Tasks 14 and 16).

- [ ] **Step 4: Commit**

```bash
git add weather-forecast/tests/test_station_registry.py
git commit -m "Let the registry tests describe a registry with more than one axis"
```

---

### Task 14: Research and register the four Celsius cities

**Files:**
- Create: `docs/superpowers/research/2026-08-27-americas-station-facts.md`
- Modify: `weather-forecast/config.py` (`STATIONS`)
- Test: `weather-forecast/tests/test_americas_region.py` (append)

**Interfaces:**
- Consumes: everything above.
- Produces: four `StationConfig` entries with `region="americas"`, `bucket_unit="C"`, `bucket_step=1`.

**Research first, registry second. Do not guess a single field.** Follow the
methodology in `docs/superpowers/research/2026-08-24-europe-station-facts.md`,
which handles the same constraint: `gamma-api.polymarket.com` and
`polymarket.com` are network-blocked, so the event data comes from a
third-party mirror and every registry-critical fact is cross-checked against
a second independent source.

- [ ] **Step 1: Do the research pass**

For Toronto, Mexico City, São Paulo and Buenos Aires, establish and write
down, with the source for each:

- ICAO (cross-check the mirror against the Wikipedia airport infobox)
- exact station name on the market's own resolution-source link
- `wunderground_slug`, cross-checked against the Wunderground history page's
  own station header for that ICAO
- lat/lon
- live METAR presence on `aviationweather.gov/api/data/metar`, and whether
  the report carries a `T`-group (0.1 °C) or whole degrees only
- `official_client_key` — whether the city is in the WWIS list
  (`worldweather.wmo.int`), fetched directly, not via the mirror
- `resolution_grade_source` — what the market's rules text actually names
- the live bucket window (`bucket_min_c`/`bucket_max_c`) and, critically,
  **confirm the unit is Celsius and the step is 1** for all four
- `iana_timezone` and standard `utc_offset_hours`, verified against the tz
  database — the spec's table says Toronto observes DST and the other three
  do not; confirm rather than copy

Record any station whose facts do not resolve cleanly as an open item, and
**do not register it** — the Europe pass caught VHHH's settlement-source
override and OPKC's station-identity ambiguity exactly this way.

Write it all to `docs/superpowers/research/2026-08-27-americas-station-facts.md`,
including an explicit note on which sources were reachable and which were
blocked, and an explicit statement that nothing fetched contained text
addressed to the agent.

- [ ] **Step 2: Write the failing test**

Append to `weather-forecast/tests/test_americas_region.py`:

```python
AMERICAS_CELSIUS = ("CYYZ", "MMMX", "SBGR", "SAEZ")  # replace with researched ICAOs


class TestTheFourCelsiusCities:
    def test_they_are_registered_in_the_americas_region(self):
        for icao in AMERICAS_CELSIUS:
            assert icao in config.STATIONS, icao
            assert config.STATIONS[icao].region == "americas", icao

    def test_they_are_on_the_default_axis(self):
        import bucket_axis

        for icao in AMERICAS_CELSIUS:
            assert bucket_axis.for_station(config.STATIONS[icao]).is_default, icao

    def test_none_of_them_may_trade_real_money(self):
        for icao in AMERICAS_CELSIUS:
            assert icao not in config.LIVE_TRADING_STATIONS, icao
            assert not config.live_mode_is_permitted(icao), icao

    def test_only_toronto_observes_dst(self):
        assert config.STATIONS["CYYZ"].iana_timezone == "America/Toronto"
        for icao in ("MMMX", "SBGR", "SAEZ"):
            assert config.STATIONS[icao].iana_timezone is None, icao

    def test_they_land_in_their_own_scheduler_groups(self):
        import scheduler

        groups = scheduler.stations_by_utc_offset()
        asia_europe = {
            icao for icao, st in config.STATIONS.items()
            if st.region in ("asia", "europe")
        }
        for offset, icaos in groups.items():
            if any(i in AMERICAS_CELSIUS for i in icaos):
                assert not (set(icaos) & asia_europe), (
                    f"offset {offset} mixes an Americas station with "
                    f"another region's"
                )
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_americas_region.py -q -k FourCelsiusCities`
Expected: FAIL — `assert 'CYYZ' in config.STATIONS`.

- [ ] **Step 4: Write the four registry entries**

Add them to `config.py`'s `STATIONS`, following the shape of the European
entries exactly. Every field comes from Step 1's research document, not from
this plan. Mark `long_term_normal_max_c` placeholders with **the month they
were taken in**, per spec §10(f):

```python
    "SBGR": StationConfig(
        icao="SBGR",
        display_name="<from research>",
        country="Brazil",
        lat=...,
        lon=...,
        wunderground_slug="...",
        long_term_normal_max_c=...,  # PLACEHOLDER -- midpoint of the live
                                     # bucket window as read in AUGUST 2026,
                                     # which is SOUTHERN-HEMISPHERE WINTER.
                                     # Not a year-round normal. Sao Paulo's
                                     # annual swing is larger than any
                                     # previously registered city's.
        official_client_key="...",
        wwis_city_name="...",
        polymarket_city_slug="...",
        region="americas",
        iana_timezone=None,          # Brazil abolished DST in 2019 -- verified
        utc_offset_hours=-3,
        bucket_min_c=...,
        bucket_max_c=...,
        bucket_unit="C",
        bucket_step=1,
        metar_ingest_mode="resolution",
        resolution_grade_source="...",
    ),
```

Also update the "how to add a station" comment at the top of `config.py` to
mention `bucket_unit`/`bucket_step` alongside `region` and `iana_timezone`.

- [ ] **Step 5: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS. `test_station_registry.py` now exercises the widened
assertions from Task 13 against real negative offsets.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/research/2026-08-27-americas-station-facts.md weather-forecast/config.py weather-forecast/tests/test_americas_region.py
git commit -m "Register the four Celsius Americas cities, collection-only"
```

---

### Task 15: The `americas` dashboard page

**Files:**
- Modify: `deploy/setup_dashboard.sh` (the generator exec line)
- Modify: `weather-forecast/tests/test_realmoney_dashboard.py` (the exec-line assertion)

**Interfaces:**
- Consumes: `config.stations_in_region("americas")` (existing).
- Produces: `americas.html`.

`generate_dashboard.py` already takes `--region` and validates it against
`config.REGION_BANKROLL_USD`, so Task 10 made this page possible. The only
work is wiring the deploy so it is actually generated — the 2026-08-25
`europe.html` trap was exactly this: a page that silently never rendered.

- [ ] **Step 1: Write the failing test**

In `weather-forecast/tests/test_realmoney_dashboard.py`, extend the existing
exec-line test (the one asserting `generate_dashboard.py --region asia` and
`--region europe` appear) with:

```python
    assert "generate_dashboard.py --region americas" in exec_line, (
        "americas.html would silently never render -- the same trap "
        "europe.html hit on 2026-08-25"
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k exec`
Expected: FAIL on the new assertion.

- [ ] **Step 3: Wire the deploy**

In `deploy/setup_dashboard.sh`, add `--region americas` alongside the
existing asia and europe invocations, following their exact form.

- [ ] **Step 4: Verify the page renders**

Run: `cd weather-forecast && python ../deploy/generate_dashboard.py --region americas`
Expected: exits 0 and writes `americas.html`. Open it and confirm it lists
only the four Americas stations, and shows this region's caps rather than
Asia's.

- [ ] **Step 5: Run the full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add deploy/setup_dashboard.sh weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Generate americas.html, so the region has a page that exists"
```

**Deploy note, not a code step:** the dashboard generators on the box are
frozen copies in `/usr/local/bin` that `git pull` does not update (memory:
`ec2-deployment`). Task 9's rendering fixes and this page reach production
only when they are explicitly reinstalled. Do not mark this task done on the
assumption a pull is enough.

---

# PHASE 3 — The eleven Fahrenheit cities

**Gate:** do not begin Phase 3 until Task 8's sweep and the full suite are green, and Task 16's settlement question is answered.

---

### Task 16: Pin the settlement source before arming anything

**Files:**
- Modify: `docs/superpowers/research/2026-08-27-americas-station-facts.md`

**Interfaces:** none. This task produces an ANSWER, not code.

The spec's one deliberately-open item. American markets resolve on NOAA's
whole-degree Fahrenheit `Temp` column, but there are three candidate NOAA
products and they disagree by 1–2 °F:

1. the hourly-observation `Temp` column at `weather.gov/wrh/timeseries?site=<icao>`
2. the ASOS 6-hour maximum group (`1xxxx` in the METAR, surfaced as `maxT`)
3. the CLI daily climate report

(2) and (3) carry peaks reached BETWEEN hourly observations, so
max-over-observations can understate the daily max. Whichever the market
actually reads decides whether `daily_max_temp_c` is the right input at all.

- [ ] **Step 1: Read one American market's rules text verbatim**

Via the same mirror the Europe research used. Record the resolution sentence
word for word, including which URL it links.

- [ ] **Step 2: Take one already-settled American day and compute all three**

Pick a date at least two days past. From `aviationweather.gov`, compute
max-over-hourly-obs in °C, convert with `floor(c*9/5+32 + 0.5)`. Separately
read that day's `maxT` and the CLI daily max. Record all three °F values and
which bucket each implies on that day's window.

- [ ] **Step 3: Compare against how the market actually resolved**

Record which of the three matches the settled bucket. If they all agree that
day, repeat on a day where they diverge — a day with a sharp afternoon peak
between obs is the one that separates them.

- [ ] **Step 4: Check `MIN_REPORTS_PER_DAY` against real US volumes**

`clients/metar_client.py` sets `MIN_REPORTS_PER_DAY = 24`, the coverage floor
below which a day is not counted. It was tuned for a half-hourly tropical
airport. Count the actual observations `aviationweather.gov` returns for one
American station over one local day and record whether 24 is a floor these
stations clear routinely, marginally, or not at all. A floor set too high
silently discards settlement days, which stalls
`MIN_RESOLUTION_OBS_BEFORE_ENTRY` forever without ever erroring.

- [ ] **Step 5: Write the finding**

Append a section to the research doc stating which product settles these
markets, the evidence, and — if the answer is (2) or (3) — an explicit note
that `daily_max_temp_c` is NOT the correct settlement input for American
stations and that Task 17 must not proceed until that is designed. **If the
evidence is ambiguous, say so and stop.** Registering eleven stations on a
guessed settlement rule is how a whole cohort mis-settles.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/research/2026-08-27-americas-station-facts.md
git commit -m "Pin which NOAA product settles the American temperature markets"
```

---

### Task 17: Research and register the eleven Fahrenheit cities

**Files:**
- Modify: `docs/superpowers/research/2026-08-27-americas-station-facts.md`
- Modify: `weather-forecast/config.py` (`STATIONS`)
- Modify: `weather-forecast/tests/test_bucket_axis.py` (narrow the Phase-1 tripwire)
- Test: `weather-forecast/tests/test_americas_region.py` (append)

**Interfaces:**
- Consumes: everything above, plus Task 16's answer.
- Produces: eleven `StationConfig` entries with `region="americas"`, `bucket_unit="F"`, `bucket_step=2`.

- [ ] **Step 1: Do the research pass**

Same fields and the same two-independent-sources discipline as Task 14, for:
New York City, Atlanta, Miami, Chicago, Houston, Dallas, Austin, Denver, Los
Angeles, San Francisco, Seattle.

Additionally, for each: **read the live bucket labels verbatim and confirm
the unit is Fahrenheit and the interior buckets are two degrees wide.** The
spec asserts this from one NYC event; eleven cities is eleven separate
claims. A city listing one-degree Fahrenheit buckets is a different axis
(`("F", 1)`) and is registered as such, not forced onto step 2.

Record each city's `bucket_min_c`/`bucket_max_c` as the LOWER-EDGE keys —
for the NYC window that is 68 and 88, not 69 and 88.

- [ ] **Step 2: Narrow the Phase-1 tripwire**

In `weather-forecast/tests/test_bucket_axis.py`,
`test_every_registered_station_is_on_the_default_axis_today` and
`TestPhaseOneChangedNothing.test_every_registered_station_is_still_on_the_default_axis`
both assert every station is Celsius. Narrow them to the Asia and Europe
regions, and say why in the same edit:

```python
    def test_every_asia_and_europe_station_is_still_on_the_default_axis(self):
        # NOT every station any more -- the Americas Fahrenheit cohort is
        # the whole point of the axis. This still pins the byte-for-byte
        # constraint for the 20 stations that predate it.
        import config

        for icao, st in config.STATIONS.items():
            if st.region in ("asia", "europe"):
                assert bucket_axis.for_station(st).is_default, icao
```

- [ ] **Step 3: Write the failing test**

Append to `weather-forecast/tests/test_americas_region.py`:

```python
AMERICAS_FAHRENHEIT = (  # replace with researched ICAOs
    "KLGA", "KATL", "KMIA", "KORD", "KIAH", "KDFW",
    "KAUS", "KDEN", "KLAX", "KSFO", "KSEA",
)


class TestTheElevenFahrenheitCities:
    def test_they_declare_a_fahrenheit_step_two_axis(self):
        import bucket_axis

        for icao in AMERICAS_FAHRENHEIT:
            axis = bucket_axis.for_station(config.STATIONS[icao])
            assert axis.unit == "F", icao
            assert axis.step == 2, icao

    def test_their_windows_are_eleven_buckets_on_a_step_two_grid(self):
        for icao in AMERICAS_FAHRENHEIT:
            st = config.STATIONS[icao]
            assert (st.bucket_max_c - st.bucket_min_c) % 2 == 0, icao
            assert (st.bucket_max_c - st.bucket_min_c) // 2 + 1 == 11, icao

    def test_none_of_them_may_trade_real_money(self):
        for icao in AMERICAS_FAHRENHEIT:
            assert icao not in config.LIVE_TRADING_STATIONS, icao
            assert not config.live_mode_is_permitted(icao), icao

    def test_pricing_one_of_them_without_an_axis_raises(self):
        from datetime import date

        import probability
        from models import CalibratedEstimate

        est = CalibratedEstimate(
            station_icao=AMERICAS_FAHRENHEIT[0], target_date=date(2026, 8, 27),
            central_estimate_c=26.0, std_dev_c=1.0, monsoon_phase="unknown",
        )
        with pytest.raises(ValueError, match="axis"):
            probability.bucket_probabilities(est, 68, 88)

    def test_their_labels_read_back_as_the_market_prints_them(self):
        import bucket_axis

        st = config.STATIONS[AMERICAS_FAHRENHEIT[0]]
        axis = bucket_axis.for_station(st)
        lo, hi = st.bucket_min_c, st.bucket_max_c
        assert axis.label(lo, lo, hi).endswith("°F or below")
        assert axis.label(hi, lo, hi).endswith("°F or higher")
        assert "-" in axis.label(lo + 2, lo, hi)
```

- [ ] **Step 4: Run it to verify it fails**

Run: `cd weather-forecast && python -m pytest tests/test_americas_region.py -q -k ElevenFahrenheit`
Expected: FAIL — the stations are not registered.

- [ ] **Step 5: Write the eleven registry entries**

Same shape as Task 14, with `bucket_unit="F"`, `bucket_step=2`, and the
lower-edge window from Step 1. Every field from research.

- [ ] **Step 6: Run the full suite, and re-run the sweep specifically**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS. Task 8's parametrised sweep now runs over eleven real
Fahrenheit stations rather than only the two synthetic axes — a containment
or tiling failure here is a real registry error, most likely a window whose
bounds were recorded as printed numbers rather than lower-edge keys.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/research/2026-08-27-americas-station-facts.md weather-forecast/config.py weather-forecast/tests/test_bucket_axis.py weather-forecast/tests/test_americas_region.py
git commit -m "Register the eleven Fahrenheit Americas cities on their own axis"
```

---

### Task 18: Final verification

**Files:** none modified.

- [ ] **Step 1: Full suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: all PASS. Record the count.

- [ ] **Step 2: Confirm no region can trade real money by accident**

Run:

```bash
cd weather-forecast && python -c "
import config
for r in sorted(config.REGION_BANKROLL_USD):
    print(r, 'live_authorised=', config.region_authorises_live_orders(r))
for icao in sorted(config.STATIONS):
    st = config.STATIONS[icao]
    if st.region == 'americas':
        assert not config.live_mode_is_permitted(icao), icao
print('all americas stations refused live mode: OK')
print('total stations:', len(config.STATIONS))
"
```

Expected: `americas live_authorised= False`, the assertion passes, and the
total is 35.

- [ ] **Step 3: Confirm the axis is still a no-op for the original twenty**

Run: `cd weather-forecast && python -m pytest tests/test_bucket_axis.py -q -k "PhaseOneChangedNothing or AxisPropertiesHold"`
Expected: all PASS.

- [ ] **Step 4: Report honestly**

State the test count, anything skipped, and every open item still outstanding
— at minimum: whether Task 16 resolved the settlement-source question or
merely documented it, and the fact that the frozen `/usr/local/bin` dashboard
generators have not been reinstalled by any step in this plan.

Do NOT claim the Americas region is trading. It is collection-only, needs at
least `MIN_RESOLUTION_OBS_BEFORE_ENTRY` days of observations, and its spread
ceiling is deliberately unset.
