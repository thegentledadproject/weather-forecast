# Europe Market Isolation Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register European temperature markets in a `region`-scoped framework that isolates them from the Asia book on capital, real-money blast radius, and DST-correct scheduling.

**Architecture:** Two new `StationConfig` fields (`region`, `iana_timezone`) become the keys for everything else. `config.current_utc_offset_hours()` layers live DST resolution over the existing static-int offset without changing any Asia station's behavior. Per-region dicts replace three classes of process-global scalar (Kelly bankroll, daily portfolio exposure, live blast radius), each keyed off a station's `region`, with Europe pinned at zero so it is structurally incapable of spending money until an operator raises it deliberately.

**Tech Stack:** Python 3.9+, stdlib `zoneinfo` (no new dependency), sqlite3 via `storage.py`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-europe-market-isolation-design.md`

## Global Constraints

- **Run tests from the code directory** `weather-forecast/` with `python -m pytest tests -q` (see `tests/conftest.py` — it inserts the code dir on `sys.path`).
- **Baseline: 716 tests pass** at the commit this plan was written against. Every "same count as before" step means 716 plus whatever that task's own new tests add.
- **No new pip dependency.** `zoneinfo` is stdlib from Python 3.9. Do not add `pytz` or `dateutil`.
- **Asia behavior must not change.** Every existing station keeps `region="asia"` and no `iana_timezone`. Any test asserting a pre-existing Asia number must still pass with the identical number.
- **Europe is funded at zero.** `REGION_BANKROLL_USD["europe"] = 0.0`, `REGION_MAX_DAILY_EXPOSURE_USD["europe"] = 0.0`, `REGION_LIVE_MAX_CONCURRENT_POSITIONS["europe"] = 0`, `REGION_LIVE_MAX_TOTAL_EXPOSURE_USD["europe"] = 0.0`, `REGION_LIVE_MAX_ORDERS_PER_DAY["europe"] = 0`. Do not "helpfully" set a non-zero starting value.
- **Asia's region values reference the existing constants, never duplicate the literals.** `REGION_BANKROLL_USD = {"asia": BANKROLL_USD, ...}` — not `{"asia": 1000.0, ...}`.
- **No European station goes into `config.LIVE_TRADING_STATIONS`.** It stays `{"WSSS", "RCSS"}`.
- Existing constants this plan depends on, verified at their current values: `BANKROLL_USD = 1000.0`, `MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD = 400.0`, `LIVE_MAX_CONCURRENT_POSITIONS = 5`, `LIVE_MAX_TOTAL_EXPOSURE_USD = 8.00`, `LIVE_MAX_ORDERS_PER_DAY = 10`, `EXPECTED_BUCKET_COUNT = 11`, `MIN_RESOLUTION_OBS_BEFORE_ENTRY = 10`.

---

## File Structure

**Modified:**
- `models.py` — `StationConfig` gains `region` and `iana_timezone`. Dataclass field definitions only; no logic.
- `config.py` — `current_utc_offset_hours()` and the six `REGION_*` dicts plus their lookup helpers; `local_today()`/`local_day_bounds_utc()` rewired; 7 new `STATIONS` entries; 7 new `MATURITY_SNAPSHOT` entries.
- `scheduler.py` — `stations_by_utc_offset()` reads the new helper.
- `entry_manager.py` — region-scoped Kelly bankroll and portfolio-exposure summing.
- `executor.py` — `_live_budget_breach()` takes a station and filters to its region.
- `storage.py` — `count_live_order_attempts()` gains an optional station filter.

**Modified (existing tests that assert registry shape and WILL fail once stations are added — Task 7 updates them):**
- `tests/test_station_registry.py` — `EXPECTED_STATION_COUNT`, offset allowlist, per-station default assertions.
- `tests/test_scheduler_groups.py` — asserts `set(groups) == {5, 8, 9}`.
- `tests/test_live_execution.py`, `tests/test_settled_token_wiring.py` — four `count_live_order_attempts` monkeypatch lambdas whose arity changes.

**Created:**
- `tests/test_region_isolation.py` — every new region/DST behavior in one file. These belong together: they are one feature with one set of fixtures, and the codebase already groups by feature (`test_portfolio_caps.py`, `test_scheduler_groups.py`) rather than by module.
- `docs/superpowers/research/2026-08-24-europe-station-facts.md` — Task 6's output, the confirmed per-station facts the registry entries are written from.

---

### Task 1: `StationConfig` region and timezone fields

**Files:**
- Modify: `models.py` — the `StationConfig` dataclass, immediately after its `utc_offset_hours` field
- Test: `tests/test_region_isolation.py` (create)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `StationConfig.region: str` (default `"asia"`) and `StationConfig.iana_timezone: Optional[str]` (default `None`). Every later task reads these two names.

- [ ] **Step 1: Write the failing test**

Create `tests/test_region_isolation.py`:

```python
"""
tests/test_region_isolation.py

Regression tests for the 2026-08-24 Europe market isolation framework
(docs/superpowers/specs/2026-08-24-europe-market-isolation-design.md).

Three separate isolation mechanisms are covered here, and they are NOT
the same mechanism -- conflating them is how a gap gets left open:

  1. StationConfig.region + iana_timezone -- the keys everything else
     is looked up by.
  2. Region-scoped SIMULATION/PAPER budget (Kelly bankroll, portfolio
     daily exposure).
  3. Region-scoped LIVE blast radius (concurrent positions, total live
     exposure, daily order rate). Live orders never pass through Kelly
     sizing at all (config.LIVE_TRADE_SIZE_USD replaces it), so (2)
     does nothing whatsoever for (3).

Plus the DST-aware offset that makes any of it correct for a region
whose clock moves twice a year.
"""

import pytest

import config
from models import StationConfig


def _station(**overrides) -> StationConfig:
    """A minimal valid StationConfig; override only what a test is about."""
    base = dict(
        icao="TEST",
        display_name="Test Station",
        country="Testland",
        lat=0.0,
        lon=0.0,
        wunderground_slug="tl/test/TEST",
        long_term_normal_max_c=30.0,
        official_client_key="wwis",
    )
    base.update(overrides)
    return StationConfig(**base)


class TestStationConfigRegionFields:
    def test_region_defaults_to_asia(self):
        assert _station().region == "asia"

    def test_iana_timezone_defaults_to_none(self):
        assert _station().iana_timezone is None

    def test_region_and_timezone_are_settable(self):
        st = _station(region="europe", iana_timezone="Europe/London")
        assert st.region == "europe"
        assert st.iana_timezone == "Europe/London"

    def test_every_registered_station_today_is_asia(self):
        """
        The default is load-bearing: it is what keeps all 13 existing
        entries in one pool with zero edits to them.
        """
        for icao, st in config.STATIONS.items():
            if st.iana_timezone is None:
                assert st.region == "asia", f"{icao}: non-Asia station must set iana_timezone"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_region_isolation.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'region'` on `test_region_and_timezone_are_settable`, and `AttributeError: 'StationConfig' object has no attribute 'region'` on the others.

- [ ] **Step 3: Write minimal implementation**

In `models.py`, directly after the `utc_offset_hours: int = 8` field and before the `bucket_min_c` comment block, add:

```python
    # Which capital pool and blast radius this station draws from. Every
    # per-region limit in config.py (REGION_BANKROLL_USD,
    # REGION_MAX_DAILY_EXPOSURE_USD, REGION_LIVE_MAX_*) is keyed off this
    # string. Defaults to "asia" so the 13 stations registered before
    # 2026-08-24 keep the single shared pool they already had, unedited.
    region: str = "asia"

    # IANA timezone name, e.g. "Europe/London". When set, it OVERRIDES
    # utc_offset_hours on the live trading path via
    # config.current_utc_offset_hours() -- the offset is resolved from the
    # tz database at call time, so a DST-observing station is correct in
    # both halves of the year. When None (every Asian station), the static
    # utc_offset_hours above is used exactly as before.
    #
    # utc_offset_hours MUST STILL BE SET even when this is: the backtest
    # engine reads station.utc_offset_hours directly (backtest/engine.py)
    # and has no notion of a moving clock. Set it to the station's
    # STANDARD-time (winter) offset.
    iana_timezone: Optional[str] = None
```

Confirm `Optional` is already imported in `models.py` (it is — used by `PointForecast.max_temp_c`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_region_isolation.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite for regressions**

Run: `python -m pytest tests -q`
Expected: PASS, same count as before this task. Adding defaulted dataclass fields changes no existing behavior.

- [ ] **Step 6: Commit**

```bash
git add models.py tests/test_region_isolation.py
git commit -m "Add region and iana_timezone to StationConfig"
```

---

### Task 2: DST-aware offset resolution

**Files:**
- Modify: `config.py` — `local_today()` and `local_day_bounds_utc()`, near the top of the file
- Test: `tests/test_region_isolation.py`

**Interfaces:**
- Consumes: `StationConfig.region`, `StationConfig.iana_timezone` (Task 1).
- Produces: `config.current_utc_offset_hours(station: Union[str, StationConfig, None]) -> int`. Tasks 3 and 7 call it. Accepts the same three argument shapes `local_today()` already does: a `StationConfig`, an ICAO string, or `None` (legacy `LOCAL_UTC_OFFSET_HOURS` default).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_region_isolation.py`:

```python
from datetime import date, datetime, timezone


class TestCurrentUtcOffsetHours:
    def test_station_without_iana_timezone_returns_the_static_int(self):
        st = _station(utc_offset_hours=9)
        assert config.current_utc_offset_hours(st) == 9

    def test_none_returns_the_legacy_default(self):
        assert config.current_utc_offset_hours(None) == config.LOCAL_UTC_OFFSET_HOURS

    def test_icao_string_is_accepted(self):
        # WSSS is UTC+8 and sets no iana_timezone.
        assert config.current_utc_offset_hours("WSSS") == 8

    def test_every_existing_station_is_unchanged_by_the_new_helper(self):
        """
        The helper must be a strict superset of the old field read. If this
        ever fails, an Asia station's trading day just moved.
        """
        for icao, st in config.STATIONS.items():
            if st.iana_timezone is None:
                assert config.current_utc_offset_hours(st) == st.utc_offset_hours, icao

    def test_london_is_plus_one_in_summer_and_zero_in_winter(self):
        """
        The whole reason this design exists. Europe/London is BST (+1) in
        August and GMT (+0) in December; a static int is wrong for one of
        them no matter which value is chosen.
        """
        st = _station(region="europe", iana_timezone="Europe/London", utc_offset_hours=0)

        summer = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        winter = datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc)

        assert config.current_utc_offset_hours(st, at=summer) == 1
        assert config.current_utc_offset_hours(st, at=winter) == 0

    def test_warsaw_is_plus_two_in_summer_and_plus_one_in_winter(self):
        st = _station(region="europe", iana_timezone="Europe/Warsaw", utc_offset_hours=1)

        summer = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        winter = datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc)

        assert config.current_utc_offset_hours(st, at=summer) == 2
        assert config.current_utc_offset_hours(st, at=winter) == 1

    def test_an_unknown_timezone_name_fails_loudly(self):
        """
        A typo'd tz name must not silently fall back to the static int --
        that would trade a DST-observing station on a wrong clock while
        looking fine.
        """
        st = _station(region="europe", iana_timezone="Europe/Nowhere", utc_offset_hours=1)
        # ZoneInfoNotFoundError subclasses KeyError -- assert the specific
        # type, so this cannot pass because of some unrelated failure.
        with pytest.raises(KeyError):
            config.current_utc_offset_hours(st)


class TestLocalTodayUsesTheHelper:
    def test_local_today_respects_a_dst_offset(self, monkeypatch):
        """
        local_today() must route through current_utc_offset_hours(), not
        read the field. At 23:30 UTC on 2026-08-24, a BST (+1) station is
        already on 2026-08-25.
        """
        st = _station(region="europe", iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setitem(config.STATIONS, "TEST", st)

        frozen = datetime(2026, 8, 24, 23, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(config, "_now_utc", lambda: frozen)

        assert config.local_today("TEST") == date(2026, 8, 25)

    def test_local_day_bounds_use_the_offset_of_the_day_being_bounded(self, monkeypatch):
        """
        The BST local day for 2026-08-24 starts at 23:00Z on the 23rd.

        _now_utc is frozen to DECEMBER on purpose while target_date is in
        AUGUST. The two are in opposite DST states, so this test can only
        pass if the offset is resolved from target_date. Resolving it from
        the wall clock -- what a bare current_utc_offset_hours(station) call
        does -- returns GMT here and moves both bounds by an hour.

        Getting this wrong is the lookahead bug local_day_bounds_utc's own
        docstring was written about, one region over. Note also that a test
        which did NOT freeze the clock would pass or fail depending on which
        month it happened to run in.
        """
        st = _station(region="europe", iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setitem(config.STATIONS, "TEST", st)
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc))

        start, end = config.local_day_bounds_utc("TEST", date(2026, 8, 24))

        assert start == datetime(2026, 8, 23, 23, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc)

    def test_local_day_bounds_in_the_other_direction_too(self, monkeypatch):
        """The mirror: a GMT target_date bounded while the wall clock says August."""
        st = _station(region="europe", iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setitem(config.STATIONS, "TEST", st)
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc))

        start, end = config.local_day_bounds_utc("TEST", date(2026, 12, 24))

        assert start == datetime(2026, 12, 24, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 12, 25, 0, 0, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_region_isolation.py -v`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'current_utc_offset_hours'`.

- [ ] **Step 3: Write minimal implementation**

In `config.py`, add the `zoneinfo` import at the top alongside the existing `datetime` import:

```python
from zoneinfo import ZoneInfo
```

Add a seam for the clock so tests can freeze it, immediately above `local_today()`:

```python
def _now_utc() -> datetime:
    """
    The current instant, as one seam. Exists so tests can freeze the clock
    without monkeypatching the datetime module itself -- the DST helpers
    below are entirely about what time it is, and cannot be tested against
    a real clock that is only in one DST state at a time.
    """
    return datetime.now(timezone.utc)


def current_utc_offset_hours(
    station: Optional[Union[str, StationConfig]] = None,
    at: Optional[datetime] = None,
) -> int:
    """
    A station's UTC offset AT A GIVEN INSTANT, in whole hours.

    WHY THIS IS NOT JUST station.utc_offset_hours. That field is a static
    int, and its own docstring records why that was acceptable: "NONE of
    the registered cities observes DST". Every European city does. A
    station carrying an iana_timezone is resolved against the tz database
    at call time, so it is correct in both halves of the year; a station
    without one keeps the static int, unchanged, forever.

    `at` defaults to now. Passing it is how the tests reach both DST
    states, and how any caller reasoning about a PAST instant stays honest.
    local_day_bounds_utc() is exactly such a caller and MUST pass it: it is
    handed historical target_dates, and an offset resolved from the wall
    clock would bound a winter day with a summer offset.

    RAISES on an unknown timezone name rather than falling back to the
    static int. A typo would otherwise trade a DST station on a silently
    wrong clock, which is the exact failure this function exists to
    prevent.
    """
    if station is None:
        return LOCAL_UTC_OFFSET_HOURS

    st = get_station(station) if isinstance(station, str) else station

    if not st.iana_timezone:
        return st.utc_offset_hours

    instant = at if at is not None else _now_utc()
    offset = instant.astimezone(ZoneInfo(st.iana_timezone)).utcoffset()
    return int(offset.total_seconds() // 3600)
```

Note on ordering: `current_utc_offset_hours` calls `get_station()`, which is defined much lower in `config.py` (line 485). That is fine — the call happens at runtime, not at import. Place the function where `local_today()` lives so the three clock functions stay together.

Now rewrite the bodies of `local_today()` and `local_day_bounds_utc()` to delegate. Replace the offset-resolution block in `local_today()`:

```python
def local_today(station: Optional[Union[str, StationConfig]] = None) -> date:
    """
    The current calendar date in a station's market timezone. Accepts a
    StationConfig, an ICAO string, or None (legacy UTC+8 default -- only
    for genuinely station-agnostic contexts).

    Delegates offset resolution to current_utc_offset_hours(), so a
    DST-observing station is correct in both halves of the year.
    """
    offset = current_utc_offset_hours(station)
    return (_now_utc() + timedelta(hours=offset)).date()
```

And in `local_day_bounds_utc()`, replace the four-line offset ternary. **The offset must be resolved AT THE DAY BEING BOUNDED, not at the moment the function runs** — this function is called with historical `target_date`s (`storage.forecast_means_by_date` iterates every stored forecast row; `spread_audit._day_end_utc` does the same), so resolving "now" would bound a December day with August's BST offset. That is the same class of hour-scale boundary error this function's own docstring was written about. Move the `midnight` computation above the offset lookup and anchor on it:

```python
    midnight = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc
    )
    # AT THE DAY BEING BOUNDED, not at the instant this runs. This function
    # is handed historical target_dates -- storage.forecast_means_by_date
    # walks every stored forecast row -- so resolving the offset from the
    # wall clock would bound a December day with August's summer-time
    # offset, silently moving the boundary this docstring exists to keep
    # honest.
    #
    # UTC midnight of target_date is the anchor rather than the true local
    # midnight, which would be circular: the local instant depends on the
    # offset being looked up. The two can disagree only on a DST transition
    # DAY, where the anchor may pick the wrong side by an hour -- accepted,
    # and far smaller than being wrong for half of every year.
    offset = current_utc_offset_hours(station, at=midnight)
    start = midnight - timedelta(hours=offset)
    return start, start + timedelta(days=1)
```

Leave that function's long docstring exactly as it is — it documents the lookahead bug and is still accurate.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_region_isolation.py -v`
Expected: PASS (all `TestCurrentUtcOffsetHours` and `TestLocalTodayUsesTheHelper` tests)

- [ ] **Step 5: Run the clock regression tests specifically**

Run: `python -m pytest tests/test_local_today.py tests/test_no_lookahead.py tests/test_forecast_lead_window.py -q`
Expected: PASS. These are the tests that pin the existing UTC-date behavior; if any fails, the delegation changed an Asia station's day boundary and must be fixed before continuing.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS, same count as before.

- [ ] **Step 7: Commit**

```bash
git add config.py tests/test_region_isolation.py
git commit -m "Resolve station UTC offset from IANA tz when set"
```

---

### Task 3: Scheduler groups on the resolved offset

**Files:**
- Modify: `scheduler.py` — `stations_by_utc_offset()`
- Test: `tests/test_region_isolation.py`

**Interfaces:**
- Consumes: `config.current_utc_offset_hours()` (Task 2).
- Produces: no new names. `stations_by_utc_offset()` keeps its `{offset: [icao, ...]}` return shape.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_region_isolation.py`:

```python
import scheduler


class TestSchedulerGroupsOnResolvedOffset:
    def test_a_dst_station_groups_by_its_current_offset(self, monkeypatch):
        """
        In August a Europe/London station belongs in the +1 group, not the
        +0 group its static utc_offset_hours names.
        """
        st = _station(icao="TEST", region="europe",
                      iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setattr(config, "STATIONS", {"TEST": st})
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc))

        groups = scheduler.stations_by_utc_offset()

        assert groups == {1: ["TEST"]}

    def test_the_same_station_groups_at_zero_in_winter(self, monkeypatch):
        st = _station(icao="TEST", region="europe",
                      iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setattr(config, "STATIONS", {"TEST": st})
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc))

        groups = scheduler.stations_by_utc_offset()

        assert groups == {0: ["TEST"]}

    def test_asia_stations_group_exactly_as_before(self):
        """The 13 registered stations set no iana_timezone, so nothing moves."""
        groups = scheduler.stations_by_utc_offset()
        for offset, icaos in groups.items():
            for icao in icaos:
                st = config.STATIONS[icao]
                if st.iana_timezone is None:
                    assert offset == st.utc_offset_hours, icao
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_region_isolation.py -k SchedulerGroups -v`
Expected: FAIL — `assert {0: ['TEST']} == {1: ['TEST']}`. The function still reads the static field, so the August case lands in the wrong group.

- [ ] **Step 3: Write minimal implementation**

In `scheduler.py`, change the offset lookup. A station whose timezone name cannot be resolved must be distinguished from one that is not registered at all — the two need different operator responses, and `current_utc_offset_hours` raises `ZoneInfoNotFoundError`, which subclasses `KeyError`, so a single broad handler would report a registered station as "unknown" and silently drop it from trading:

```python
        try:
            offset = config.current_utc_offset_hours(icao)
        except ZoneInfoNotFoundError as exc:
            # REGISTERED, but its iana_timezone is not in the tz database --
            # a typo, or a zone name that has been retired. config.current_utc_
            # offset_hours raises rather than falling back to the static int
            # precisely so this cannot trade on a silently wrong clock; the
            # generic handler below would undo that by reporting it as an
            # unknown station and moving on.
            #
            # Still skip rather than raise: this function's existing stance is
            # that one bad name must not stop the other stations from trading,
            # and that is right. But the message has to say what actually
            # happened, because the consequence is that this station does not
            # trade AT ALL until someone fixes the config.
            print(
                f"[scheduler] {icao} is REGISTERED but its UTC offset could not be "
                f"resolved ({exc}) -- check StationConfig.iana_timezone. It will NOT "
                f"be scheduled and will not trade until this is corrected."
            )
            continue
        except KeyError as exc:
            print(f"[scheduler] skipping unknown station: {exc}")
            continue
```

Add the import at the top of `scheduler.py`:

```python
from zoneinfo import ZoneInfoNotFoundError
```

Then extend that function's docstring with:

```
    The offset is RESOLVED, not read: a station carrying an iana_timezone
    (every European entry) reports its current DST offset, so it joins the
    group whose local clock it actually shares right now. See the
    known limitation in run_forever() -- grouping happens once at startup.

    Two failure modes are skipped rather than raised, and they are NOT the
    same: an ICAO that is not in the registry at all, and a registered
    station whose iana_timezone the tz database does not know. Both let the
    other stations keep trading; only the second means a station you believe
    is live is silently absent from every cycle.
```

Also add the malformed-timezone test alongside the others:

```python
    def test_a_registered_station_with_a_bad_timezone_is_skipped_loudly(
        self, monkeypatch, capsys
    ):
        """
        A typo'd iana_timezone must not be reported as an "unknown station".
        The station IS registered; it simply cannot be scheduled, and the
        log has to say so or it silently stops trading with a message that
        sends the operator looking in the wrong place.
        """
        bad = _station(icao="BADTZ", region="europe", iana_timezone="Europe/Nowhere")
        good = _station(icao="OKTZ", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {"BADTZ": bad, "OKTZ": good})
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc))

        groups = scheduler.stations_by_utc_offset()

        # The healthy station still trades -- one bad config must not stop it.
        assert groups == {1: ["OKTZ"]}

        out = capsys.readouterr().out
        assert "BADTZ" in out
        assert "REGISTERED" in out
        assert "iana_timezone" in out
        assert "unknown station" not in out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_region_isolation.py -k SchedulerGroups -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Document the startup-only grouping limitation**

In `scheduler.py`, inside `run_forever()`'s docstring, after the existing paragraph about each group owning a `next_run_ts`, add:

```
    GROUPS ARE COMPUTED ONCE, HERE, AND NEVER RECOMPUTED. For a station
    with a static utc_offset_hours that is simply true. For a station
    carrying an iana_timezone it is a known limitation: crossing a DST
    transition while the daemon runs leaves that station on its
    pre-transition offset -- every schedule window an hour off its real
    local clock -- until the process restarts. Restart the daemon on each
    BST/CEST transition date. Deliberately not solved with live
    regrouping: it is a twice-a-year event, and the same operator-action
    stance the bucket-bounds resweep takes.
```

- [ ] **Step 6: Run the scheduler tests**

Run: `python -m pytest tests/test_scheduler_groups.py tests/test_region_isolation.py -q`
Expected: PASS. `test_scheduler_groups.py` still asserts `{5, 8, 9}` and still passes — no European station is registered yet.

- [ ] **Step 7: Commit**

```bash
git add scheduler.py tests/test_region_isolation.py
git commit -m "Group scheduler stations by resolved DST offset"
```

---

### Task 4: Region-scoped simulation capital

**Files:**
- Modify: `config.py` — immediately after the `MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD` assignment
- Modify: `entry_manager.py` — the `bankroll_sized_usd = kelly_applied * config.BANKROLL_USD` line in `evaluate_entry()`, the `portfolio_day_exposure_usd()` function, and the `apply_portfolio_budget(...)` call site in `decide_portfolio_entries()`
- Test: `tests/test_region_isolation.py`

**Interfaces:**
- Consumes: `StationConfig.region` (Task 1).
- Produces:
  - `config.REGION_BANKROLL_USD: dict[str, float]`
  - `config.REGION_MAX_DAILY_EXPOSURE_USD: dict[str, float]`
  - `config.region_of(station_icao: str) -> str`
  - `config.region_bankroll_usd(station_icao: str) -> float`
  - `config.region_max_daily_exposure_usd(station_icao: str) -> float`
  - `entry_manager.portfolio_day_exposure_usd(is_paper=None, region=None) -> Optional[float]` — `region=None` preserves the existing all-stations sum for any caller that does not pass one.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_region_isolation.py`:

```python
import entry_manager


class TestRegionScopedCapital:
    def test_asia_values_equal_the_pre_existing_flat_constants(self):
        """
        The region dicts must REFERENCE the old constants, not restate
        them. If someone retunes BANKROLL_USD and Asia's pool does not
        move, this catches it.
        """
        assert config.REGION_BANKROLL_USD["asia"] == config.BANKROLL_USD
        assert (config.REGION_MAX_DAILY_EXPOSURE_USD["asia"]
                == config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD)

    def test_europe_starts_at_zero(self):
        assert config.REGION_BANKROLL_USD["europe"] == 0.0
        assert config.REGION_MAX_DAILY_EXPOSURE_USD["europe"] == 0.0

    def test_region_lookup_helpers_resolve_through_the_station(self, monkeypatch):
        st = _station(icao="TEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setitem(config.STATIONS, "TEST", st)

        assert config.region_of("TEST") == "europe"
        assert config.region_bankroll_usd("TEST") == 0.0
        assert config.region_max_daily_exposure_usd("TEST") == 0.0

    def test_an_asia_station_reads_the_asia_pool(self):
        assert config.region_bankroll_usd("WSSS") == config.BANKROLL_USD
        assert (config.region_max_daily_exposure_usd("WSSS")
                == config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD)

    def test_an_unknown_region_fails_loudly(self, monkeypatch):
        """
        A station naming a region with no funding entry must raise, not
        default to Asia's money.
        """
        st = _station(icao="TEST", region="atlantis")
        monkeypatch.setitem(config.STATIONS, "TEST", st)

        with pytest.raises(KeyError):
            config.region_bankroll_usd("TEST")


class TestRegionScopedPortfolioExposure:
    def test_exposure_sums_only_the_named_region(self, monkeypatch):
        """
        An Asia station's spend must not consume Europe's remaining budget.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        def fake_station_exposure(icao, target_date, is_paper=None):
            return 100.0 if icao == "WSSS" else 0.0

        monkeypatch.setattr(entry_manager, "station_day_exposure_usd", fake_station_exposure)

        assert entry_manager.portfolio_day_exposure_usd(region="europe") == 0.0
        assert entry_manager.portfolio_day_exposure_usd(region="asia") == 100.0

    def test_no_region_still_sums_everything(self, monkeypatch):
        """Back-compat: the parameterless call keeps its old meaning."""
        def fake_station_exposure(icao, target_date, is_paper=None):
            return 1.0

        monkeypatch.setattr(entry_manager, "station_day_exposure_usd", fake_station_exposure)

        assert entry_manager.portfolio_day_exposure_usd() == float(len(config.STATIONS))

    def test_an_unreadable_station_still_fails_closed(self, monkeypatch):
        """The fail-closed rule must survive the region filter."""
        monkeypatch.setattr(entry_manager, "station_day_exposure_usd",
                            lambda icao, target_date, is_paper=None: None)

        assert entry_manager.portfolio_day_exposure_usd(region="asia") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_region_isolation.py -k "RegionScopedCapital or RegionScopedPortfolio" -v`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'REGION_BANKROLL_USD'`.

- [ ] **Step 3: Write the config half**

In `config.py`, immediately after `MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD = 400.0`:

```python
# --- Per-region capital pools ---------------------------------------------
# The portfolio caps above are ONE pool shared by every registered station.
# That was right while the registry was one region with correlated errors
# and one shared thesis. It stops being right the moment a second region is
# registered: a European cohort's drawdown would eat the Asian book's
# sizing budget, and neither cohort's numbers would mean anything about the
# other.
#
# Asia's entries REFERENCE the constants above rather than restating them,
# so retuning BANKROLL_USD still moves the Asian pool and there is exactly
# one number to change.
#
# EUROPE IS FUNDED AT ZERO ON PURPOSE. A station registered into a
# zero-funded region collects data, produces decisions and is scored, but
# Kelly sizing multiplies by 0.0 and every candidate resolves to a $0
# order. Raising these is a deliberate, auditable, one-line operator
# decision -- not a side effect of adding a station to the registry.
REGION_BANKROLL_USD = {
    "asia": BANKROLL_USD,
    "europe": 0.0,
}

REGION_MAX_DAILY_EXPOSURE_USD = {
    "asia": MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD,
    "europe": 0.0,
}


def region_of(station_icao: str) -> str:
    """The capital pool a station draws from. See StationConfig.region."""
    return get_station(station_icao).region


def region_bankroll_usd(station_icao: str) -> float:
    """
    Kelly's bankroll for THIS station's region.

    Raises KeyError on a region with no funding entry rather than falling
    back to a default. A station whose region was typo'd must not quietly
    size against another region's money.
    """
    region = region_of(station_icao)
    if region not in REGION_BANKROLL_USD:
        raise KeyError(
            f"{station_icao} names region {region!r}, which has no entry in "
            f"config.REGION_BANKROLL_USD (known: {list(REGION_BANKROLL_USD)})."
        )
    return REGION_BANKROLL_USD[region]


def region_max_daily_exposure_usd(station_icao: str) -> float:
    """This station's region's portfolio-wide daily exposure cap."""
    region = region_of(station_icao)
    if region not in REGION_MAX_DAILY_EXPOSURE_USD:
        raise KeyError(
            f"{station_icao} names region {region!r}, which has no entry in "
            f"config.REGION_MAX_DAILY_EXPOSURE_USD "
            f"(known: {list(REGION_MAX_DAILY_EXPOSURE_USD)})."
        )
    return REGION_MAX_DAILY_EXPOSURE_USD[region]
```

- [ ] **Step 4: Write the entry_manager half**

In `entry_manager.py`, find the single line assigning `bankroll_sized_usd` and replace it:

```python
    bankroll_sized_usd = kelly_applied * config.BANKROLL_USD
```

with:

```python
    # THIS STATION'S REGION'S bankroll, not one global number. A station in
    # a zero-funded region sizes to $0 here and every cap below is a no-op
    # -- which is the intended state for a newly registered region.
    bankroll_sized_usd = kelly_applied * config.region_bankroll_usd(station_icao)
```

Change `portfolio_day_exposure_usd`'s signature and body:

```python
def portfolio_day_exposure_usd(
    is_paper: Optional[bool] = None,
    region: Optional[str] = None,
) -> Optional[float]:
```

Inside it, replace `for icao in config.STATIONS:` with:

```python
    for icao in config.STATIONS:
        if region is not None and config.region_of(icao) != region:
            continue
```

And extend its docstring with:

```
    `region` scopes the sum to one capital pool. None keeps the original
    all-stations meaning for callers that predate regions. Passing it is
    what stops an Asian drawdown from consuming a European station's
    budget, and vice versa -- the two cohorts share no thesis, so they
    must not share a denominator.
```

At the `apply_portfolio_budget` call site, replace:

```python
    portfolio_usd = portfolio_day_exposure_usd(is_paper=candidate_is_paper)
    if portfolio_usd is None:
        portfolio_usd = config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD
```

with:

```python
    station_region = config.region_of(station_icao)
    portfolio_usd = portfolio_day_exposure_usd(
        is_paper=candidate_is_paper, region=station_region,
    )
    if portfolio_usd is None:
        # Fail closed, same rule as the station cap above: an unknown
        # portion of the region's spend means the total is unknown, and an
        # unknown total must not be treated as a small one.
        portfolio_usd = config.region_max_daily_exposure_usd(station_icao)
```

And in the `apply_portfolio_budget(...)` call immediately below, pass the region's cap explicitly:

```python
    decisions = apply_portfolio_budget(
        decisions,
        existing_exposure_usd=existing_usd,
        portfolio_exposure_usd=portfolio_usd,
        max_portfolio_usd=config.region_max_daily_exposure_usd(station_icao),
    )
```

`apply_portfolio_budget`'s own `max_portfolio_usd=None` default stays as it is — it still falls back to the global constant for the unit tests in `tests/test_portfolio_caps.py` that call it directly.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_region_isolation.py -k "RegionScopedCapital or RegionScopedPortfolio" -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Run the budget regression tests**

Run: `python -m pytest tests/test_portfolio_caps.py tests/test_risk_budget_fixes.py tests/test_parity_entry.py tests/test_gap_risk_sizing.py -q`
Expected: PASS. These pin the existing sizing and cap arithmetic; WSSS resolves to `region="asia"` whose pool equals the old constants, so every number must be identical.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS, same count as before.

- [ ] **Step 8: Commit**

```bash
# SCOPED ADD -- this repo has had concurrent uncommitted work in both of
# these files. Stage hunks, never the whole file, and confirm with
# `git status` + `git diff --cached` that only this task's changes are in.
git add -p config.py entry_manager.py
git add tests/test_region_isolation.py
git status
git diff --cached --stat
git commit -m "Scope Kelly bankroll and portfolio exposure per region"
```

---

### Task 5: Region-scoped live blast radius

**Files:**
- Modify: `config.py` — immediately after the `LIVE_MAX_ORDERS_PER_DAY` assignment
- Modify: `storage.py` — `count_live_order_attempts()`
- Modify: `executor.py` — `_live_budget_breach()` and its single call site in `_open_via_order_path()`
- Modify: `tests/test_live_execution.py`, `tests/test_settled_token_wiring.py` (monkeypatch arity)
- Test: `tests/test_region_isolation.py`

**Interfaces:**
- Consumes: `config.region_of()` (Task 4).
- Produces:
  - `config.REGION_LIVE_MAX_CONCURRENT_POSITIONS: dict[str, int]`
  - `config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD: dict[str, float]`
  - `config.REGION_LIVE_MAX_ORDERS_PER_DAY: dict[str, int]`
  - `storage.count_live_order_attempts(kind: str, since_iso: str, station_icaos: Optional[list] = None) -> Optional[int]`
  - `executor._live_budget_breach(size_usd: float, station_icao: str) -> Optional[str]` — **the second parameter is new and required.**

- [ ] **Step 1: Write the failing test**

Append to `tests/test_region_isolation.py`:

```python
import executor
import storage
from models import Position


class TestRegionScopedLiveBlastRadius:
    def test_asia_values_equal_the_pre_existing_flat_constants(self):
        assert (config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["asia"]
                == config.LIVE_MAX_CONCURRENT_POSITIONS)
        assert (config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD["asia"]
                == config.LIVE_MAX_TOTAL_EXPOSURE_USD)
        assert (config.REGION_LIVE_MAX_ORDERS_PER_DAY["asia"]
                == config.LIVE_MAX_ORDERS_PER_DAY)

    def test_europe_is_locked_at_zero(self):
        assert config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["europe"] == 0
        assert config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD["europe"] == 0.0
        assert config.REGION_LIVE_MAX_ORDERS_PER_DAY["europe"] == 0

    def test_a_europe_station_is_refused_even_on_an_empty_live_book(
        self, monkeypatch
    ):
        """
        THE POINT OF THIS TASK. Zero open positions, zero orders today, and
        the entry is still refused -- because the region's concurrent cap
        is 0. Kelly-side isolation (Task 4) does nothing here: live orders
        never pass through Kelly sizing at all.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
        monkeypatch.setattr(storage, "count_live_order_attempts",
                            lambda kind, since, station_icaos=None: 0)
        monkeypatch.setattr(
            executor.wallet_client, "reconcile_cached",
            lambda positions, **_: executor.wallet_client.Reconciliation(
                ok=True, checked=True, reason="stubbed"),
        )

        breach = executor._live_budget_breach(1.00, "EUTEST")

        assert breach is not None
        assert "europe" in breach

    def test_asia_positions_do_not_consume_a_europe_budget(self, monkeypatch):
        """
        Once Europe IS funded, the two regions count independently. Three
        open Asia positions fill Asia's cap of 3 and leave Europe's own
        budget untouched.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})
        monkeypatch.setitem(config.REGION_LIVE_MAX_CONCURRENT_POSITIONS, "europe", 3)
        monkeypatch.setitem(config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD, "europe", 8.00)
        monkeypatch.setitem(config.REGION_LIVE_MAX_ORDERS_PER_DAY, "europe", 10)

        asia_positions = [
            Position(
                position_id=f"p{i}", station_icao="WSSS", target_date=date(2026, 8, 24),
                bucket_c=32, side="YES", entry_price=0.30, size_usd=3.75,
                entry_time="2026-08-24T00:00:00+00:00", status="open",
                token_id=f"TOK{i}", is_paper=False, size_shares=5.0,
                execution_mode="live",
            )
            for i in range(3)
        ]
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: asia_positions)
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
        monkeypatch.setattr(storage, "count_live_order_attempts",
                            lambda kind, since, station_icaos=None: 0)
        monkeypatch.setattr(
            executor.wallet_client, "reconcile_cached",
            lambda positions, **_: executor.wallet_client.Reconciliation(
                ok=True, checked=True, reason="stubbed"),
        )

        # Asia is full at 3 concurrent...
        assert executor._live_budget_breach(1.00, "WSSS") is not None
        # ...and Europe, now funded, is unaffected by them.
        assert executor._live_budget_breach(1.00, "EUTEST") is None

    def test_the_order_rate_limit_counts_only_the_region(self, monkeypatch):
        """
        The daily order cap is counted from an audit table keyed by
        station_icao. It must be filtered to the region too, or Asia's ten
        orders would exhaust Europe's separate allowance.
        """
        seen = {}

        def fake_count(kind, since, station_icaos=None):
            seen["station_icaos"] = station_icaos
            return 0

        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
        monkeypatch.setattr(storage, "count_live_order_attempts", fake_count)
        monkeypatch.setattr(
            executor.wallet_client, "reconcile_cached",
            lambda positions, **_: executor.wallet_client.Reconciliation(
                ok=True, checked=True, reason="stubbed"),
        )

        executor._live_budget_breach(1.00, "WSSS")

        assert seen["station_icaos"] is not None
        assert "WSSS" in seen["station_icaos"]
        assert all(config.region_of(i) == "asia" for i in seen["station_icaos"])

    def test_reconciliation_sees_the_WHOLE_live_book_not_just_this_region(
        self, monkeypatch
    ):
        """
        THE ORDERING CONSTRAINT, PINNED.

        reconcile_cached() compares the database's ENTIRE live book against
        the exchange's actual holdings. The region filter must therefore run
        AFTER it: filtering first would hand reconciliation one region's
        positions, so every OTHER region's real holdings would read as
        unrecorded exposure and every entry would be refused.

        This is inert while Europe is locked at 0 -- no live European
        position can exist, so the whole book and the Asia book are the same
        set. It stops being inert the day a second region is funded, and a
        refactor that moves the filter one block up would pass every other
        test in the suite. Hence a test that asserts on what reconciliation
        was actually HANDED, rather than on the breach result.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        def _pos(position_id, station_icao):
            return Position(
                position_id=position_id, station_icao=station_icao,
                target_date=date(2026, 8, 24), bucket_c=32, side="YES",
                entry_price=0.30, size_usd=1.00,
                entry_time="2026-08-24T00:00:00+00:00", status="open",
                token_id=f"TOK-{position_id}", is_paper=False,
                size_shares=5.0, execution_mode="live",
            )

        book = [_pos("asia1", "WSSS"), _pos("eu1", "EUTEST")]
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: book)
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
        monkeypatch.setattr(storage, "count_live_order_attempts",
                            lambda kind, since, station_icaos=None: 0)

        seen = {}

        def _capturing_reconcile(positions, **_):
            # RECORD what reconciliation was handed -- the whole point.
            seen["positions"] = list(positions)
            return executor.wallet_client.Reconciliation(
                ok=True, checked=True, reason="stubbed")

        monkeypatch.setattr(executor.wallet_client, "reconcile_cached",
                            _capturing_reconcile)

        executor._live_budget_breach(1.00, "WSSS")

        handed = {p.station_icao for p in seen["positions"]}
        assert handed == {"WSSS", "EUTEST"}, (
            f"reconciliation was handed {handed} -- it must see the WHOLE live "
            f"book. If this fails, the region filter has been moved above the "
            f"reconcile_cached() call."
        )



class TestCountLiveOrderAttemptsFilter:
    def test_the_station_filter_narrows_the_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))

        storage.record_live_order_attempt(
            kind="entry", station_icao="WSSS", outcome="filled", notional_usd=1.0)
        storage.record_live_order_attempt(
            kind="entry", station_icao="RCSS", outcome="filled", notional_usd=1.0)

        assert storage.count_live_order_attempts("entry", "2000-01-01") == 2
        assert storage.count_live_order_attempts(
            "entry", "2000-01-01", station_icaos=["WSSS"]) == 1
        assert storage.count_live_order_attempts(
            "entry", "2000-01-01", station_icaos=["WSSS", "RCSS"]) == 2

    def test_an_empty_station_list_counts_nothing(self, tmp_path, monkeypatch):
        """
        A region with no registered stations has no orders, and must read 0
        rather than degrading to the unfiltered total.
        """
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
        storage.record_live_order_attempt(
            kind="entry", station_icao="WSSS", outcome="filled", notional_usd=1.0)

        assert storage.count_live_order_attempts(
            "entry", "2000-01-01", station_icaos=[]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_region_isolation.py -k "LiveBlastRadius or CountLiveOrder" -v`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'REGION_LIVE_MAX_CONCURRENT_POSITIONS'`.

- [ ] **Step 3: Write the config half**

In `config.py`, immediately after `LIVE_MAX_ORDERS_PER_DAY = 10`:

```python
# --- Per-region LIVE blast radius -----------------------------------------
# A SEPARATE MECHANISM from REGION_BANKROLL_USD, and the distinction is the
# whole reason this block exists. Live orders never pass through Kelly
# sizing at all -- LIVE_TRADE_SIZE_USD replaces it outright -- so scoping
# the Kelly bankroll by region does precisely nothing to the real-money
# path. The three caps above are the real-money path, and they were
# documented as "across all live stations": process-global.
#
# Left that way, isolation would hold only until the first European station
# were ever promoted, at which point it would silently begin competing with
# WSSS and RCSS for the same slots and the same dollar ceiling -- discovered
# under promotion pressure, which is the worst moment to discover it.
#
# EUROPE IS 0/0.0/0. Not "small": zero. A European station cannot submit a
# live order regardless of LIVE_TRADING_STATIONS membership or the
# POLYMARKET_LIVE_TRADING process flag, because its region authorises no
# concurrent positions at all.
#
# RE-DERIVE, DO NOT COPY, IF EUROPE IS EVER FUNDED.
# Asia's pair is not two independently chosen numbers: they encode an
# intended BINDING ORDER -- dollars bind first, count is a sanity bound --
# and test_live_execution.py::test_the_dollar_cap_binds_before_the_count_cap
# asserts that relationship against ASSUMED_EXCHANGE_MIN_SHARES,
# MAX_ENTRY_PRICE and LIVE_TRADE_SIZE_USD. Europe's worst case will differ
# (different bucket economics, possibly a different MAX_ENTRY_PRICE
# regime), so if it is ever funded its ceiling must be re-derived to
# satisfy that same relationship -- not copied from Asia's number.
REGION_LIVE_MAX_CONCURRENT_POSITIONS = {
    "asia": LIVE_MAX_CONCURRENT_POSITIONS,
    "europe": 0,
}

REGION_LIVE_MAX_TOTAL_EXPOSURE_USD = {
    "asia": LIVE_MAX_TOTAL_EXPOSURE_USD,
    "europe": 0.0,
}

REGION_LIVE_MAX_ORDERS_PER_DAY = {
    "asia": LIVE_MAX_ORDERS_PER_DAY,
    "europe": 0,
}


def stations_in_region(region: str) -> list:
    """Every registered ICAO drawing on one region's pools."""
    return [icao for icao, st in STATIONS.items() if st.region == region]
```

**Do NOT parameterize `test_the_dollar_cap_binds_before_the_count_cap` over
all regions in this task.** Its first assertion is a strict `<`, so for a
zero-funded region it reads `0.0 < 0 * worst_case` — that is `0.0 < 0.0`,
which is false. A naively region-looped version of that test fails on Europe
by construction. The invariant is only meaningful for a funded region: leave
the test asserting the flat Asia constants, and let whatever change first
funds a second region write the region-aware version.

- [ ] **Step 4: Write the storage half**

In `storage.py`, change `count_live_order_attempts`:

```python
def count_live_order_attempts(
    kind: str,
    since_iso: str,
    station_icaos: Optional[list] = None,
) -> Optional[int]:
    """
    How many live orders of one kind were SUBMITTED at or after `since_iso`.

    `station_icaos` narrows the count to a set of stations -- how the
    per-region daily order cap is enforced. None means every station, the
    original meaning, kept for callers that predate regions. An EMPTY list
    means no stations and correctly counts 0; it must not be conflated with
    None, or a region with no registered stations would read the global
    total.

    Returns None if the count could not be read. None is not zero: callers
    gating on this must treat an unreadable count as "cannot authorise",
    the same way the reconciliation check does -- a rate limit that fails
    open is not a rate limit.
    """
    try:
        with _db() as conn:
            if station_icaos is None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM live_order_attempts WHERE kind = ? AND ts >= ?",
                    (kind, since_iso),
                ).fetchone()
            elif not station_icaos:
                return 0
            else:
                placeholders = ",".join("?" for _ in station_icaos)
                row = conn.execute(
                    f"SELECT COUNT(*) FROM live_order_attempts "
                    f"WHERE kind = ? AND ts >= ? AND station_icao IN ({placeholders})",
                    (kind, since_iso, *station_icaos),
                ).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        print(f"[storage] could not count live order attempts: {exc}")
        return None
```

- [ ] **Step 5: Write the executor half**

In `executor.py`, change the signature:

```python
def _live_budget_breach(size_usd: float, station_icao: str) -> Optional[str]:
```

Add to its docstring, after the existing first paragraph:

```
    EVERY CAP BELOW IS SCOPED TO THIS STATION'S REGION. The caps are a
    blast radius, and a blast radius is per-cohort: a European station's
    live entries must neither consume nor be blocked by the Asian book's
    slots. See config.REGION_LIVE_MAX_* for why this is a separate
    mechanism from the Kelly-side region pools.
```

Add the region filter **after the `if not recon.ok: return ...` block and before the first cap check** — that placement is load-bearing:

```python
    # AFTER reconciliation, DELIBERATELY. reconcile_cached() compares the
    # database's ENTIRE live book against the exchange's actual holdings;
    # handing it one region's positions would report every other region's
    # real holdings as unrecorded exposure and fail every entry. The caps
    # below are per-region; the reconciliation that licenses trusting them
    # is not, and cannot be.
    region = config.region_of(station_icao)
    live_positions = [
        p for p in live_positions
        if p.station_icao in config.STATIONS
        and config.region_of(p.station_icao) == region
    ]
```

Note the `p.station_icao in config.STATIONS` guard: an orphaned position from a de-registered station would otherwise raise `KeyError` inside a function whose entire job is to fail safely. It is excluded from the region's count, which is the safe direction only because the reconciliation check above already refused to proceed if the exchange and database disagree.

Replace the three cap checks:

```python
    max_concurrent = config.REGION_LIVE_MAX_CONCURRENT_POSITIONS[region]
    if len(live_positions) >= max_concurrent:
        return (
            f"{len(live_positions)} live position(s) already open in region "
            f"{region!r}, at its REGION_LIVE_MAX_CONCURRENT_POSITIONS limit of "
            f"{max_concurrent}"
        )

    max_exposure = config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD[region]
    exposure = sum(p.size_usd for p in live_positions)
    if exposure + size_usd > max_exposure:
        return (
            f"${exposure:.2f} live exposure in region {region!r} + ${size_usd:.2f} "
            f"would exceed its REGION_LIVE_MAX_TOTAL_EXPOSURE_USD ceiling of "
            f"${max_exposure:.2f}"
        )
```

And the rate limit:

```python
    today = datetime.now(timezone.utc).date().isoformat()
    submitted = storage.count_live_order_attempts(
        "entry", today, station_icaos=config.stations_in_region(region),
    )
    if submitted is None:
        return (
            "could not read today's live order count -- refusing to authorise on an "
            "unenforceable rate limit (a cap that fails open is not a cap)"
        )
    max_orders = config.REGION_LIVE_MAX_ORDERS_PER_DAY[region]
    if submitted >= max_orders:
        return (
            f"{submitted} live order(s) already SUBMITTED today (filled or not) in "
            f"region {region!r}, at its REGION_LIVE_MAX_ORDERS_PER_DAY limit of "
            f"{max_orders}"
        )
    return None
```

At its call site in `_open_via_order_path()`:

```python
        breach = _live_budget_breach(spec.notional_usd, decision.station_icao)
```

- [ ] **Step 6: Update the existing tests whose monkeypatch arity changed**

Four call sites stub `count_live_order_attempts` with a two-argument lambda. The executor now passes `station_icaos=`, so each raises `TypeError` until updated. Add the keyword parameter to each:

- in `tests/test_live_execution.py`, the `captured` fixture → `lambda kind, since, station_icaos=None: 0`
- in `tests/test_live_execution.py`, `test_an_unreadable_count_fails_closed` → `lambda kind, since, station_icaos=None: None`
- both `count_live_order_attempts` stubs in `tests/test_settled_token_wiring.py` → add `station_icaos=None` to each lambda's parameter list, keeping its existing return value.

Every direct call to `_live_budget_breach` now needs the station. There are **seven across two files** — do not stop at the first file:

- `tests/test_live_execution.py` — five calls, all in the live-budget section: `executor._live_budget_breach(1.95)` → `executor._live_budget_breach(1.95, "WSSS")`.
- `tests/test_settled_token_wiring.py` — **two more**, inside the settled-token reconciliation tests: `executor._live_budget_breach(1.00)` → `executor._live_budget_breach(1.00, "WSSS")`. These are easy to miss because that file's tests are about reconciliation, not budgets, but they call the function directly and will raise `TypeError` without the new argument.

Find them all before editing:

```bash
grep -rn "_live_budget_breach\|count_live_order_attempts" tests/
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_region_isolation.py -k "LiveBlastRadius or CountLiveOrder" -v`
Expected: PASS (7 tests)

- [ ] **Step 8: Run the live-path regression tests**

Run: `python -m pytest tests/test_live_execution.py tests/test_settled_token_wiring.py tests/test_settled_token_reconciliation.py tests/test_reconciliation_units.py -q`
Expected: PASS. WSSS and RCSS are both `region="asia"`, whose caps equal the old constants, so every existing accept/reject decision must be identical.

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS, same count as before.

- [ ] **Step 10: Commit**

```bash
# SCOPED ADD -- see Task 4's note. config.py especially.
git add -p config.py storage.py executor.py
git add tests/test_region_isolation.py tests/test_live_execution.py tests/test_settled_token_wiring.py
git status
git diff --cached --stat
git commit -m "Scope the live blast radius per region"
```

---

### Task 6: Region-scope the pooled forecast-error spread

**Files:**
- Modify: `calibration.py` (`pooled_error_spread`, and its `_pooled_spread_cache` key)
- Test: `tests/test_region_isolation.py`

**Interfaces:**
- Consumes: `config.region_of()` (Task 4).
- Produces: `calibration.pooled_error_spread(region: Optional[str] = None) -> tuple`. `None` keeps the all-stations pool for callers that predate regions.

**Why this task exists — it is NOT in the original spec.** `pooled_error_spread()` pools forecast errors across **every registered station** and is the spread tier a station falls back to when it has too little history of its own (`config.MIN_SPREAD_PAIRS = 5`). Every newly registered European station starts on exactly that tier. Two consequences, and the second is the serious one:

1. European stations would derive their spread from Asian tropical forecast errors — wrong, but only wrong for Europe.
2. **Once European errors accumulate, they are pooled into the number Asian stations fall back on.** Spread feeds `probability.py`, which feeds EV, which feeds entry decisions. Registering Europe would therefore silently change Asian trading behavior — a direct violation of this plan's own Global Constraint that Asia behavior must not change, and of the spec's "full isolation" premise.

The pooled spread is a shared *statistical estimator*, which is a third kind of coupling — not capital (Task 4), not blast radius (Task 5). Temperate and tropical stations have genuinely different error distributions; pooling them produces a number that describes neither.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_region_isolation.py`:

```python
import calibration


class TestPooledSpreadIsRegionScoped:
    def test_the_pool_excludes_other_regions(self, monkeypatch, tmp_path):
        """
        A European station's errors must not enter the pool an Asian
        station falls back on. Spread feeds EV feeds entries, so this is
        the path by which registering Europe could silently move Asian
        trading behavior.
        """
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
        calibration._pooled_spread_cache.clear()

        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        # Asia errors are tight; the European station's are wild. If the
        # European rows leak into Asia's pool, Asia's spread inflates.
        def fake_samples(icao, source):
            if icao == "EUTEST":
                return [-8.0, 8.0, -8.0, 8.0, -8.0, 8.0]
            if icao == "WSSS":
                return [-0.5, 0.5, -0.5, 0.5, -0.5, 0.5]
            return []

        monkeypatch.setattr(storage, "forecast_error_samples", fake_samples)

        asia_spread, asia_n = calibration.pooled_error_spread(region="asia")
        calibration._pooled_spread_cache.clear()
        eu_spread, eu_n = calibration.pooled_error_spread(region="europe")

        assert asia_n == 6, "asia pool must contain only WSSS's six samples"
        assert eu_n == 6, "europe pool must contain only EUTEST's six samples"
        assert asia_spread < 1.0, f"asia spread {asia_spread} inflated by European errors"
        assert eu_spread > 5.0, f"europe spread {eu_spread} diluted by Asian errors"

    def test_the_cache_key_separates_regions(self, monkeypatch, tmp_path):
        """
        The cache is keyed on DB_PATH. Without the region in the key, the
        first region to compute would serve its spread to the other -- the
        leak this task exists to close, reintroduced by the cache.
        """
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
        calibration._pooled_spread_cache.clear()

        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        def fake_samples(icao, source):
            if icao == "EUTEST":
                return [-8.0, 8.0, -8.0, 8.0, -8.0, 8.0]
            if icao == "WSSS":
                return [-0.5, 0.5, -0.5, 0.5, -0.5, 0.5]
            return []

        monkeypatch.setattr(storage, "forecast_error_samples", fake_samples)

        # No clear() between these two calls -- the cache must not confuse them.
        asia_spread, _ = calibration.pooled_error_spread(region="asia")
        eu_spread, _ = calibration.pooled_error_spread(region="europe")

        assert asia_spread != eu_spread

    def test_no_region_still_pools_everything(self, monkeypatch, tmp_path):
        """Back-compat for callers that predate regions."""
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
        calibration._pooled_spread_cache.clear()

        monkeypatch.setattr(storage, "forecast_error_samples",
                            lambda icao, source: [-1.0, 1.0])

        _, n = calibration.pooled_error_spread()

        assert n == 2 * len(config.STATIONS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_region_isolation.py -k PooledSpread -v`
Expected: FAIL — `TypeError: pooled_error_spread() got an unexpected keyword argument 'region'`.

- [ ] **Step 3: Write the implementation**

In `calibration.py`, change `pooled_error_spread`:

```python
def pooled_error_spread(region: Optional[str] = None) -> tuple:
```

Extend its docstring with:

```
    `region` scopes the pool to one cohort of stations. This is not a
    refinement, it is a correctness requirement once the registry spans
    more than one climate: temperate and tropical stations have genuinely
    different error distributions, and a pool spanning both describes
    neither. It is also the path by which a newly registered region would
    otherwise change an existing region's trading behaviour without
    touching a single line of that region's code -- spread feeds
    probability feeds EV feeds entries.

    None pools every registered station, the original meaning, kept for
    callers that predate regions.
```

Add the region to the cache key and the station filter:

```python
    now = time.time()
    # The region is part of the key, not just the db path. Without it the
    # first region to compute would serve its spread to every other one --
    # reintroducing the exact leak the filter below removes.
    key = (str(config.DB_PATH), region)
    hit = _pooled_spread_cache.get(key)
    if hit is not None and now - hit[2] < config.POOLED_SPREAD_CACHE_TTL_S:
        return hit[0], hit[1]

    centred = []
    for icao in config.STATIONS:
        if region is not None and config.region_of(icao) != region:
            continue
        try:
```

Leave the rest of the function body unchanged. `calibration.py` currently imports only `from typing import List` — add `Optional` to that import; it is not there today.

**Do NOT add a module-level `import storage` to `calibration.py`.** That module deliberately imports storage inside the two functions that need it, with the written rationale "local: keeps calibration importable without a db". The tests above patch `storage.forecast_error_samples` on the storage module directly, which reaches the function-local import just fine — both bind the same module object, and attribute lookup happens at call time. `tests/test_region_isolation.py` needs its own top-level `import storage` for that.

- [ ] **Step 4: Find and update the callers**

```bash
grep -rn "pooled_error_spread" --include=*.py .
```

There is exactly ONE production caller today: the `pooled, _ = pooled_error_spread()` line in `calibration.estimate_std_dev()`, in its third spread tier. `station_icao` is already a parameter of that function and is in scope there. Change it to:

```python
    pooled, _ = pooled_error_spread(
        region=config.region_of(station_icao) if station_icao else None,
    )
```

`station_icao` is genuinely optional in that function, and `None` correctly means "a station-agnostic caller wants the whole pool". The other three call sites are in `tests/test_spread_estimator.py` and stay parameterless on purpose — they test the unfiltered pool. Check `backtest/` for a mirrored copy and update it the same way if one exists; the backtest and live paths are parity-tested.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_region_isolation.py -k PooledSpread -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run the calibration and parity regression tests**

Run: `python -m pytest tests/test_spread_estimator.py tests/test_calibration_inputs.py tests/test_parity_entry.py tests/test_forecast_bias.py tests/test_determinism.py -q`
Expected: PASS. With only Asian stations registered, `region="asia"` pools exactly the same stations the unfiltered call did, so every existing number must be unchanged. Watch `test_pooled_cache_is_keyed_on_the_database` in particular: it asserts the cache does not leak across databases, and this task changes that cache's key from a plain string to a `(db_path, region)` tuple.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS, same count as before.

- [ ] **Step 8: Commit**

```bash
# SCOPED ADD -- see Task 4's note.
git add -p calibration.py
git add tests/test_region_isolation.py
git status
git diff --cached --stat
git commit -m "Scope the pooled forecast-error spread per region"
```

---

### Task 7: Confirm the per-station facts (research, no code)

**Files:**
- Create: `docs/superpowers/research/2026-08-24-europe-station-facts.md`

**Interfaces:**
- Consumes: nothing in code.
- Produces: the confirmed values Task 7 writes into `config.STATIONS`. Task 7 must not invent any of them.

**Why this is its own task:** the Asia expansion's two most expensive surprises — Hong Kong settling on the HK Observatory rather than the airport METAR, and Karachi's unresolved station identity — were both found by exactly this check, and both would have silently corrupted the observation blend if assumed. The European markets already look different from Asia's in one visible respect: their resolution text names **NOAA** station records rather than Wunderground's. Do not assume the Asian defaults transfer.

- [ ] **Step 1: Pull each event's full record from the Gamma API**

For each of the 7 cities, fetch and read the whole event payload — not just the title:

```bash
curl -s "https://gamma-api.polymarket.com/events?slug=highest-temperature-in-london-on-august-25-2026" | python -m json.tool > /tmp/london.json
```

Repeat with slugs `highest-temperature-in-{paris,madrid,amsterdam,milan,munich,warsaw}-on-august-25-2026`. Use tomorrow's date if today's event has already resolved. The slug pattern is the one `market_discovery.build_event_slug()` builds.

- [ ] **Step 2: Record, per city, the facts the registry entry needs**

For each city, extract and write down:

| Field | Where it comes from |
|---|---|
| `polymarket_city_slug` | the slug fragment that resolved, e.g. `london` |
| settlement station | the event `description` field — it names the exact weather station and source (e.g. "NOAA at the London City Airport Station") |
| `icao` | the ICAO of THAT named station, not the city's busiest airport. London City is EGLC, not Heathrow EGLL. |
| `lat` / `lon` | the named station's coordinates |
| live bucket window | the `min` and `max` degree labels across the event's markets |
| bucket count | how many markets the event lists |

- [ ] **Step 3: Resolve the remaining registry fields per city**

- `wunderground_slug`: confirm a Wunderground history page exists for that ICAO (`https://www.wunderground.com/history/daily/<slug>`). If the market settles on NOAA rather than Wunderground, record that explicitly — it decides `resolution_grade_source` and `metar_ingest_mode`, exactly as it did for VHHH.
- `official_client_key`: check whether the city appears in the WMO WWIS city list (`https://worldweather.wmo.int/en/json/full_city_list.txt`) and record the exact `wwis_city_name` string. All 7 are expected to be present (unlike Taipei), but confirm rather than assume — a wrong city name yields an honest `None` forecast, and the official source carries 40% of the calibration blend.
- `long_term_normal_max_c`: an August climatological daily-max normal for that station. Mark it a placeholder if unverified, the way the Asia entries are marked.
- `iana_timezone` and `utc_offset_hours` (standard-time value):

| City | `iana_timezone` | `utc_offset_hours` (standard) |
|---|---|---|
| London | `Europe/London` | 0 |
| Paris | `Europe/Paris` | 1 |
| Madrid | `Europe/Madrid` | 1 |
| Amsterdam | `Europe/Amsterdam` | 1 |
| Milan | `Europe/Rome` | 1 |
| Munich | `Europe/Berlin` | 1 |
| Warsaw | `Europe/Warsaw` | 1 |

- [ ] **Step 4: Flag any city that cannot satisfy a registry invariant**

`tests/test_station_registry.py` enforces invariants that a European market might genuinely fail. Check each city against them and record the answer:

- **Bucket span must be exactly `config.EXPECTED_BUCKET_COUNT` (11).** If a city's event lists a different number of buckets, it CANNOT be registered without changing a constant the discovery and backtest paths both depend on. Report it; do not register that city, and do not change `EXPECTED_BUCKET_COUNT` as a workaround.
- `polymarket_city_slug` and `wunderground_slug` must be unique across the whole registry.
- `official_client_key` must already exist in `clients/official/registry._CLIENTS` (currently `nea`, `met_malaysia`, `wwis`, `hko`). A city needing a new national adapter is a separate piece of work — record it and leave that city out of Task 7.

- [ ] **Step 5: Write the findings document**

Create `docs/superpowers/research/2026-08-24-europe-station-facts.md` with one section per city containing every field above, each marked **confirmed** (with the URL it came from) or **placeholder**. End with an explicit list of cities that are registrable now and cities that are blocked, with the reason.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/research/2026-08-24-europe-station-facts.md
git commit -m "Record confirmed per-station facts for the Europe registry"
```

---

### Task 8: Register the European stations

**Files:**
- Modify: `config.py` (`STATIONS`, `MATURITY_SNAPSHOT`)
- Modify: `tests/test_station_registry.py`, `tests/test_scheduler_groups.py`
- Test: `tests/test_region_isolation.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7. Every literal written here comes from Task 7's document.
- Produces: the registered European `StationConfig` entries.

**Do not start this task until Task 7's document exists.** If it lists blocked cities, register only the registrable ones and adjust the counts below to match.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_region_isolation.py`:

```python
class TestEuropeRegistry:
    def test_every_europe_station_sets_an_iana_timezone(self):
        """
        A DST region without a tz name is the exact bug this framework
        exists to prevent -- it would trade on a static offset that is
        wrong for half the year.
        """
        for icao, st in config.STATIONS.items():
            if st.region == "europe":
                assert st.iana_timezone, f"{icao}: region=europe requires an iana_timezone"

    def test_every_declared_timezone_actually_resolves(self):
        """
        A typo'd iana_timezone is a STATIC config error -- it needs no clock
        to detect, so it should be caught here at collection time and not at
        05:00 in the daemon.

        scheduler.stations_by_utc_offset() skips such a station and logs it
        loudly (Task 3), which keeps the other stations trading but means the
        typo'd one silently does not trade at all. This assertion is what
        stops that state from ever reaching a deployment.
        """
        for icao, st in config.STATIONS.items():
            if not st.iana_timezone:
                continue
            # Raises ZoneInfoNotFoundError if the name is not in the tz db.
            config.current_utc_offset_hours(st)

    def test_every_europe_station_also_keeps_a_static_offset(self):
        """
        backtest/engine.py reads station.utc_offset_hours directly and has
        no notion of a moving clock. The static field must stay set to the
        standard-time value.
        """
        for icao, st in config.STATIONS.items():
            if st.region == "europe":
                assert st.utc_offset_hours in (0, 1), (
                    f"{icao}: expected a European standard-time offset, "
                    f"got {st.utc_offset_hours}"
                )

    def test_no_europe_station_is_allowlisted_for_real_money(self):
        for icao in config.LIVE_TRADING_STATIONS:
            assert config.region_of(icao) == "asia", (
                f"{icao} is a non-Asian station on the real-money allowlist; "
                f"its region is funded at zero and this is a contradiction"
            )

    def test_europe_stations_are_present_and_all_exploratory(self):
        europe = config.stations_in_region("europe")
        assert europe, "no European station registered"
        for icao in europe:
            assert config.MATURITY_SNAPSHOT[icao] == "exploratory", icao

    def test_every_named_region_has_funding_entries(self):
        """
        Every region named by a station must have funding entries in all
        five dicts, or a lookup raises at trade time.
        """
        regions = {st.region for st in config.STATIONS.values()}
        for region in regions:
            assert region in config.REGION_BANKROLL_USD, region
            assert region in config.REGION_MAX_DAILY_EXPOSURE_USD, region
            assert region in config.REGION_LIVE_MAX_CONCURRENT_POSITIONS, region
            assert region in config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD, region
            assert region in config.REGION_LIVE_MAX_ORDERS_PER_DAY, region
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_region_isolation.py -k EuropeRegistry -v`
Expected: FAIL on `test_europe_stations_are_present_and_all_exploratory` — `AssertionError: no European station registered`.

- [ ] **Step 3: Add the station entries**

In `config.py`, after the last Asian entry (`OPKC`) and still inside `STATIONS`, add a section header and one entry per registrable city. Use the values from Task 7's document — the entry below is the SHAPE, with London's fields as an illustration; replace every value with the confirmed one:

```python
    # --- Europe ------------------------------------------------------------
    # Registered 2026-08-24. Confirmed facts in
    # docs/superpowers/research/2026-08-24-europe-station-facts.md.
    #
    # TWO THINGS EVERY ENTRY HERE MUST CARRY, neither of which any Asian
    # entry needs:
    #   region="europe"       -- draws on a pool funded at $0. Collection
    #                            and scoring only; it cannot size an order.
    #   iana_timezone=...     -- these cities observe DST. utc_offset_hours
    #                            is ALSO set, to the STANDARD-time value,
    #                            because backtest/engine.py reads it
    #                            directly and has no moving clock.
    "EGLC": StationConfig(
        icao="EGLC",
        display_name="London City Airport",
        country="United Kingdom",
        lat=51.5048,
        lon=0.0495,
        wunderground_slug="gb/london/EGLC",
        long_term_normal_max_c=23.5,  # PLACEHOLDER -- confirm before trusting
        official_client_key="wwis",
        wwis_city_name="London",
        polymarket_city_slug="london",
        region="europe",
        iana_timezone="Europe/London",
        utc_offset_hours=0,
        bucket_min_c=17,
        bucket_max_c=27,
    ),
```

Repeat for each remaining registrable city. `monsoon_phase_by_month` is deliberately omitted (defaults to `{}`) — the shared SE Asian monsoon lookup is meaningless in Europe and feeds no calculation, matching how the 11 new Asian stations were registered.

- [ ] **Step 4: Add the maturity snapshot entries**

In `config.py`'s `MATURITY_SNAPSHOT` dict, add one `"exploratory"` line per newly registered ICAO, after `"OPKC"`:

```python
    "EGLC": "exploratory",
```

`STATION_MATURITY` needs no edit — it is a derived `_MaturityMapping()`, not a literal.

- [ ] **Step 5: Update the registry tests that assert the old shape**

In `tests/test_station_registry.py`:

- `EXPECTED_STATION_COUNT = 13` → the new total (20 if all 7 register).
- `test_utc_offset_hours_in_registered_timezones` asserts `in (5, 8, 9)`. Widen it and explain why:

```python
def test_utc_offset_hours_in_registered_timezones():
    # 5/8/9 are the Asian registry. 0/1 are European STANDARD-time offsets
    # (see StationConfig.iana_timezone -- the live path resolves DST via
    # config.current_utc_offset_hours(); this static field is what the
    # backtest reads).
    for icao, st in config.STATIONS.items():
        assert st.utc_offset_hours in (0, 1, 5, 8, 9), (
            f"{icao}: unexpected utc_offset_hours {st.utc_offset_hours}"
        )
```

- `test_default_bucket_edge_mode_and_resolution_source_elsewhere` asserts every non-VHHH station uses `metar_daily_max`. If Task 7 found that the European markets settle on a different source, this test's exemption list must grow to match, with the reason written in. If they do settle on the METAR record, it passes unchanged.
- `test_wwis_stations_have_city_name_except_taipei` will cover the new stations automatically once `wwis_city_name` is set.

In `tests/test_scheduler_groups.py`, `test_stations_group_by_utc_offset_matching_the_registry` asserts `set(groups) == {5, 8, 9}` and builds its expectation from `st.utc_offset_hours`. Both break for a DST station in summer. Rewrite it to build from the resolved offset:

```python
def test_stations_group_by_utc_offset_matching_the_registry():
    groups = scheduler.stations_by_utc_offset()

    expected = {}
    for icao, st in config.STATIONS.items():
        # The RESOLVED offset, not the static field: a European station's
        # group follows its current DST state (config.current_utc_offset_hours).
        expected.setdefault(config.current_utc_offset_hours(icao), []).append(icao)

    assert {k: sorted(v) for k, v in groups.items()} == {
        k: sorted(v) for k, v in expected.items()
    }
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_region_isolation.py tests/test_station_registry.py tests/test_scheduler_groups.py -v`
Expected: PASS

- [ ] **Step 7: Verify the registry against the live API**

Run a one-off check that every registered European station's slug actually resolves and its bucket window matches what was recorded:

```bash
python -c "
import config, market_discovery
from datetime import timedelta
for icao in config.stations_in_region('europe'):
    st = config.get_station(icao)
    d = config.local_today(st)
    ev = market_discovery.fetch_event(market_discovery.build_event_slug(st, d))
    print(icao, st.polymarket_city_slug, 'OK' if ev else 'NO EVENT',
          len(ev.get('markets', [])) if ev else 0)
"
```

Expected: every station prints `OK` with a market count of 11. A `NO EVENT` means the slug is wrong or that city has no market today; a count other than 11 contradicts `EXPECTED_BUCKET_COUNT` and must be resolved before that station stays registered.

- [ ] **Step 8: Verify the isolation actually holds end to end**

```bash
python -c "
import config, entry_manager
for icao in config.stations_in_region('europe'):
    assert config.region_bankroll_usd(icao) == 0.0, icao
    assert config.REGION_LIVE_MAX_CONCURRENT_POSITIONS[config.region_of(icao)] == 0, icao
    assert icao not in config.LIVE_TRADING_STATIONS, icao
print('europe isolated:', config.stations_in_region('europe'))
print('asia untouched:', len(config.stations_in_region('asia')), 'stations')
"
```

Expected: prints the European station list, then `asia untouched: 13 stations`.

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS. Report the test count against the pre-Task-1 baseline; the only increase should be the new tests this plan added.

- [ ] **Step 10: Commit**

```bash
# SCOPED ADD -- see Task 4's note.
git add -p config.py
git add tests/test_region_isolation.py tests/test_station_registry.py tests/test_scheduler_groups.py
git status
git diff --cached --stat
git commit -m "Register European stations in the isolated europe region"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| 1. `StationConfig` additions | Task 1 |
| 2. DST-aware offset | Task 2 |
| 3. Region-scoped capital | Task 4 |
| 4. Region-scoped live blast radius | Task 5 |
| 5. Promotion gate (no new code) | Task 8, Step 1 — asserted, not built |
| 6. Scheduling isolation + known limitation | Task 3 (limitation documented in Step 5) |
| 7. Station registry entries + open research item | Tasks 7 and 8 |
| Testing section | Every task's own steps; registry invariants in Task 8 |
| *(not in spec)* pooled-spread isolation | Task 6 — see below |

**Known deviations from the spec, deliberate:**

- **Task 6 has no spec section.** `calibration.pooled_error_spread()` pools
  forecast errors across every registered station and is the spread tier a
  new station falls back on. It is a third coupling the spec did not
  identify — neither capital nor blast radius, but a shared statistical
  estimator — and it is the one that would let registering Europe change
  Asian trading behaviour, since spread feeds probability feeds EV feeds
  entries. The spec has been amended to match; if the amended spec is not
  present, Task 6 is the authority for this gap.
- The spec's section 3 named the `bankroll_sized_usd` line and the `portfolio_day_exposure_usd` caller. Task 4 also passes `max_portfolio_usd` explicitly at the `apply_portfolio_budget` call site — without it the region's cap would be computed and then ignored in favor of the global default.
- Task 2 adds `config._now_utc()`, which the spec did not name. It is required: the DST behavior cannot be tested against a real clock that is only ever in one DST state.
- Task 5 adds `config.stations_in_region()`, which the spec did not name. The per-region order-rate filter needs a station list.
