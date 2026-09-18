# Wave 2: Correct the Inputs, One Day — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** DRAFT 2026-09-18 — not started. Target merge + deploy ~2026-09-21.

**Goal:** Correct, on one day, the five statistics the entry path is computed from — the forecast-error sample's fetch window (2a), the unconditional spread floor on measured tiers (2b), the unsigned admission-edge compare (2c), the cached calibration failure (2d), the total-forecast-outage hole in the mix guard (2e) — bring the replay's haircut and exit-fee arithmetic level with live (2f), stamp the deploy date in `config.REGIME_BOUNDARIES` so every cohort report splits at it, and ship the read-only script that answers the pre-registered reads and the stop condition.
**Architecture:** Every behaviour change (2a–2e) is one on/off constant in a single `WAVE 2` block at the end of `config.py`, defaulting on, consumed at exactly one site each: `config.error_sample_fetch_bounds_utc` narrows the window `storage.forecast_rows_in_error_sample` (new pure function, the single implementation behind bias, RMSE, spread and source-mix) admits rows through; `calibration._clamp_spread(measured=True)` floors measured tiers at `MEASURED_SPREAD_MIN_C` instead of `SPREAD_FLOOR_C`; `entry_manager.edge_misses_bar` (shared with `backtest/entry_sim.py`) makes veto 0a2 a signed compare; `probability_calibration.calibration_for` returns a failed fit without caching it; `entry_manager.today_source_mix_for` passes an empty source list through as `frozenset()` and the mix guard tests `is not None` instead of truthiness. 2f threads the replay book's mode (`"paper"`) through `entry_sim.evaluate_entry_sim` into the same `_book_has_stop` helper live uses and adds the exit-fee term to `net_ev_at_size`. A new `regimes.py` splits any row list at the boundaries; `cohort_monitor`, `calibration_panel` and `promotion_dossier` print per-segment totals by default with `--no-regime-split` / `regime_split=False` for the pooled number. `wave2_falsifier.py` opens the DB `mode=ro` and composes the pure storage/calibration/cohort helpers over rows it fetched itself.
**Tech Stack:** Python 3.12, sqlite3, pytest; no numpy/scipy (not on the box)
**Spec:** docs/superpowers/specs/2026-09-17-evidence-first-remediation-design.md (section "Wave 2 — correct the inputs, one day")

## Global Constraints
- Every behaviour change (2a, 2b, 2c, 2d, 2e) sits behind its own on/off constant in the `WAVE 2` block of `config.py`, defaulting to on; `False` restores the pre-Wave-2 rule exactly at that site (spec: "a revert is a config flip"). 2f is backtest-only and has no flag (spec table: "backtest only").
- The six items (2a–2f), `regimes.py` + the three report splits, and `wave2_falsifier.py` ship in ONE merge and ONE deploy, so the wave has exactly one date in the data (spec Principle 1 and 3).
- `config.REGIME_BOUNDARIES` is `()` on the branch throughout Tasks 1–6 and is stamped with the ACTUAL deploy date in the final commit before merge (Task 7). If the deploy slips past that date, a one-line follow-up commit moves it: the boundary must equal the first day the box ran the new code.
- No schema change in this wave, so the deploy is the plain `deploy_daemon.sh` path (no backup-and-stop). Never deploy during 05:00–08:00 SGT.
- `backtest/engine.py:988` passes `allow_measured_spread=False` unconditionally and is NOT changed: the replay is structurally blind to 2a and 2b (see Task 2). No task may claim a replay result about either.
- Nothing here touches live-money semantics: `HOLD_TO_SETTLEMENT_MODES`, redemption, `LIVE_TRADE_SIZE_USD` and the live executor are untouched (spec Principle 4).
- 2c, 2d and 2e can only REFUSE entries or retry a failed read; a test in each pins that the flag-off path reproduces today's decision.
- Run pytest from the `weather-forecast/` package dir; never on the EC2 box.
- Commit after every task; commit messages end with the line: Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
---

**Paths.** The git root is `C:\Users\user\Downloads\weather-forecast`; the Python package is the nested `weather-forecast/` directory. Every path below is relative to the package dir (`weather-forecast/storage.py` == `C:\Users\user\Downloads\weather-forecast\weather-forecast\storage.py`). Every `pytest` and `git` command runs from the package dir. Line numbers are as of HEAD `ee298b8`, before any Wave 2 task; when an earlier task has shifted them, match on the quoted code, not the number. The suite collects **1757** tests at HEAD.

**Branch.** `git checkout -b feat/wave2-correct-the-inputs` from `main` at `ee298b8` before Task 1.

**Three facts that shape the tasks.**

1. *The error sample has ONE implementation and four consumers.* `storage._forecast_rows_in_local_day` (storage.py:727-756) keeps rows with `start <= fetched < end` from `config.local_day_bounds_utc`; `_forecast_means_in_local_day` averages it (→ `forecast_error_samples` / `forecast_error_samples_dated` → `entry_manager.forecast_bias_stats`, `calibration.measured_error_spread`, `calibration.corrected_error_rmse`, `calibration.pooled_error_spread`; and `forecast_means_by_date` → `bucket_bias`), and `forecast_source_mix_by_date` takes its key set (→ the mix guard). Narrowing that one function narrows all of them consistently. The scheduler fetches forecasts all day (`_run_collection_cycle` rides along on every monitor_only tick, scheduler.py:368-385), so afternoon rows DO exist and the window matters. `config.SCHEDULE_WINDOWS` (config.py:2404-2406) runs collection 04:00–05:00 and primary entries 05:00–08:00 local, so the half-open window `(4, 8)` contains exactly the fetches an entry decision could have seen.
2. *A prototype of the 2a window was run against the suite (scratchpad plugin, read-only) and 23 existing tests fail because their fixtures fetch at `T00:00:00+00:00` (= 08:00 local at UTC+8, the excluded half-open end) or `T02:00:00+00:00`/`T05:00:00+00:00` (10:00/13:00 local).* They are in `tests/test_forecast_bias.py`, `tests/test_spread_estimator.py`, `tests/test_spread_tier_brier_days.py`, `tests/test_station_maturity.py`. Task 1 moves those fetch times to 05:00 local via `config.local_day_bounds_utc(icao, target)[0] + timedelta(hours=5)`, which is inside the window for every station regardless of offset.
3. *`gate_edge` is already side-adjusted on both bases.* `ev_engine.compute_ev_table` (ev_engine.py:308, 329) sets `model_prob = side_model_prob` (P(this side wins)) and `raw_edge = side_model_prob - price`; the isotonic map is applied to `side_model_prob` (ev_engine.py:369-371) and `entry_manager._calibrated_or_raw_edge` (entry_manager.py:331-334) returns `calibrated - market_price`. A negative `gate_edge` therefore means "this side is overpriced" on EITHER basis, so the signed compare in 2c is correct on both and the flag applies to both (the raw basis is unreachable with a negative edge live, because `ev_engine.best_opportunities` screens on `net_ev_per_dollar` first; the calibrated basis is the one that matters). Probed on the current code: model_prob 0.09 at ask 0.05 with a calibrated 0.0 refuses today at `kelly_nonpositive`; after 2c it refuses at `0a2`.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `config.py` | modify | `error_sample_fetch_bounds_utc` after `local_day_bounds_utc`; the `WAVE 2` flag block at the end of the file (2a/2b/2c/2d/2e flags, `MEASURED_SPREAD_MIN_C`, `REGIME_BOUNDARIES`); the `SPREAD_FLOOR_C` note corrected |
| `storage.py` | modify | `forecast_rows_in_error_sample` (pure) + `_forecast_rows_in_sample_window` (renamed from `_forecast_rows_in_local_day`); four stale docstrings corrected |
| `calibration.py` | modify | `_clamp_spread(measured=)`, `priced_measured_spread`, `corrected_error_rmse_from_dated`, `measured_error_spread_from_errors`, `MEASURED_SPREAD_SOURCES`; measured tiers exempt from the floor |
| `entry_manager.py` | modify | `edge_misses_bar`, `today_source_mix_for`; veto 0a2 signed; mix guard `is not None`; stale comment at 826-829 |
| `backtest/entry_sim.py` | modify | `REPLAY_BOOK_MODE`, `execution_mode` parameter, `_book_has_stop` threaded into the haircut, exit-fee term in `net_ev_at_size`, `edge_misses_bar` at gate 2 |
| `probability_calibration.py` | modify | `calibration_for` does not cache a failed fit |
| `regimes.py` | create | `boundaries`, `segment_labels`, `segment_index`, `regime_segments` |
| `cohort_monitor.py` | modify | `--no-regime-split`; per-regime all-time blocks by default |
| `calibration_panel.py` | modify | `render_regime_split_html`; `cohort_card(regime_split=True)` |
| `promotion_dossier.py` | modify | `_print_calibration_stats`; per-regime BEATS_MARKET blocks; `--no-regime-split` |
| `wave2_falsifier.py` | create | read-only operator script: reads (i)–(iv) and the stop-condition verdict |
| `tests/test_wave2_error_sample_window.py` | create | 2a: bounds, half-open window, on/off, means and mix share it, pure filter parity, stale comments gone |
| `tests/test_forecast_bias.py`, `tests/test_spread_estimator.py`, `tests/test_spread_tier_brier_days.py`, `tests/test_station_maturity.py` | modify | fixtures fetch at 05:00 local |
| `tests/test_wave2_spread_floor_exemption.py` | create | 2b: per-tier floor behaviour, `MEASURED_SPREAD_MIN_C`, ceiling still applies, flag off, upper gate untouched, pure extractions agree |
| `tests/test_corrected_error_spread_tier.py`, `tests/test_spread_estimator.py` | modify | the two assertions that pinned the floor on a measured tier |
| `tests/test_wave2_signed_admission_edge.py` | create | 2c: negative calibrated edge → `0a2` live and sim; flag off → `kelly_nonpositive`; NO-side raw basis |
| `tests/test_parity_entry.py` | modify | gate-7 case relabelled; exit-fee case added |
| `tests/test_wave2_calibration_retry.py` | create | 2d: failing fit then working fit yields a map; flag off caches |
| `tests/test_wave2_forecast_outage_refuses.py` | create | 2e: `[]` → `frozenset()` → refused at `collection_gate`; `None` → guard skipped; flag off |
| `tests/test_wave2_entry_sim_parity.py` | create | 2f: haircut and exit-fee parity on a stopless book |
| `tests/test_wave2_regimes.py` | create | `regimes.py` pure behaviour |
| `tests/test_wave2_regime_reports.py` | create | the three reports split by default and pool on request |
| `tests/test_wave2_falsifier.py` | create | reads (i)–(iv) and both stop-condition verdicts on a fixture DB, read-only connection |
| `tests/test_wave2_regime_boundary_stamp.py` | create | every boundary parses as an ISO date, is not in the future, strictly increasing |

---

### Task 1: 2a — the error sample admits only forecasts fetched 04:00–08:00 local
**Files:** Modify `config.py` (helper after `local_day_bounds_utc`, line 159; new `WAVE 2` block after `STATION_MATURITY = _MaturityMapping()`, line 4998), `storage.py` (`_forecast_rows_in_local_day` 727-756 and its two callers at 723 and 909; docstrings at 776-777, 783-786, 826-829, 859-860), `tests/test_forecast_bias.py` (253, 258), `tests/test_spread_estimator.py` (47), `tests/test_spread_tier_brier_days.py` (36), `tests/test_station_maturity.py` (52, 130) / Test `tests/test_wave2_error_sample_window.py`
**Interfaces:** Consumes: `config.local_day_bounds_utc` / Produces: `config.ERROR_SAMPLE_FETCH_WINDOW_LOCAL: tuple = (4, 8)`, `config.ERROR_SAMPLE_FETCH_WINDOW_ENABLED: bool = True`, `config.error_sample_fetch_bounds_utc(station, target_date, enabled: Optional[bool] = None) -> tuple[datetime, datetime]`, `storage.forecast_rows_in_error_sample(station_icao, rows, window_enabled: Optional[bool] = None) -> Dict[date, List[Tuple[str, float]]]` (pure; `rows` in the shape `forecast_rows_with_fetch_time` returns), `storage._forecast_rows_in_sample_window(station_icao)` (the I/O wrapper the four consumers call)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave2_error_sample_window.py
"""
Wave 2 item 2a. The bias / RMSE / spread / source-mix error sample is fitted
on forecast rows fetched INSIDE the pre-entry window -- local 04:00-08:00 of
the target day (config.SCHEDULE_WINDOWS: collection 04:00-05:00, entries
05:00-08:00) -- not on every row fetched during the local day. A row fetched
at 14:00 local has, at most stations, already seen the afternoon maximum it
claims to forecast, and the sd measured on it is not the sd the 05:00 entry
decision faces (edge review 2026-09-15: morning-only sd +6.5%).

ONE window, ONE implementation: storage.forecast_rows_in_error_sample is the
pure filter, and every consumer (bias, corrected RMSE, measured spread, the
pooled spread, bucket_bias, the mix guard) reaches it through
storage._forecast_rows_in_sample_window. wave2_falsifier.py reuses the pure
function on rows it fetched read-only.
"""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import config
import storage

TARGET = date(2026, 8, 10)
# WSSS is UTC+8: its 2026-08-10 runs 2026-08-09T16:00Z .. 2026-08-10T16:00Z,
# so local 04:00 is 2026-08-09T20:00Z and local 08:00 is 2026-08-10T00:00Z.
AT_0359 = "2026-08-09T19:59:00+00:00"
AT_0500 = "2026-08-09T21:00:00+00:00"
AT_0800 = "2026-08-10T00:00:00+00:00"
AT_1400 = "2026-08-10T06:00:00+00:00"


def _db_with(tmp_path, monkeypatch, forecast_rows):
    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE observations (station_icao TEXT, target_date TEXT, "
        "max_temp_c REAL, source TEXT)"
    )
    conn.execute(
        "CREATE TABLE forecasts (station_icao TEXT, source TEXT, target_date TEXT, "
        "max_temp_c REAL, fetched_at TEXT, raw_note TEXT)"
    )
    conn.execute(
        "INSERT INTO observations VALUES ('WSSS','2026-08-10',32.0,'metar_daily_max')"
    )
    for source, temp, fetched_at in forecast_rows:
        conn.execute(
            "INSERT INTO forecasts VALUES ('WSSS',?,'2026-08-10',?,?,'')",
            (source, temp, fetched_at),
        )
    conn.commit()
    conn.close()


def test_the_window_bounds_are_local_04_to_08():
    lo, hi = config.error_sample_fetch_bounds_utc("WSSS", TARGET)
    assert lo == datetime(2026, 8, 9, 20, tzinfo=timezone.utc)
    assert hi == datetime(2026, 8, 10, 0, tzinfo=timezone.utc)


def test_disabled_bounds_are_the_whole_local_day():
    assert config.error_sample_fetch_bounds_utc("WSSS", TARGET, enabled=False) == \
        config.local_day_bounds_utc("WSSS", TARGET)


def test_the_flag_is_the_default_for_the_bounds(monkeypatch):
    monkeypatch.setattr(config, "ERROR_SAMPLE_FETCH_WINDOW_ENABLED", False)
    assert config.error_sample_fetch_bounds_utc("WSSS", TARGET) == \
        config.local_day_bounds_utc("WSSS", TARGET)


def test_a_1400_local_fetch_is_excluded_and_a_0500_fetch_included(tmp_path, monkeypatch):
    """THE CHANGE. Only the 05:00 row counts: 30.0 - 32.0. Averaging both
    would give 32.0 - 32.0 = 0.0, i.e. the afternoon row -- which has seen
    the maximum -- would zero the measured error."""
    _db_with(tmp_path, monkeypatch, [("a", 30.0, AT_0500), ("b", 34.0, AT_1400)])
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [-2.0]


def test_the_window_is_half_open_at_both_ends(tmp_path, monkeypatch):
    """03:59 is before collection opens; 08:00 is the instant entries shut.
    Neither is a fetch an entry decision could have used."""
    _db_with(tmp_path, monkeypatch, [
        ("a", 30.0, AT_0500), ("b", 20.0, AT_0359), ("c", 40.0, AT_0800),
    ])
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [-2.0]


def test_flag_off_restores_the_local_day_sample(tmp_path, monkeypatch):
    """The revert path: every row inside the local day counts again."""
    monkeypatch.setattr(config, "ERROR_SAMPLE_FETCH_WINDOW_ENABLED", False)
    _db_with(tmp_path, monkeypatch, [("a", 30.0, AT_0500), ("b", 34.0, AT_1400)])
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [0.0]


def test_the_means_and_the_mix_share_the_window(tmp_path, monkeypatch):
    """bucket_bias scores forecast_means_by_date and the mix guard reads
    forecast_source_mix_by_date; both must see the same rows the bias saw."""
    _db_with(tmp_path, monkeypatch, [("a", 30.0, AT_0500), ("b", 34.0, AT_1400)])
    assert storage.forecast_means_by_date("WSSS") == {TARGET: 30.0}
    assert storage.forecast_source_mix_by_date("WSSS") == {TARGET: frozenset({"a"})}


def test_the_pure_filter_is_what_the_storage_path_runs(tmp_path, monkeypatch):
    _db_with(tmp_path, monkeypatch, [
        ("a", 30.0, AT_0500), ("b", 34.0, AT_1400), ("c", 31.0, AT_0359),
    ])
    rows = storage.forecast_rows_with_fetch_time("WSSS")
    assert storage.forecast_rows_in_error_sample("WSSS", rows) == \
        storage._forecast_rows_in_sample_window("WSSS")
    assert storage.forecast_rows_in_error_sample("WSSS", rows, window_enabled=False) == {
        TARGET: [("a", 30.0), ("b", 34.0), ("c", 31.0)],
    }


def test_the_old_local_day_name_is_gone():
    """The rename is deliberate: a function called _in_local_day that applies
    a four-hour window is the stale-comment problem in a function name."""
    assert not hasattr(storage, "_forecast_rows_in_local_day")


def test_the_stale_lookahead_comments_are_corrected():
    """
    Spec 2a: 'storage.py comment at the day filter corrected'. Three
    docstrings still described the pre-2026-08-21 UTC comparison, and one
    said the sample 'mirrors blend_central_estimate's own forecast term' --
    which it no longer does once the window is narrower than the blend's.
    """
    assert "date(fetched_at) <= target_date" not in storage.forecast_rows_with_fetch_time.__doc__
    assert "date(f.fetched_at) <= target_date" not in storage.forecast_means_by_date.__doc__
    assert "on or before the target date" not in storage.forecast_error_samples.__doc__
    assert "mirrors blend_central_estimate" not in storage.forecast_error_samples.__doc__
    assert "ERROR_SAMPLE_FETCH_WINDOW_LOCAL" in storage.forecast_error_samples.__doc__
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave2_error_sample_window.py -v` / Expected: FAIL with `AttributeError: module 'config' has no attribute 'error_sample_fetch_bounds_utc'` (and `assert [0.0] == [-2.0]` once the helper exists)

- [ ] **Step 3: Write minimal implementation**

config.py, directly after `local_day_bounds_utc` returns (line 159, `return start, start + timedelta(days=1)`):

```python


def error_sample_fetch_bounds_utc(
    station: Union[str, StationConfig], target_date: date, enabled: Optional[bool] = None
) -> tuple:
    """
    (start, end) of the fetch window a forecast row must fall in to enter
    the error sample -- the bias, the corrected RMSE, the measured spread,
    the pooled spread and the source mix are ALL fitted on this set
    (storage.forecast_rows_in_error_sample). Half-open: start <= t < end.

    WAVE 2 (2a). local_day_bounds_utc() above cuts hindsight at the END of
    the local day, which stops a 23:00 row that has seen the maximum from
    entering. It does not stop a 14:00 row, which at most stations has seen
    it too: the daily maximum lands early-to-mid afternoon, and the
    scheduler keeps fetching all day (scheduler._run_collection_cycle rides
    along on every monitor_only tick). Measured on the production record
    2026-09-15, the morning-only error sd is +6.5% wider than the all-day
    one -- the all-day sample was making the model look sharper than the
    05:00 decision actually is.

    The window is ERROR_SAMPLE_FETCH_WINDOW_LOCAL in LOCAL hours of the
    target day: (4, 8) is exactly the fetches an entry decision could have
    seen (SCHEDULE_WINDOWS: collection 04:00-05:00, entries 05:00-08:00).
    Anchored on the local day's start, so a DST station is right in both
    halves of the year for the same reason local_day_bounds_utc is.

    `enabled` defaults to ERROR_SAMPLE_FETCH_WINDOW_ENABLED; passing it
    explicitly is how wave2_falsifier.py measures both windows from one
    set of rows without flipping the production flag.
    """
    start, end = local_day_bounds_utc(station, target_date)
    if enabled is None:
        enabled = ERROR_SAMPLE_FETCH_WINDOW_ENABLED
    if not enabled:
        return start, end
    lo_h, hi_h = ERROR_SAMPLE_FETCH_WINDOW_LOCAL
    return start + timedelta(hours=lo_h), start + timedelta(hours=hi_h)
```

config.py, appended after the last line (`STATION_MATURITY = _MaturityMapping()`, line 4998):

```python


# ===========================================================================
# WAVE 2 (2026-09-21) -- CORRECT THE INPUTS, ONE DAY.
# docs/superpowers/specs/2026-09-17-evidence-first-remediation-design.md
#
# Every flag here defaults ON and is read at exactly one site, named beside
# it. A revert is a flip here, never a code change, so the revert lands on
# one day too. The stop condition (wave2_falsifier.py, read iii) flips
# ERROR_SAMPLE_FETCH_WINDOW_ENABLED and SPREAD_FLOOR_MEASURED_TIERS_EXEMPT
# off TOGETHER -- never one alone, because the pre-registered reads cannot
# attribute a P&L move to one of the two.
# ===========================================================================

# 2a. WHICH FETCHES ENTER THE ERROR SAMPLE. Local hours of the target day,
# half-open. Read by error_sample_fetch_bounds_utc(); applied in
# storage.forecast_rows_in_error_sample(). See the helper's docstring for
# the measurement and for why (4, 8) and not the local day.
ERROR_SAMPLE_FETCH_WINDOW_LOCAL = (4, 8)
ERROR_SAMPLE_FETCH_WINDOW_ENABLED = True
```

storage.py `_forecast_rows_in_local_day` (line 727-756) — old:
```python
def _forecast_rows_in_local_day(station_icao: str) -> Dict[date, List[Tuple[str, float]]]:
    """
    {target_date: [(source, max_temp_c)]} for rows fetched INSIDE the
    station's own local target day.

    The single implementation of that window. _forecast_means_in_local_day()
    averages it and forecast_source_mix_by_date() takes its key set, so the
    bias and the mix that guards it can never be measured over different
    forecast sets.
    """
    station = config.get_station(station_icao)
    buckets: Dict[date, List[Tuple[str, float]]] = {}
    for target_date_iso, fetched_at, temp_c, source in forecast_rows_with_fetch_time(
        station_icao
    ):
        try:
            target_date = date.fromisoformat(target_date_iso)
            fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            # A row we cannot place in time cannot be shown to be free of
            # lookahead, so it does not get the benefit of the doubt.
            continue
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        start, end = config.local_day_bounds_utc(station, target_date)
        if not (start <= fetched < end):
            continue
        buckets.setdefault(target_date, []).append((source, temp_c))
    return buckets
```
new:
```python
def forecast_rows_in_error_sample(
    station_icao: str,
    rows: List[Tuple[str, str, float, str]],
    window_enabled: Optional[bool] = None,
) -> Dict[date, List[Tuple[str, float]]]:
    """
    {target_date: [(source, max_temp_c)]} for the rows an ERROR SAMPLE may
    be fitted on: fetched inside config.error_sample_fetch_bounds_utc() --
    local 04:00-08:00 of the target day (WAVE 2, 2a), or the whole local
    day when ERROR_SAMPLE_FETCH_WINDOW_ENABLED is off.

    PURE over `rows` in the shape forecast_rows_with_fetch_time() returns,
    so wave2_falsifier.py can apply the same rule to rows it read through
    a read-only connection. `window_enabled` overrides the flag for that
    caller only; production callers leave it None.

    The single implementation of the window. _forecast_means_in_local_day()
    averages it and forecast_source_mix_by_date() takes its key set, so the
    bias, the corrected RMSE, the measured and pooled spreads, bucket_bias
    and the mix guard can never be measured over different forecast sets.
    """
    station = config.get_station(station_icao)
    buckets: Dict[date, List[Tuple[str, float]]] = {}
    for target_date_iso, fetched_at, temp_c, source in rows:
        try:
            target_date = date.fromisoformat(target_date_iso)
            fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            # A row we cannot place in time cannot be shown to be free of
            # lookahead, so it does not get the benefit of the doubt.
            continue
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        start, end = config.error_sample_fetch_bounds_utc(
            station, target_date, enabled=window_enabled
        )
        if not (start <= fetched < end):
            continue
        buckets.setdefault(target_date, []).append((source, temp_c))
    return buckets


def _forecast_rows_in_sample_window(station_icao: str) -> Dict[date, List[Tuple[str, float]]]:
    """forecast_rows_in_error_sample() over this station's stored rows --
    the I/O wrapper every consumer in this module calls."""
    return forecast_rows_in_error_sample(
        station_icao, forecast_rows_with_fetch_time(station_icao)
    )
```

storage.py `_forecast_means_in_local_day` body (line 721-724) — old:
```python
    return {
        d: sum(t for _, t in rows) / len(rows)
        for d, rows in _forecast_rows_in_local_day(station_icao).items()
    }
```
new:
```python
    return {
        d: sum(t for _, t in rows) / len(rows)
        for d, rows in _forecast_rows_in_sample_window(station_icao).items()
    }
```
and in its docstring (line 694-697) — old:
```python
    Per target date, the mean of the forecasts ISSUED DURING THAT
    STATION'S OWN LOCAL TARGET DAY -- the set the live blend actually
    averages, and the single definition both public callers below share.
```
new:
```python
    Per target date, the mean of the forecasts fetched INSIDE THE ERROR-
    SAMPLE WINDOW (config.error_sample_fetch_bounds_utc: local 04:00-08:00
    of the target day since WAVE 2, the whole local day before it) -- the
    single definition both public callers below share. Note this is
    NARROWER than the set blend_central_estimate() averages on a given
    tick, deliberately: see forecast_error_samples().
```

storage.py `forecast_source_mix_by_date` body (line 908-911) — old:
```python
    return {
        d: frozenset(src for src, _ in rows)
        for d, rows in _forecast_rows_in_local_day(station_icao).items()
    }
```
new:
```python
    return {
        d: frozenset(src for src, _ in rows)
        for d, rows in _forecast_rows_in_sample_window(station_icao).items()
    }
```
and its docstring line 900 `Shares _forecast_rows_in_local_day() with forecast_error_samples() ON` → `Shares _forecast_rows_in_sample_window() with forecast_error_samples() ON`.

storage.py `forecast_error_samples` docstring (line 766-786) — old:
```python
    Two deliberate choices:
      - `source` is the station's own resolution_grade_source, matching
        count_observations_from_source(): the error must be measured
        against the record the market actually settles on, not against a
        convenient proxy.
      - Only forecasts fetched on or before the target date count. A row
        fetched afterwards has seen the day it is "forecasting" and would
        flatter the bias toward zero -- the same lookahead the backtest
        goes to lengths to avoid.

    The per-date forecast mean mirrors blend_central_estimate's own
    forecast term, so the number measured is the number corrected.
```
new:
```python
    Two deliberate choices:
      - `source` is the station's own resolution_grade_source, matching
        count_observations_from_source(): the error must be measured
        against the record the market actually settles on, not against a
        convenient proxy.
      - Only forecasts fetched inside config.ERROR_SAMPLE_FETCH_WINDOW_LOCAL
        (local 04:00-08:00 of the target day; WAVE 2, 2a) count. A row
        fetched later has, at most stations, already seen the maximum it
        is "forecasting" and would flatter the error toward zero -- the
        same lookahead the backtest goes to lengths to avoid, one step
        earlier in the day than the local-day cut caught it.

    The per-date forecast mean is therefore the MORNING forecast term --
    the one the 05:00-08:00 entry decision actually had -- and NOT the
    all-day set blend_central_estimate() would average on a later tick.
    The number measured is the number the entry path corrects with.
```

storage.py `forecast_means_by_date` docstring (line 826-829) — old:
```python
    THIS IS forecast_error_samples()' FORECAST TERM, LIFTED OUT. Same
    sources, same exclusion list, same `date(f.fetched_at) <= target_date`
    lookahead rule, same AVG -- the only thing dropped is the join to
    observations, because the caller supplies the truth instead.
```
new:
```python
    THIS IS forecast_error_samples()' FORECAST TERM, LIFTED OUT. Same
    sources, same exclusion list, same fetch window
    (config.error_sample_fetch_bounds_utc), same AVG -- the only thing
    dropped is the join to observations, because the caller supplies the
    truth instead.
```

storage.py `forecast_rows_with_fetch_time` docstring (line 858-866) — old:
```python
    THE TWO OMISSIONS ARE THE POINT, and neither is a relaxation.
    forecast_error_samples() and forecast_means_by_date() both AVG per
    target date and both cut lookahead at `date(fetched_at) <= target_date`
    -- a UTC calendar comparison. spread_audit.py needs to re-average the
    subset of rows that existed at a given LEAD, measured against the end
    of the station's own local day, so it needs the fetch times the AVG
    throws away and a cutoff strictly tighter than the UTC one (a station
    at UTC+8 finishes its day at 16:00Z, so 16:00Z-23:59Z on the target
    date is same-calendar-day and still lookahead). Handing back rows and
    letting the caller cut is the only way to get that; the caller is
    responsible for applying a non-negative lead, and its tests pin it.
```
new:
```python
    THE TWO OMISSIONS ARE THE POINT, and neither is a relaxation.
    forecast_error_samples() and forecast_means_by_date() both AVG per
    target date and both apply the error-sample fetch window
    (forecast_rows_in_error_sample: local 04:00-08:00 of the target day).
    spread_audit.py needs to re-average the subset of rows that existed at
    a given LEAD, measured against the end of the station's own local day,
    so it needs the fetch times the AVG throws away and its own cutoff.
    Handing back rows and letting the caller cut is the only way to get
    that; the caller is responsible for applying a non-negative lead, and
    its tests pin it.
```

Fixture edits (fact 2 above). Each moves the fetch to 05:00 local for the station in hand.

tests/test_forecast_bias.py (line 253 and 258) — old:
```python
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','b','2026-08-01',31.0,'2026-08-01T05:00:00+00:00','')")
```
new:
```python
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','b','2026-08-01',31.0,'2026-07-31T23:00:00+00:00','')")
```
old:
```python
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','a','2026-08-02',30.0,'2026-08-02T00:00:00+00:00','')")
```
new:
```python
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','a','2026-08-02',30.0,'2026-08-01T21:00:00+00:00','')")
```

tests/test_spread_estimator.py (line 45-48) — old:
```python
        storage.save_forecast(PointForecast(
            station_icao=icao, source="open_meteo_ecmwf", target_date=target,
            max_temp_c=truth + err, fetched_at=f"{target.isoformat()}T00:00:00+00:00",
        ))
```
new:
```python
        storage.save_forecast(PointForecast(
            station_icao=icao, source="open_meteo_ecmwf", target_date=target,
            max_temp_c=truth + err,
            # 05:00 local, inside the WAVE 2 error-sample window for any offset.
            fetched_at=(config.local_day_bounds_utc(icao, target)[0]
                        + timedelta(hours=5)).isoformat(),
        ))
```
(`from datetime import date, timedelta` and `import config` already exist at lines 15 and 20 of that file.)

tests/test_spread_tier_brier_days.py (line 32-37) — old:
```python
def _seed_forecast(target_date, value, fetched_at=None):
    storage.save_forecast(PointForecast(
        station_icao=STATION, source=SOURCE, target_date=target_date,
        max_temp_c=value,
        fetched_at=fetched_at or f"{target_date.isoformat()}T02:00:00+00:00",
    ))
```
new:
```python
def _seed_forecast(target_date, value, fetched_at=None):
    storage.save_forecast(PointForecast(
        station_icao=STATION, source=SOURCE, target_date=target_date,
        max_temp_c=value,
        # 05:00 local: inside the WAVE 2 error-sample window (2a).
        fetched_at=fetched_at or (
            config.local_day_bounds_utc(STATION, target_date)[0] + timedelta(hours=5)
        ).isoformat(),
    ))
```
(`from datetime import date, timedelta` and `import config` already exist at lines 14 and 18 of that file.)

tests/test_station_maturity.py (line 49-53 and 128-131; `timedelta` and `config` are imported at lines 13 and 17) — old:
```python
        storage.save_forecast(PointForecast(
            station_icao=icao, source="open_meteo_ecmwf", target_date=target,
            max_temp_c=32.0 + errors[i % len(errors)],
            fetched_at=f"{target.isoformat()}T00:00:00+00:00",
        ))
```
new:
```python
        storage.save_forecast(PointForecast(
            station_icao=icao, source="open_meteo_ecmwf", target_date=target,
            max_temp_c=32.0 + errors[i % len(errors)],
            fetched_at=(config.local_day_bounds_utc(icao, target)[0]
                        + timedelta(hours=5)).isoformat(),
        ))
```
old:
```python
        storage.save_forecast(PointForecast(
            station_icao="WSSS", source="open_meteo_ecmwf", target_date=target,
            max_temp_c=32.0 + err, fetched_at=f"{target.isoformat()}T00:00:00+00:00",
        ))
```
new:
```python
        storage.save_forecast(PointForecast(
            station_icao="WSSS", source="open_meteo_ecmwf", target_date=target,
            max_temp_c=32.0 + err,
            fetched_at=(config.local_day_bounds_utc("WSSS", target)[0]
                        + timedelta(hours=5)).isoformat(),
        ))
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave2_error_sample_window.py tests/test_forecast_bias.py tests/test_spread_estimator.py tests/test_spread_tier_brier_days.py tests/test_station_maturity.py tests/test_forecast_lead_window.py -v` / Expected: PASS (10 new + every pre-existing test in those files)
- [ ] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1767
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add config.py storage.py tests/test_wave2_error_sample_window.py tests/test_forecast_bias.py tests/test_spread_estimator.py tests/test_spread_tier_brier_days.py tests/test_station_maturity.py
git commit -m "Wave 2 (2a): error sample admits only forecasts fetched 04:00-08:00 local

config.error_sample_fetch_bounds_utc narrows the local-day bound to
ERROR_SAMPLE_FETCH_WINDOW_LOCAL=(4, 8) -- exactly the collection and entry
windows -- behind ERROR_SAMPLE_FETCH_WINDOW_ENABLED. storage applies it in
the new pure forecast_rows_in_error_sample (renamed I/O wrapper
_forecast_rows_in_sample_window), so the bias, corrected RMSE, measured
and pooled spreads, bucket_bias and the mix guard all move together.
Four stale docstrings that still described the pre-2026-08-21 UTC
comparison or claimed the sample mirrors the blend's own term are
corrected. Four test fixtures move their fetch time to 05:00 local.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: 2b — the spread floor applies only to unmeasured tiers
**Files:** Modify `calibration.py` (`_clamp_spread` 250-266; `measured_error_spread` 269-292; `corrected_error_rmse` 315-360; `estimate_std_dev` measured tier 553-559), `config.py` (the `SPREAD_FLOOR_C` note 4209-4216; the `WAVE 2` block), `tests/test_corrected_error_spread_tier.py` (`test_it_falls_back_to_the_naive_sd_below_the_pair_floor`, line 78-95), `tests/test_spread_estimator.py` (`test_a_tiny_measured_spread_is_raised_to_the_floor`, line 144-155) / Test `tests/test_wave2_spread_floor_exemption.py`
**Interfaces:** Consumes: Task 1 (the measured tiers now read the morning sample) / Produces: `config.SPREAD_FLOOR_MEASURED_TIERS_EXEMPT: bool = True`, `config.MEASURED_SPREAD_MIN_C: float = 0.30`, `calibration.MEASURED_SPREAD_SOURCES: frozenset`, `calibration._clamp_spread(value, station_icao=None, measured: bool = False)`, `calibration.priced_measured_spread(value, station_icao) -> float`, `calibration.corrected_error_rmse_from_dated(dated) -> (rmse, n)`, `calibration.measured_error_spread_from_errors(errors) -> (sd, n)` (both pure; the I/O functions wrap them). `spread_source` strings are unchanged.

**Why the replay cannot see this.** `backtest/engine.py:988` passes `allow_measured_spread=False` unconditionally, so `estimate_std_dev` short-circuits to `"replay_constant"` (calibration.py:513-526) before either measured tier runs. No replay result can confirm or refute 2b (or 2a); the safety argument is the pre-registered read (ii) and the stop condition in `wave2_falsifier.py`, not a backtest. This plan does not change engine.py.

**Why 0.30.** Settlement rounds the daily maximum to a whole degree (1C buckets on Asia/Europe; 2F = 1.11C on 11 US cities). A forecast error sample is therefore measured against a truth that carries uniform rounding noise of width one bucket, whose sd is `sqrt(1/12) = 0.289C` (0.32C at 1.11C). No honest error sd can sit below that: a measured value under it is a sample artefact (few pairs, identical rounded errors), not a sharper forecast. `MEASURED_SPREAD_MIN_C = 0.30` is that bound, rounded to the grid — a NUMERICAL-SANITY floor, not a confidence floor. The confidence floor was `SPREAD_FLOOR_C = 0.70`, and the 2026-09-09 EDDM/MMMX finding is that it is wrong in both directions on a two-sided bucket market (config.py:4180-4207).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave2_spread_floor_exemption.py
"""
Wave 2 item 2b. SPREAD_FLOOR_C (0.70) stays on every UNMEASURED tier
(ensemble, pooled_error, fallback_default, replay_constant). A MEASURED
tier -- corrected_error, measured_error: this station's own error record --
prices its own value, subject to (i) the existing upper gate
config.MAX_ERROR_RMSE_PER_BUCKET in entry_manager.collection_only_reason and
(ii) MEASURED_SPREAD_MIN_C = 0.30, the settlement-rounding sd sqrt(1/12),
below which no error sd is a measurement.

WHY. EDDM's priced sd pinned to EXACTLY 0.700 against a measured 0.445; the
model underpriced the WINNING bucket on 10 of 10 days and sold it as NO
(EDDM 0-for-11 on NO). A too-wide spread manufactures NO-side edges on the
bucket most likely to win. See config.SPREAD_FLOOR_C's note.

THE REPLAY CANNOT SCORE THIS: backtest/engine.py passes
allow_measured_spread=False unconditionally. Pinned below so nobody reads a
sweep as evidence about it.
"""
from datetime import date, timedelta

import pytest

import calibration
import config

ASIA = "ZSPD"      # ceiling 2.0
EUROPE = "EDDM"    # ceiling 2.0
AMERICAS = "KSEA"  # ceiling None


def _tiers(monkeypatch, corrected, measured):
    monkeypatch.setattr(calibration, "corrected_error_rmse", lambda icao: corrected)
    monkeypatch.setattr(calibration, "measured_error_spread", lambda icao: measured)


# --- measured tiers price their own value ------------------------------------

def test_a_corrected_rmse_under_the_old_floor_is_priced_as_is(monkeypatch):
    """EDDM: 0.445 measured, 0.700 priced. Now 0.45 (rounded to the grid)."""
    _tiers(monkeypatch, (0.445, 20), (0.452, 20))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "corrected_error"
    assert sd == 0.45


def test_the_naive_sd_tier_is_exempt_too(monkeypatch):
    _tiers(monkeypatch, (None, 9), (0.452, 14))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "measured_error"
    assert sd == 0.45


def test_a_measured_value_below_the_rounding_sd_is_raised_to_it(monkeypatch):
    """sqrt(1/12) = 0.289: a 0.20 'measurement' against whole-degree truth
    is a sample artefact, not a sharper forecast."""
    _tiers(monkeypatch, (0.20, 20), (0.20, 20))
    sd, source = calibration.estimate_std_dev([], [], station_icao=ASIA)
    assert source == "corrected_error"
    assert sd == config.MEASURED_SPREAD_MIN_C == 0.30


def test_the_regional_ceiling_still_applies_to_a_measured_tier(monkeypatch):
    _tiers(monkeypatch, (2.6, 20), (2.6, 20))
    sd, _ = calibration.estimate_std_dev([], [], station_icao=ASIA)
    assert sd == config.SPREAD_CEILING_C
    sd, _ = calibration.estimate_std_dev([], [], station_icao=AMERICAS)
    assert sd == 2.6


# --- unmeasured tiers keep the floor -----------------------------------------

def test_the_ensemble_tier_keeps_the_floor(monkeypatch):
    _tiers(monkeypatch, (None, 0), (None, 0))
    sd, source = calibration.estimate_std_dev(
        [], [], ensemble_members=[32.0, 32.1, 31.9, 32.05], station_icao=EUROPE)
    assert source == "ensemble"
    assert sd == config.SPREAD_FLOOR_C


def test_the_pooled_tier_keeps_the_floor(monkeypatch):
    _tiers(monkeypatch, (None, 0), (None, 0))
    monkeypatch.setattr(calibration, "pooled_error_spread", lambda region=None: (0.41, 200))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "pooled_error"
    assert sd == config.SPREAD_FLOOR_C


def test_the_fallback_and_replay_constants_are_untouched(monkeypatch):
    _tiers(monkeypatch, (None, 0), (None, 0))
    monkeypatch.setattr(calibration, "pooled_error_spread", lambda region=None: (None, 0))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert (sd, source) == (config.POOLED_SPREAD_FALLBACK_C, "fallback_default")
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE, allow_measured=False)
    assert (sd, source) == (config.POOLED_SPREAD_FALLBACK_C, "replay_constant")


# --- the flag ----------------------------------------------------------------

def test_flag_off_restores_the_unconditional_floor(monkeypatch):
    monkeypatch.setattr(config, "SPREAD_FLOOR_MEASURED_TIERS_EXEMPT", False)
    _tiers(monkeypatch, (0.445, 20), (0.452, 20))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "corrected_error"
    assert sd == config.SPREAD_FLOOR_C


def test_spread_source_strings_are_unchanged():
    assert calibration.MEASURED_SPREAD_SOURCES == frozenset({"corrected_error", "measured_error"})
    assert not (calibration.MEASURED_SPREAD_SOURCES & config.LOW_CONFIDENCE_SPREAD_SOURCES)


# --- the upper gate is the other half of the argument ------------------------

def test_the_error_width_gate_still_stops_a_station_wider_than_its_bucket():
    """The floor comes off the bottom; MAX_ERROR_RMSE_PER_BUCKET stays on top.
    A measured tier is priced as-is BETWEEN the two, never outside them."""
    import entry_manager
    reason = entry_manager.collection_only_reason(
        "ZSPD", 999, bias_n=99, bias_stderr=0.1, enforce_bias_quality=True,
        error_width_ratio=1.2,
    )
    assert reason is not None and "wider than" in reason


# --- the pure extractions the falsifier composes -----------------------------

def test_corrected_rmse_from_dated_is_what_the_io_function_computes(monkeypatch):
    day0 = date(2026, 8, 1)
    dated = [(day0 + timedelta(days=i), e) for i, e in enumerate(
        [0.5, -0.5, 0.4, -0.4, 0.6, -0.6, 0.3, -0.3, 0.5, -0.5, 0.2, -0.2,
         0.4, -0.4, 0.5, -0.5, 0.3, -0.3, 0.6, -0.6, 0.4, -0.4])]
    monkeypatch.setattr(calibration, "_dated_error_samples", lambda icao: dated)
    assert calibration.corrected_error_rmse("ZSPD") == calibration.corrected_error_rmse_from_dated(dated)
    rmse, n = calibration.corrected_error_rmse_from_dated(dated)
    assert rmse is not None and n >= config.MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE


def test_measured_spread_from_errors_matches_the_tier_rule():
    assert calibration.measured_error_spread_from_errors([0.5, -0.5]) == (None, 2)
    sd, n = calibration.measured_error_spread_from_errors([0.5, -0.5, 0.4, -0.4, 0.6, -0.6])
    assert n == 6 and sd == pytest.approx(0.5550, abs=1e-3)  # sqrt(1.54 / 5)


def test_priced_measured_spread_is_the_measured_clamp():
    assert calibration.priced_measured_spread(0.445, EUROPE) == 0.45
    assert calibration.priced_measured_spread(0.20, EUROPE) == config.MEASURED_SPREAD_MIN_C
    assert calibration.priced_measured_spread(2.6, ASIA) == config.SPREAD_CEILING_C


def test_the_replay_path_is_blind_to_this_change_and_says_so():
    import inspect
    from backtest import engine
    assert "allow_measured_spread=False" in inspect.getsource(engine)
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave2_spread_floor_exemption.py -v` / Expected: FAIL with `assert 0.7 == 0.45` on the first test and `AttributeError: module 'calibration' has no attribute 'MEASURED_SPREAD_SOURCES'` / `'priced_measured_spread'` / `'corrected_error_rmse_from_dated'`

- [ ] **Step 3: Write minimal implementation**

config.py `WAVE 2` block, appended after `ERROR_SAMPLE_FETCH_WINDOW_ENABLED = True`:

```python

# 2b. THE SPREAD FLOOR APPLIES ONLY TO UNMEASURED TIERS. Read by
# calibration._clamp_spread(measured=...). A tier that is this station's
# own error record (corrected_error, measured_error) prices its own value
# between MEASURED_SPREAD_MIN_C below and, above, the existing upper gate
# MAX_ERROR_RMSE_PER_BUCKET (entry_manager.collection_only_reason) plus the
# regional ceiling. Ensemble, pooled and fallback tiers keep SPREAD_FLOOR_C.
#
# MEASURED_SPREAD_MIN_C is NUMERICAL SANITY, not confidence. Settlement is
# a whole-degree bucket, so every error in the sample is measured against
# a truth carrying uniform rounding noise of width one bucket: sd =
# sqrt(1/12) = 0.289C (0.32C on a 2F bucket). An error sd below that is a
# sample artefact -- a handful of pairs whose rounded errors coincide --
# and not a sharper forecast. 0.30 is that bound on the 0.01 grid.
#
# THE REPLAY CANNOT SEE THIS: backtest/engine.py passes
# allow_measured_spread=False unconditionally, so every replay prices on
# "replay_constant". The safety argument is wave2_falsifier.py read (ii)
# and the stop condition, not a backtest.
SPREAD_FLOOR_MEASURED_TIERS_EXEMPT = True
MEASURED_SPREAD_MIN_C = 0.30
```

config.py `SPREAD_FLOOR_C` note (line 4209-4216) — old:
```python
# THIS IS DOCUMENTED, NOT FIXED. The floor is still unconditional and still
# binds on every station measuring under 0.70. Standing it down for a
# well-measured station is a real change to what the book buys and is
# deliberately NOT made here -- see
# docs/superpowers/plans/2026-09-09-spread-width-remediation.md Task 3,
# and note that its planned replay gate does not exist:
# backtest/engine.py passes allow_measured_spread=False unconditionally, so
# no replay can see a spread change at all.
SPREAD_FLOOR_C = 0.7
```
new:
```python
# FIXED IN WAVE 2 (2026-09-21), see SPREAD_FLOOR_MEASURED_TIERS_EXEMPT at
# the end of this file: this floor now binds only on the UNMEASURED tiers
# (ensemble, pooled_error, fallback_default). A station's own measured
# error prices as-is between MEASURED_SPREAD_MIN_C and the
# MAX_ERROR_RMSE_PER_BUCKET gate. The replay still cannot see either:
# backtest/engine.py passes allow_measured_spread=False unconditionally,
# so the evidence is wave2_falsifier.py's read (ii), not a sweep.
SPREAD_FLOOR_C = 0.7
```

calibration.py `_clamp_spread` (line 250-266) — old:
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
new:
```python
# The two tiers that are THIS STATION'S OWN error record. Everything else
# estimate_std_dev can return is a property of a model, of the book, or of
# nothing (a constant), and keeps the confidence floor.
MEASURED_SPREAD_SOURCES = frozenset({"corrected_error", "measured_error"})


def _clamp_spread(value: float, station_icao: str = None, measured: bool = False) -> float:
    """
    Hold a spread inside its REGION's band -- see SPREAD_FLOOR_C and
    config.REGION_SPREAD_CEILING_C.

    WAVE 2 (2b): the floor depends on WHAT THE VALUE IS. An unmeasured
    tier (measured=False) keeps SPREAD_FLOOR_C: a too-narrow guess is the
    dangerous direction. A measured tier (measured=True: this station's
    own corrected RMSE or error sd) is floored only at
    config.MEASURED_SPREAD_MIN_C, the settlement-rounding sd -- raising a
    genuinely sharp station to 0.70 manufactures NO-side edges on the
    bucket most likely to win (EDDM, 2026-09-09). Its upper bound is the
    MAX_ERROR_RMSE_PER_BUCKET gate in entry_manager, which stops the
    station outright rather than clamping. SPREAD_FLOOR_MEASURED_TIERS_
    EXEMPT=False restores the unconditional floor.

    The ceiling is regional, and a region whose ceiling is None is not
    clamped at all. station_icao defaults to None for station-agnostic
    callers, which keeps the legacy global ceiling.
    """
    if measured and config.SPREAD_FLOOR_MEASURED_TIERS_EXEMPT:
        floored = max(value, config.MEASURED_SPREAD_MIN_C)
    else:
        floored = max(value, config.SPREAD_FLOOR_C)
    if station_icao is None:
        return round(min(floored, config.SPREAD_CEILING_C), 2)
    ceiling = config.region_spread_ceiling_c(station_icao)
    if ceiling is None:
        return round(floored, 2)
    return round(min(floored, ceiling), 2)


def priced_measured_spread(value: float, station_icao: str) -> float:
    """The spread a MEASURED tier prices for `value` -- _clamp_spread with
    measured=True, public so wave2_falsifier.py reports the number the next
    cycle will actually use."""
    return _clamp_spread(value, station_icao, measured=True)
```

calibration.py `measured_error_spread` (line 269-292) — old:
```python
    if len(errors) < max(2, config.MIN_SPREAD_PAIRS):
        return None, len(errors)
    return statistics.stdev(errors), len(errors)
```
new:
```python
    return measured_error_spread_from_errors(errors)


def measured_error_spread_from_errors(errors: List[float]) -> tuple:
    """measured_error_spread()'s arithmetic over an error list already in
    hand -- pure, so wave2_falsifier.py can run it on rows it read itself."""
    if len(errors) < max(2, config.MIN_SPREAD_PAIRS):
        return None, len(errors)
    return statistics.stdev(errors), len(errors)
```
and in its docstring (line 275-276) `which is lookahead-guarded (only\n    forecasts fetched on or before the target date count)` → `which admits only forecasts fetched inside the error-sample window\n    (config.error_sample_fetch_bounds_utc)`.

calibration.py `corrected_error_rmse` (line 344-360) — old:
```python
    dated = _dated_error_samples(station_icao)
    warmup = max(1, config.MIN_BIAS_PAIRS_BEFORE_ENTRY)

    residuals = []
    for i, (target_date, error_c) in enumerate(dated):
        if i < warmup:
            continue
        bias, _, _ = bias_stats_weighted(
            dated[:i], target_date, config.BIAS_HALF_LIFE_DAYS)
        if bias is None:
            continue
        residuals.append(float(error_c) - bias)

    if len(residuals) < config.MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE:
        return None, len(residuals)
    return math.sqrt(statistics.fmean(r * r for r in residuals)), len(residuals)
```
new:
```python
    return corrected_error_rmse_from_dated(_dated_error_samples(station_icao))


def corrected_error_rmse_from_dated(dated) -> tuple:
    """corrected_error_rmse()'s arithmetic over [(target_date, error_c)]
    sorted oldest first -- pure, so wave2_falsifier.py can run it on rows
    it read through a read-only connection."""
    warmup = max(1, config.MIN_BIAS_PAIRS_BEFORE_ENTRY)

    residuals = []
    for i, (target_date, error_c) in enumerate(dated):
        if i < warmup:
            continue
        bias, _, _ = bias_stats_weighted(
            dated[:i], target_date, config.BIAS_HALF_LIFE_DAYS)
        if bias is None:
            continue
        residuals.append(float(error_c) - bias)

    if len(residuals) < config.MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE:
        return None, len(residuals)
    return math.sqrt(statistics.fmean(r * r for r in residuals)), len(residuals)
```

calibration.py `estimate_std_dev` measured tier (line 553-559) — old:
```python
        corrected, _ = corrected_error_rmse(station_icao)
        if corrected is not None:
            return _clamp_spread(corrected, station_icao), "corrected_error"

        measured, _ = measured_error_spread(station_icao)
        if measured is not None:
            return _clamp_spread(measured, station_icao), "measured_error"
```
new:
```python
        # WAVE 2 (2b): measured=True -- this station's own record is floored
        # at the rounding sd, not at the confidence floor. See _clamp_spread.
        corrected, _ = corrected_error_rmse(station_icao)
        if corrected is not None:
            return _clamp_spread(corrected, station_icao, measured=True), "corrected_error"

        measured, _ = measured_error_spread(station_icao)
        if measured is not None:
            return _clamp_spread(measured, station_icao, measured=True), "measured_error"
```

tests/test_corrected_error_spread_tier.py `test_it_falls_back_to_the_naive_sd_below_the_pair_floor` (line 91-95) — old:
```python
    assert source == "measured_error"
    # SPREAD_FLOOR_C still binds on the fallback. Standing it down is a
    # separate change; this test pins that it did NOT happen here.
    assert sd == config.SPREAD_FLOOR_C
```
new:
```python
    assert source == "measured_error"
    # WAVE 2 (2b) stood the floor down for measured tiers: 0.452 prices as
    # 0.45. tests/test_wave2_spread_floor_exemption.py owns the floor rule.
    assert sd == 0.45
```
and its module docstring paragraph `WHAT THIS CHANGE DELIBERATELY DOES NOT DO. It does not touch\nSPREAD_FLOOR_C, ...` (line 28-32) → replace with `The floor on measured tiers was stood down in WAVE 2 (2b); see\ntests/test_wave2_spread_floor_exemption.py.`

tests/test_spread_estimator.py `test_a_tiny_measured_spread_is_raised_to_the_floor` (line 144-155) — old:
```python
def test_a_tiny_measured_spread_is_raised_to_the_floor():
    """
    The safety-critical direction. A too-narrow spread makes the model look
    certain, which inflates the model-vs-market gap, which is an edge the
    sizing code will happily act on. WSSS's real measured spread (~0.56C)
    sits below the floor.
    """
    seed_pairs("WSSS", [0.01, 0.0, -0.01, 0.0, 0.01, -0.01])
    sd, src = calibration.estimate_std_dev([_fc(32.0)], [_obs(32.0)], station_icao="WSSS")

    assert src == "measured_error"
    assert sd == pytest.approx(config.SPREAD_FLOOR_C)
```
new:
```python
def test_a_tiny_measured_spread_is_raised_to_the_rounding_sd():
    """
    A measured sd of 0.009C against whole-degree truth is a sample artefact.
    WAVE 2 (2b): measured tiers are floored at MEASURED_SPREAD_MIN_C (the
    settlement-rounding sd), no longer at SPREAD_FLOOR_C.
    """
    seed_pairs("WSSS", [0.01, 0.0, -0.01, 0.0, 0.01, -0.01])
    sd, src = calibration.estimate_std_dev([_fc(32.0)], [_obs(32.0)], station_icao="WSSS")

    assert src == "measured_error"
    assert sd == pytest.approx(config.MEASURED_SPREAD_MIN_C)
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave2_spread_floor_exemption.py tests/test_corrected_error_spread_tier.py tests/test_spread_estimator.py tests/test_error_width_gate.py tests/test_low_confidence_spread_gate.py -v` / Expected: PASS (14 new + all pre-existing)
- [ ] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1782
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add calibration.py config.py tests/test_wave2_spread_floor_exemption.py tests/test_corrected_error_spread_tier.py tests/test_spread_estimator.py
git commit -m "Wave 2 (2b): SPREAD_FLOOR_C applies only to unmeasured spread tiers

calibration._clamp_spread takes measured=True from the corrected_error and
measured_error tiers and floors them at MEASURED_SPREAD_MIN_C=0.30 (the
settlement-rounding sd, sqrt(1/12)) instead of SPREAD_FLOOR_C, behind
SPREAD_FLOOR_MEASURED_TIERS_EXEMPT. Ensemble, pooled and fallback tiers
keep the 0.70 floor; the regional ceiling and the MAX_ERROR_RMSE_PER_BUCKET
upper gate are untouched. The corrected-RMSE and error-sd arithmetic is
extracted into pure functions for wave2_falsifier.py. The replay is blind
to this (engine.py allow_measured_spread=False) and the config note says so.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: 2c + 2d + 2e — three only-refuse fixes, three commits
**Files:** Modify `entry_manager.py` (new helpers after `preclamp_size_usd` ~line 402; veto 0a2 at 1036; the mix guard at 846; the stale comment at 826-829; `decide_portfolio_entries` at 1692), `backtest/entry_sim.py` (imports 109-121; gate 2 at 284), `probability_calibration.py` (`calibration_for` 305-317), `config.py` (`WAVE 2` block), `tests/test_parity_entry.py` (the `gate7_kelly_le_zero` case, line 61) / Tests `tests/test_wave2_signed_admission_edge.py`, `tests/test_wave2_calibration_retry.py`, `tests/test_wave2_forecast_outage_refuses.py`
**Interfaces:** Produces: `config.SIGNED_ADMISSION_EDGE: bool = True`, `config.RETRY_FAILED_CALIBRATION_FITS: bool = True`, `config.REFUSE_ON_TOTAL_FORECAST_OUTAGE: bool = True`, `entry_manager.edge_misses_bar(gate_edge: float, min_abs_edge: float) -> bool`, `entry_manager.today_source_mix_for(forecast_sources) -> Optional[frozenset]`. Rule ids unchanged: 2c refuses at `0a2`, 2e refuses at `collection_gate` (through `collection_only_decision`).

**Why three commits, not one.** Each fix has its own flag and its own revert story; a `git revert` of one must not carry the other two. They share a task because each is under twenty lines and the three tests are independent — the sub-steps below are 3a (2c), 3b (2d), 3c (2e), each with its own write-test / fail / implement / pass / commit cycle. Run the full suite once, after 3c.

#### 3a — 2c: veto 0a2 is a signed compare

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave2_signed_admission_edge.py
"""
Wave 2 item 2c. Veto 0a2 compared abs(gate_edge) against MIN_ABS_RAW_EDGE.
gate_edge is ALREADY SIDE-ADJUSTED on both bases -- ev_engine stores
side_model_prob = P(this side wins) and raw_edge = side_model_prob - price,
and the calibrated edge is apply_map(side_model_prob) - price -- so a
NEGATIVE gate_edge means this side is overpriced, and abs() was admitting
it as if it were a disagreement in our favour. On the raw basis it never
mattered live (best_opportunities screens on net_ev first); on the
calibrated basis it does: the isotonic map has a hard zero under ~0.096, so
a model_prob 0.09 at ask 0.05 has raw edge +0.04 (passes) and calibrated
edge -0.05 (abs 0.05 -- passes 0a2, then fails Kelly). The refusal was
right by accident and recorded under the wrong rule_id. Signed compare,
same rule_id, mirrored in entry_sim.
"""
from datetime import date

import pytest

import config
import entry_manager
import probability_calibration as pc
from backtest import entry_sim
from models import EVResult

DAY = date(2026, 9, 21)


def _ev(model_prob, price, calibrated=None, source=pc.NO_TIER, side="YES"):
    return EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=32, side=side,
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.0, fee_rate_pct=0.0,
        net_ev_per_dollar=(model_prob - price) / price,
        calibrated_prob=calibrated, calibration_source=source,
        # A measured source, so the LOW_CONFIDENCE doubling of the bar does
        # not confound the sign test (EVResult defaults to fallback_default).
        spread_source="corrected_error",
    )


@pytest.fixture
def no_live_io(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)
    monkeypatch.setattr(entry_manager, "count_stop_outs_for_bucket", lambda *a, **k: 0)


def _sim(ev):
    return entry_sim.evaluate_entry_sim(
        ev, token_id="tok", open_count_for_bucket=0, opposite_count_for_bucket=0,
        stop_outs_for_bucket=0, depth_usd=100_000.0, slippage_fn=lambda s: 0.0,
        min_net_ev=-9.0, sizing_bankroll=1_000.0,
    )


# The hard-zero case: raw +0.04 clears the 0.03 bar, calibrated is -0.05.
HARD_ZERO = dict(model_prob=0.09, price=0.05, calibrated=0.0, source=pc.STATION_TIER)


def test_a_negative_calibrated_edge_is_refused_at_0a2_not_at_kelly(no_live_io):
    decision = entry_manager.evaluate_entry(_ev(**HARD_ZERO), token_id="tok", min_net_ev=-9.0)
    assert not decision.approved
    assert decision.rule_id == "0a2"
    assert decision.admission_edge == pytest.approx(-0.05)
    assert pc.STATION_TIER in decision.reason


def test_the_replica_refuses_at_the_same_rule(no_live_io):
    assert _sim(_ev(**HARD_ZERO)).rule_id == "0a2"


def test_flag_off_restores_the_abs_compare(no_live_io, monkeypatch):
    """The revert path: abs(-0.05) >= 0.03 passes 0a2 and Kelly refuses."""
    monkeypatch.setattr(config, "SIGNED_ADMISSION_EDGE", False)
    live = entry_manager.evaluate_entry(_ev(**HARD_ZERO), token_id="tok", min_net_ev=-9.0)
    assert live.rule_id == "kelly_nonpositive"
    assert _sim(_ev(**HARD_ZERO)).rule_id == "kelly_nonpositive"


def test_a_positive_edge_that_clears_the_bar_still_trades(no_live_io):
    decision = entry_manager.evaluate_entry(
        _ev(0.46, 0.30, calibrated=0.38, source=pc.STATION_TIER), token_id="tok", min_net_ev=-9.0)
    assert decision.approved


def test_a_small_positive_edge_is_still_refused(no_live_io):
    decision = entry_manager.evaluate_entry(
        _ev(0.432, 0.30, calibrated=0.301, source=pc.STATION_TIER), token_id="tok", min_net_ev=-9.0)
    assert decision.rule_id == "0a2"


def test_the_raw_basis_is_side_adjusted_so_signed_is_right_there_too(no_live_io):
    """A NO with P(NO wins)=0.50 at a NO ask of 0.60 is a -0.10 edge: an
    overpriced side. abs() admitted it; signed refuses it at 0a2."""
    decision = entry_manager.evaluate_entry(
        _ev(0.50, 0.60, side="NO"), token_id="tok", min_net_ev=-9.0)
    assert decision.rule_id == "0a2"
    good = entry_manager.evaluate_entry(_ev(0.70, 0.60, side="NO"), token_id="tok", min_net_ev=-9.0)
    assert good.rule_id != "0a2"


def test_the_helper_is_the_one_both_sides_call():
    import inspect
    assert "edge_misses_bar(" in inspect.getsource(entry_manager.evaluate_entry)
    assert "edge_misses_bar(" in inspect.getsource(entry_sim.evaluate_entry_sim)
    assert entry_manager.edge_misses_bar(-0.05, 0.03) is True
    assert entry_manager.edge_misses_bar(0.05, 0.03) is False
    assert entry_manager.edge_misses_bar(0.02, 0.03) is True
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave2_signed_admission_edge.py -v` / Expected: FAIL with `assert 'kelly_nonpositive' == '0a2'` and `AttributeError: module 'entry_manager' has no attribute 'edge_misses_bar'`

- [ ] **Step 3: Write minimal implementation**

config.py `WAVE 2` block, appended:
```python

# 2c. VETO 0a2 IS A SIGNED COMPARE. Read by entry_manager.edge_misses_bar
# (shared with backtest/entry_sim.py). gate_edge is side-adjusted on both
# bases -- P(this side wins) minus this side's ask -- so a negative value
# is an OVERPRICED side, not a disagreement in our favour, and abs() was
# admitting it. Live it only ever mattered on the calibrated basis (the
# map's hard zero under ~0.096 turns a +0.04 raw edge into -0.05), where
# Kelly then refused it under the wrong rule_id. False restores abs().
SIGNED_ADMISSION_EDGE = True
```

entry_manager.py, after `preclamp_size_usd` (ends ~line 402 `return round(size_usd, 2)`):
```python


def edge_misses_bar(gate_edge: float, min_abs_edge: float) -> bool:
    """
    Veto 0a2's comparison, shared with backtest/entry_sim.py.

    WAVE 2 (2c): SIGNED. gate_edge is already side-adjusted (see
    admission_edge and ev_engine.compute_ev_table's side_model_prob), so a
    negative value is this side being OVERPRICED and must miss the bar.
    abs() let a -0.05 calibrated edge through to Kelly, which refused it
    under kelly_nonpositive; the decision was right and the rule_id was
    wrong. config.SIGNED_ADMISSION_EDGE=False restores abs().
    """
    if config.SIGNED_ADMISSION_EDGE:
        return gate_edge < min_abs_edge
    return abs(gate_edge) < min_abs_edge
```

entry_manager.py veto 0a2 (line 1036) — old:
```python
    gate_edge = deciding["admission_edge"]
    if gate_edge is not None and abs(gate_edge) < min_abs_edge:
```
new:
```python
    gate_edge = deciding["admission_edge"]
    if gate_edge is not None and edge_misses_bar(gate_edge, min_abs_edge):
```
(The reason string keeps its `Absolute edge {gate_edge:+.3f} below required minimum` prefix: `backtest/engine._REASON_PREFIXES` keys the funnel on `"Absolute edge"`, and the signed number is printed with its sign.)

backtest/entry_sim.py imports (line 109-121) — old:
```python
from entry_manager import (
    _calibration_note,
    admission_edge,
```
new:
```python
from entry_manager import (
    _calibration_note,
    admission_edge,
    edge_misses_bar,
```
backtest/entry_sim.py gate 2 (line 284) — old:
```python
    gate_edge = deciding["admission_edge"]
    if gate_edge is not None and abs(gate_edge) < min_abs_edge:
```
new:
```python
    gate_edge = deciding["admission_edge"]
    if gate_edge is not None and edge_misses_bar(gate_edge, min_abs_edge):
```

tests/test_parity_entry.py (line 61) — old:
```python
    ("gate7_kelly_le_zero",         make_ev(0.20, 0.35), 0,    0,    0,    1000.0, 0.01, 0.15),
```
new:
```python
    # WAVE 2 (2c): a -0.15 raw edge now misses the SIGNED 0a2 bar in both
    # implementations; kelly_nonpositive is reachable only with
    # config.SIGNED_ADMISSION_EDGE=False (tests/test_wave2_signed_admission_edge.py).
    ("gate2b_negative_edge_signed_bar", make_ev(0.20, 0.35), 0, 0,    0,    1000.0, 0.01, 0.15),
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave2_signed_admission_edge.py tests/test_parity_entry.py tests/test_calibrated_admission.py tests/test_gate_census.py tests/test_reason_funnel.py -v` / Expected: PASS (7 new + all pre-existing; the gate census still counts 17 sites)
- [ ] **Step 5: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add config.py entry_manager.py backtest/entry_sim.py tests/test_wave2_signed_admission_edge.py tests/test_parity_entry.py
git commit -m "Wave 2 (2c): veto 0a2 compares the signed admission edge

entry_manager.edge_misses_bar (shared with backtest/entry_sim.py) replaces
abs(gate_edge) < min_abs_edge with gate_edge < min_abs_edge behind
SIGNED_ADMISSION_EDGE. gate_edge is side-adjusted on both bases, so a
negative value is an overpriced side; abs() let the calibrated map's hard
zero through to Kelly, which refused under the wrong rule_id. Same rule
id 0a2, same reason prefix, no new gate site.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

#### 3b — 2d: a failed calibration fit is not cached

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave2_calibration_retry.py
"""
Wave 2 item 2d. probability_calibration.calibration_for cached
(None, NO_TIER, 0) on ANY exception, so one transient storage failure at
05:00 left every station uncalibrated -- sized on the raw model_prob with
the double buffer, admitted on the raw edge -- for the rest of the day
(trade-logic review 2026-09-15: 'calibration failures cached all day').
The failure is now logged and returned WITHOUT caching, so the next cycle
retries. Success is still cached per station-day as before.
"""
from datetime import date, timedelta

import config
import probability_calibration as pc

DAY_N = date(2026, 9, 21)


def _rows(n, model_prob, outcome, day, station="WSSS"):
    return [
        {"station_icao": station, "target_date": day, "model_prob": model_prob, "outcome": outcome}
        for _ in range(n)
    ]


def _flaky_cohort(fail_first):
    """A load_cohort that raises on the first `fail_first` calls, then fits."""
    calls = {"n": 0}

    def _load(**kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            raise RuntimeError("storage is briefly gone")
        return _rows(40, 0.60, 1.0, DAY_N - timedelta(days=1)), {}

    return _load, calls


def test_a_failed_fit_is_retried_on_the_next_call(monkeypatch):
    load, calls = _flaky_cohort(fail_first=1)
    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", load)
    pc.clear_cache()

    first = pc.calibration_for("WSSS", DAY_N)
    second = pc.calibration_for("WSSS", DAY_N)

    assert first == (None, pc.NO_TIER, 0)
    assert second[1] == pc.STATION_TIER and second[0] is not None
    assert calls["n"] == 2


def test_a_successful_fit_is_still_cached(monkeypatch):
    load, calls = _flaky_cohort(fail_first=0)
    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", load)
    pc.clear_cache()
    pc.calibration_for("WSSS", DAY_N)
    pc.calibration_for("WSSS", DAY_N)
    assert calls["n"] == 1


def test_the_failure_still_degrades_to_uncalibrated_not_an_exception(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("storage is gone")
    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", _boom)
    pc.clear_cache()
    assert pc.calibration_for("WSSS", DAY_N) == (None, pc.NO_TIER, 0)


def test_flag_off_caches_the_failure_as_before(monkeypatch):
    monkeypatch.setattr(config, "RETRY_FAILED_CALIBRATION_FITS", False)
    load, calls = _flaky_cohort(fail_first=1)
    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", load)
    pc.clear_cache()

    pc.calibration_for("WSSS", DAY_N)
    second = pc.calibration_for("WSSS", DAY_N)

    assert second == (None, pc.NO_TIER, 0)
    assert calls["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave2_calibration_retry.py -v` / Expected: FAIL on `test_a_failed_fit_is_retried_on_the_next_call` with `assert 'uncalibrated' == 'station_isotonic'` and `assert 1 == 2`

- [ ] **Step 3: Write minimal implementation**

config.py `WAVE 2` block, appended:
```python

# 2d. A FAILED CALIBRATION FIT IS NOT CACHED. Read by
# probability_calibration.calibration_for. The map cache is keyed per
# station-day so a success is fitted once; a FAILURE (any exception in the
# cohort read or the fit) used to be cached the same way, leaving every
# station uncalibrated -- raw sizing, raw admission -- until the next day
# after one transient storage error. Now logged and returned uncached, so
# the next cycle retries. False restores the all-day cache of the failure.
RETRY_FAILED_CALIBRATION_FITS = True
```

probability_calibration.py `calibration_for` (line 305-317) — old:
```python
    try:
        if target_day not in _COHORT_CACHE:
            rows, _ = cohort_monitor.load_cohort(until=target_day)
            _COHORT_CACHE[target_day] = rows
        result = fit_for_day(_COHORT_CACHE[target_day], target_day, station_icao)
    except Exception as exc:  # noqa: BLE001 -- must not take the entry path down
        print(
            f"[probability_calibration] could not fit a map for {station_icao} "
            f"on {target_day} ({exc}) -- sizing on the raw model_prob with the "
            f"double buffer, which is the previous behaviour."
        )
        result = (None, NO_TIER, 0)

    _CACHE[key] = result
    return result
```
new:
```python
    try:
        if target_day not in _COHORT_CACHE:
            rows, _ = cohort_monitor.load_cohort(until=target_day)
            _COHORT_CACHE[target_day] = rows
        result = fit_for_day(_COHORT_CACHE[target_day], target_day, station_icao)
    except Exception as exc:  # noqa: BLE001 -- must not take the entry path down
        print(
            f"[probability_calibration] could not fit a map for {station_icao} "
            f"on {target_day} ({exc}) -- sizing on the raw model_prob with the "
            f"double buffer, which is the previous behaviour."
            + (" Not cached; the next cycle retries."
               if config.RETRY_FAILED_CALIBRATION_FITS else "")
        )
        # WAVE 2 (2d): a failure is this CYCLE's answer, not the day's. Cached,
        # one transient storage error uncalibrated every station until
        # midnight (review 2026-09-15). Returned uncached, the next cycle
        # re-reads; a success below is still cached per station-day.
        if config.RETRY_FAILED_CALIBRATION_FITS:
            return (None, NO_TIER, 0)
        result = (None, NO_TIER, 0)

    _CACHE[key] = result
    return result
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave2_calibration_retry.py tests/test_calibrated_sizing.py -v` / Expected: PASS (4 new + all pre-existing; `test_an_unreadable_cohort_falls_back_to_uncalibrated_rather_than_raising` still passes because the return value is unchanged)
- [ ] **Step 5: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add config.py probability_calibration.py tests/test_wave2_calibration_retry.py
git commit -m "Wave 2 (2d): a failed calibration fit is returned uncached so the next cycle retries

calibration_for used to store (None, NO_TIER, 0) in the per-station-day
cache on any exception, so one transient storage error at 05:00 left every
station on raw sizing and raw admission until midnight. Behind
RETRY_FAILED_CALIBRATION_FITS the failure is logged and returned without
caching; a successful fit is still cached once per station-day.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

#### 3c — 2e: a total forecast outage refuses through the mix guard

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave2_forecast_outage_refuses.py
"""
Wave 2 item 2e. decide_portfolio_entries built today_source_mix as
`frozenset(forecast_sources) if forecast_sources else None`, and the mix
guard tested `bias_source_mix and today_source_mix and ...` -- so an EMPTY
source list (every forecast fetch failed this cycle; the estimate fell
through to observed/normal) read as "mix unknown, skip the guard" and the
station kept trading on a central estimate with no forecast term at all
(edge review 2026-09-15: 'total forecast outage fails open').

`[]` and `None` are different facts. None still means "the caller was not
taught to pass a mix" and skips the guard (entry_sim, operator scripts).
[] now becomes frozenset(), the guard compares it against the fitted mix,
and refuses with every fitted source reported missing. rule_id stays
collection_gate, through collection_only_decision.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from clients import market_client
from models import EVResult

FITTED = frozenset({"open_meteo_ecmwf", "open_meteo_gfs"})
DAY = date(2026, 9, 21)


# --- the pure guard ------------------------------------------------------

def _reason(today_mix):
    return entry_manager.collection_only_reason(
        "WSSS", 999, bias_n=99, bias_stderr=0.1, enforce_bias_quality=True,
        bias_source_mix=FITTED, today_source_mix=today_mix,
    )


def test_an_empty_mix_is_refused_by_the_guard():
    reason = _reason(frozenset())
    assert reason is not None
    assert "missing open_meteo_ecmwf, open_meteo_gfs" in reason


def test_an_unknown_mix_still_skips_the_guard():
    assert _reason(None) is None


# --- the translation at the call site ------------------------------------

def test_none_stays_none_and_empty_becomes_the_empty_set():
    assert entry_manager.today_source_mix_for(None) is None
    assert entry_manager.today_source_mix_for([]) == frozenset()
    assert entry_manager.today_source_mix_for(["a", "b"]) == frozenset({"a", "b"})


def test_flag_off_folds_empty_back_to_none(monkeypatch):
    monkeypatch.setattr(config, "REFUSE_ON_TOTAL_FORECAST_OUTAGE", False)
    assert entry_manager.today_source_mix_for([]) is None
    assert entry_manager.today_source_mix_for(["a"]) == frozenset({"a"})


# --- end to end through decide_portfolio_entries -------------------------

def _ev(bucket=32):
    return EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=bucket, side="YES",
        model_prob=0.55, market_price=0.35, raw_edge=0.20, estimated_slippage_pct=0.01,
        fee_rate_pct=0.02, net_ev_per_dollar=0.20 / 0.35 - 0.03, spread_source="corrected_error",
        market_bid=0.33,
    )


@pytest.fixture
def graduated_wsss(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(executor, "EXECUTION_MODE", {icao: "paper" for icao in config.STATIONS})
    monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.1, 20, 0.1))
    monkeypatch.setattr(entry_manager, "resolution_obs_count",
                        lambda icao: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY)
    monkeypatch.setattr(entry_manager, "forecast_bias_source_mix", lambda icao: FITTED)
    monkeypatch.setattr(entry_manager, "station_error_width_ratio", lambda icao: None)
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    entry_manager._collection_only_logged.clear()
    return {32: {"yes_token_id": "y32", "no_token_id": "n32"}}


def test_a_total_outage_refuses_every_candidate_at_collection_gate(graduated_wsss):
    decisions = entry_manager.decide_portfolio_entries(
        [_ev()], graduated_wsss, min_net_ev=0.15, forecast_sources=[],
    )
    assert len(decisions) == 1
    assert not decisions[0].approved
    assert decisions[0].rule_id == "collection_gate"
    assert "missing open_meteo_ecmwf, open_meteo_gfs" in decisions[0].reason


def test_a_caller_that_passes_none_is_not_gated_on_the_mix(graduated_wsss):
    decisions = entry_manager.decide_portfolio_entries(
        [_ev()], graduated_wsss, min_net_ev=0.15, forecast_sources=None,
    )
    assert decisions[0].rule_id != "collection_gate"


def test_the_fitted_mix_still_passes(graduated_wsss):
    decisions = entry_manager.decide_portfolio_entries(
        [_ev()], graduated_wsss, min_net_ev=0.15, forecast_sources=sorted(FITTED),
    )
    assert decisions[0].rule_id != "collection_gate"
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave2_forecast_outage_refuses.py -v` / Expected: FAIL with `assert None is not None` (the guard) and `AttributeError: module 'entry_manager' has no attribute 'today_source_mix_for'`

- [ ] **Step 3: Write minimal implementation**

config.py `WAVE 2` block, appended:
```python

# 2e. A TOTAL FORECAST OUTAGE REFUSES. Read by entry_manager.today_source_
# mix_for. An empty forecast source list -- every fetch failed, the
# estimate fell through to observed/normal -- used to be folded to None
# ("mix unknown"), which the mix guard skips, so the station traded a
# central estimate with no forecast term. [] now reaches the guard as
# frozenset() and is refused (rule_id collection_gate) with every fitted
# source reported missing. None keeps meaning "not taught to pass a mix".
# False folds [] back to None.
REFUSE_ON_TOTAL_FORECAST_OUTAGE = True
```

entry_manager.py, after `edge_misses_bar` (Task 3a):
```python


def today_source_mix_for(forecast_sources) -> Optional[frozenset]:
    """
    The mix decide_portfolio_entries hands the guard, from the blend's
    source list this cycle.

    None -> None: the caller was not taught to pass a mix, and the guard
    does not run (backtest/entry_sim.py, operator scripts). [] -> frozenset()
    (WAVE 2, 2e): every source failed this cycle, which is a mix the fitted
    bias was NOT measured on, and the guard must refuse it rather than skip.
    config.REFUSE_ON_TOTAL_FORECAST_OUTAGE=False folds [] back to None.
    """
    if forecast_sources is None:
        return None
    if not forecast_sources and not config.REFUSE_ON_TOTAL_FORECAST_OUTAGE:
        return None
    return frozenset(forecast_sources)
```

entry_manager.py mix guard (line 846) — old:
```python
    if bias_source_mix and today_source_mix and bias_source_mix != today_source_mix:
```
new:
```python
    # `is not None`, not truthiness (WAVE 2, 2e): an EMPTY today's mix is a
    # known mix -- no source at all -- and must be compared, not skipped.
    if (
        bias_source_mix is not None
        and today_source_mix is not None
        and bias_source_mix != today_source_mix
    ):
```
and the comment block above it (line 826-829) — old:
```python
    # storage.forecast_error_samples() fits it on "the per-date forecast
    # mean [that] mirrors blend_central_estimate's own forecast term", so it
    # is exactly right while the mix is stable -- and it is: measured over
```
new:
```python
    # storage.forecast_error_samples() fits it on the per-date MORNING
    # forecast mean (the 04:00-08:00 fetch window, WAVE 2 2a), so it is
    # exactly right while the mix is stable -- and it is: measured over
```
and the paragraph `# FAILS OPEN when either mix is unknown, ...` (line 838-844) gains, after `on a check they never opted into.`:
```python
    # An EMPTY mix is not unknown: decide_portfolio_entries passes
    # frozenset() for a cycle with no forecast source at all (2e), and that
    # is refused below with every fitted source reported missing.
```

entry_manager.py `decide_portfolio_entries` (line 1690-1692) — old:
```python
        # None when the caller has not been taught to pass it -- the guard
        # then does not run. See collection_only_reason().
        today_source_mix=frozenset(forecast_sources) if forecast_sources else None,
```
new:
```python
        # None when the caller has not been taught to pass it -- the guard
        # then does not run; frozenset() for a total outage, which it refuses.
        # See today_source_mix_for() and collection_only_reason().
        today_source_mix=today_source_mix_for(forecast_sources),
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave2_forecast_outage_refuses.py tests/test_bias_mix_guard.py tests/test_wave1_entry_decisions_recorded.py tests/test_wave1_shadow_pass.py -v` / Expected: PASS (7 new + all pre-existing; `test_unknown_mixes_do_not_block_trading` still passes because both `None` checks are preserved)
- [ ] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1800
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add config.py entry_manager.py tests/test_wave2_forecast_outage_refuses.py
git commit -m "Wave 2 (2e): a total forecast outage reaches the mix guard as frozenset() and is refused

decide_portfolio_entries folded an empty source list to None, and the
mix guard tested truthiness, so a cycle with no forecast at all skipped
the guard and traded a central estimate with no forecast term. New
today_source_mix_for keeps None as 'not taught to pass a mix' and passes
[] through as frozenset() behind REFUSE_ON_TOTAL_FORECAST_OUTAGE; the
guard now tests `is not None`. Refusal is rule_id collection_gate with
every fitted source reported missing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: 2f — entry_sim haircut and exit-fee parity with live
**Files:** Modify `backtest/entry_sim.py` (imports 109-121; signature 154-165; haircut block 366-380; `net_ev_at_size` 445-446), `tests/test_parity_entry.py` (`make_ev` 30-39; `CASES` 51-67) / Test `tests/test_wave2_entry_sim_parity.py`
**Interfaces:** Consumes: `entry_manager._book_has_stop(station_icao, execution_mode)`, `probability_calibration.haircut_applies`, `EVResult.expected_exit_fee_pct` / Produces: `entry_sim.REPLAY_BOOK_MODE = "paper"`, `evaluate_entry_sim(..., execution_mode: str = REPLAY_BOOK_MODE)`. No flag: backtest-only (spec table).

**What is wrong today, measured on HEAD.** With every station pinned to `paper` (as the parity test does) live computes `_has_stop = _book_has_stop(icao, mode)` → `False` (`"paper"` is in `HOLD_TO_SETTLEMENT_MODES`) and passes it to `gap_risk_haircut(..., has_stop=False)`; entry_sim passes `has_stop=True` and omits the kwarg (entry_sim.py:366-380). Both currently produce the same number only because `config.SIZE_STOPLESS_BOOKS_ON_PURE_KELLY` is `False`; flip it and live sizes 76.92 where the sim sizes 55.70 on the parity fixture. Live also subtracts `expected_exit_fee_pct` from `net_ev_at_size` (entry_manager.py:1266-1271); the sim does not (entry_sim.py:446), so an EV row carrying 0.04 of exit fee prices 0.5014 live and 0.5414 in the replay. The engine's own rows carry `expected_exit_fee_pct=0.0` (`Position(is_paper=True)`, mode `paper`, no exit fee — engine.py:1030-1075 never sets it), so no replay NUMBER changes today; what changes is that the replica can no longer drift from live on either term.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave2_entry_sim_parity.py
"""
Wave 2 item 2f. backtest/entry_sim.evaluate_entry_sim is the pure replica
of entry_manager.evaluate_entry and had two arithmetic gaps against live:

  * the gap-risk haircut was called with has_stop=True (the default) while
    live threads _book_has_stop(station, execution_mode) -- False on the
    paper book since HOLD_TO_SETTLEMENT_MODES -- so the two disagree the
    moment SIZE_STOPLESS_BOOKS_ON_PURE_KELLY is True;
  * net_ev_at_size omitted the expected_exit_fee_pct term live subtracts.

The replay emulates the PAPER book (engine builds every Position with
is_paper=True, execution_mode 'paper'), so evaluate_entry_sim now takes
execution_mode=REPLAY_BOOK_MODE and derives has_stop through the SAME
helper live uses, and subtracts the same exit-fee term.
"""
from dataclasses import asdict
from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from backtest import entry_sim
from clients import market_client
from models import EVResult


def _ev(exit_fee=0.0):
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 8, 10), bucket_c=32, side="YES",
        model_prob=0.55, market_price=0.35, raw_edge=0.20, estimated_slippage_pct=0.01,
        fee_rate_pct=0.02, net_ev_per_dollar=0.20 / 0.35 - 0.03,
        spread_source="corrected_error", market_bid=0.33, expected_exit_fee_pct=exit_fee,
    )


@pytest.fixture
def paper_book(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {icao: "paper" for icao in config.STATIONS})
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)


def _both(ev):
    live = entry_manager.evaluate_entry(ev, "TOKEN-1", min_net_ev=0.15)
    sim = entry_sim.evaluate_entry_sim(
        ev=ev, token_id="TOKEN-1", open_count_for_bucket=0, opposite_count_for_bucket=0,
        stop_outs_for_bucket=0, depth_usd=1000.0, slippage_fn=lambda s: 0.01,
        min_net_ev=0.15, sizing_bankroll=config.BANKROLL_USD,
    )
    return live, sim


def _diffs(live, sim):
    l, s = asdict(live), asdict(sim)
    return {k: (v, s[k]) for k, v in l.items() if s[k] != v}


def test_the_replay_book_is_paper():
    assert entry_sim.REPLAY_BOOK_MODE == "paper"
    assert "paper" in config.HOLD_TO_SETTLEMENT_MODES


def test_parity_holds_when_stopless_books_size_on_pure_kelly(paper_book, monkeypatch):
    """The haircut gap. Live retires the haircut on a stopless calibrated
    book only; with the pure-Kelly flag on it retires it outright, and the
    sim must follow through the same helper."""
    monkeypatch.setattr(config, "SIZE_STOPLESS_BOOKS_ON_PURE_KELLY", True)
    live, sim = _both(_ev())
    assert live.approved and sim.approved
    assert not _diffs(live, sim)


def test_parity_holds_with_an_exit_fee_on_the_row(paper_book):
    live, sim = _both(_ev(exit_fee=0.04))
    assert live.net_ev_at_size == pytest.approx((0.20 / 0.35) - 0.01 - 0.02 - 0.04)
    assert not _diffs(live, sim)


def test_the_sim_derives_has_stop_through_the_live_helper():
    import inspect
    src = inspect.getsource(entry_sim.evaluate_entry_sim)
    assert "_book_has_stop(" in src
    assert "has_stop=True" not in src


def test_a_book_with_a_stop_keeps_the_haircut_in_the_sim(paper_book, monkeypatch):
    """execution_mode is a real parameter: 'simulation' is not in
    HOLD_TO_SETTLEMENT_MODES, so the haircut applies and the size is smaller."""
    monkeypatch.setattr(config, "SIZE_STOPLESS_BOOKS_ON_PURE_KELLY", True)
    stopless = entry_sim.evaluate_entry_sim(
        ev=_ev(), token_id="T", open_count_for_bucket=0, opposite_count_for_bucket=0,
        stop_outs_for_bucket=0, depth_usd=1000.0, slippage_fn=lambda s: 0.01,
        min_net_ev=0.15, sizing_bankroll=config.BANKROLL_USD, execution_mode="paper",
    )
    stopped = entry_sim.evaluate_entry_sim(
        ev=_ev(), token_id="T", open_count_for_bucket=0, opposite_count_for_bucket=0,
        stop_outs_for_bucket=0, depth_usd=1000.0, slippage_fn=lambda s: 0.01,
        min_net_ev=0.15, sizing_bankroll=config.BANKROLL_USD, execution_mode="simulation",
    )
    assert stopped.recommended_size_usd < stopless.recommended_size_usd
```

tests/test_parity_entry.py `make_ev` (line 30-39) — old:
```python
def make_ev(model_prob, price, station="WSSS", bucket=32, side="YES", fee=0.0,
            spread_source="ensemble"):
    raw_edge = None if price is None else model_prob - price
    return EVResult(
        station_icao=station, target_date=date(2026, 8, 10), bucket_c=bucket, side=side,
        model_prob=model_prob, market_price=price, raw_edge=raw_edge,
        estimated_slippage_pct=0.01, fee_rate_pct=fee,
        net_ev_per_dollar=None if price in (None, 0) else raw_edge / price - 0.01 - fee,
        spread_source=spread_source,
    )
```
new:
```python
def make_ev(model_prob, price, station="WSSS", bucket=32, side="YES", fee=0.0,
            spread_source="ensemble", exit_fee=0.0):
    raw_edge = None if price is None else model_prob - price
    return EVResult(
        station_icao=station, target_date=date(2026, 8, 10), bucket_c=bucket, side=side,
        model_prob=model_prob, market_price=price, raw_edge=raw_edge,
        estimated_slippage_pct=0.01, fee_rate_pct=fee,
        net_ev_per_dollar=None if price in (None, 0) else raw_edge / price - 0.01 - fee - exit_fee,
        spread_source=spread_source, expected_exit_fee_pct=exit_fee,
    )
```
and `CASES` gains, after the `gate12_approved_exploratory` line:
```python
    # WAVE 2 (2f): the exit-fee term live subtracts from net_ev_at_size.
    ("gate12_approved_with_exit_fee", make_ev(0.55, 0.35, exit_fee=0.04), 0, 0, 0, 1000.0, 0.01, 0.15),
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave2_entry_sim_parity.py tests/test_parity_entry.py -v` / Expected: FAIL with `AttributeError: module 'backtest.entry_sim' has no attribute 'REPLAY_BOOK_MODE'`, `EntryDecision field mismatch: {'net_ev_at_size': (0.5014..., 0.5414...), 'reason': (...)}` on the new parity case, and `assert not {'recommended_size_usd': (76.92, 55.7), ...}` on the pure-Kelly test

- [ ] **Step 3: Write minimal implementation**

backtest/entry_sim.py imports (line 109-121) — old:
```python
from entry_manager import (
    _calibration_note,
    admission_edge,
    edge_misses_bar,
```
new:
```python
from entry_manager import (
    _book_has_stop,
    _calibration_note,
    admission_edge,
    edge_misses_bar,
```

backtest/entry_sim.py, directly above `def _maturity_for(` (line 143):
```python
# WAVE 2 (2f). The book the replay emulates. backtest/engine.py builds every
# replayed Position with is_paper=True and the Position default
# execution_mode 'paper', so the replica sizes and prices as the PAPER book
# does: entry_manager._book_has_stop("paper") is False under
# config.HOLD_TO_SETTLEMENT_MODES, and the paper EV table carries no exit
# fee. Threaded through the SAME helper live reads rather than restated, so
# the two cannot disagree about which books have a stop.
REPLAY_BOOK_MODE = "paper"


```

backtest/entry_sim.py signature (line 154-165) — old:
```python
def evaluate_entry_sim(
    ev: EVResult,
    token_id: str,
    open_count_for_bucket: Optional[int],
    opposite_count_for_bucket: Optional[int],
    stop_outs_for_bucket: Optional[int],
    depth_usd: Optional[float],
    slippage_fn: Callable[[float], float],
    min_net_ev: float,
    sizing_bankroll: float,
    station_maturity: Optional[str] = None,
) -> EntryDecision:
```
new:
```python
def evaluate_entry_sim(
    ev: EVResult,
    token_id: str,
    open_count_for_bucket: Optional[int],
    opposite_count_for_bucket: Optional[int],
    stop_outs_for_bucket: Optional[int],
    depth_usd: Optional[float],
    slippage_fn: Callable[[float], float],
    min_net_ev: float,
    sizing_bankroll: float,
    station_maturity: Optional[str] = None,
    execution_mode: str = REPLAY_BOOK_MODE,
) -> EntryDecision:
```
and its docstring's injected-inputs list gains, after the `sizing_bankroll` entry:
```python
      execution_mode         <- executor.EXECUTION_MODE[station]. The book
                                being replayed; REPLAY_BOOK_MODE ("paper")
                                is what the engine's positions are. Feeds
                                entry_manager._book_has_stop exactly as
                                live's evaluate_entry does (WAVE 2, 2f).
```

backtest/entry_sim.py haircut block (line 366-380) — old:
```python
    # Gap-risk haircut, same position in the chain as live (after the hard
    # ceiling and maturity, before the live fixed size). Imported from
    # entry_manager, not restated: a replay that sized without the haircut
    # would report a strategy nobody is running.
    #
    # The P3-6 condition is mirrored rather than assumed away. It is a NO-OP
    # here today -- replayed EVResults carry no calibration, and an
    # uncalibrated row keeps the haircut by design -- but stating the rule is
    # what stops the replica silently diverging the first time the backtest is
    # given a calibrated book. has_stop=True matches this function's existing
    # call, which has never passed the flag.
    if probability_calibration.haircut_applies(
        has_stop=True,
        calibration_source=getattr(
            ev, "calibration_source", probability_calibration.NO_TIER
        ),
    ):
        size_usd *= gap_risk_haircut(ev.market_price, station_icao, ev.market_bid)
```
new:
```python
    # Gap-risk haircut, same position in the chain as live (after the hard
    # ceiling and maturity, before the live fixed size). Imported from
    # entry_manager, not restated: a replay that sized without the haircut
    # would report a strategy nobody is running.
    #
    # The P3-6 condition is mirrored rather than assumed away. It is a NO-OP
    # here today -- replayed EVResults carry no calibration, and an
    # uncalibrated row keeps the haircut by design -- but stating the rule is
    # what stops the replica silently diverging the first time the backtest is
    # given a calibrated book.
    #
    # WAVE 2 (2f): has_stop comes from the SAME helper live reads, for the
    # book the replay emulates (REPLAY_BOOK_MODE). It used to be hard-coded
    # True, which agreed with live only while
    # config.SIZE_STOPLESS_BOOKS_ON_PURE_KELLY stayed False.
    _has_stop = _book_has_stop(station_icao, execution_mode)
    if probability_calibration.haircut_applies(
        has_stop=_has_stop,
        calibration_source=getattr(
            ev, "calibration_source", probability_calibration.NO_TIER
        ),
    ):
        size_usd *= gap_risk_haircut(
            ev.market_price, station_icao, ev.market_bid, has_stop=_has_stop,
        )
```

backtest/entry_sim.py `net_ev_at_size` (line 443-446) — old:
```python
    # Re-check slippage and net EV at the ACTUAL recommended size, not the
    # flat screening size ev_engine used -- identical arithmetic to live.
    slippage_at_size = slippage_fn(depth_capped_usd)
    net_ev_at_size = (ev.raw_edge / ev.market_price) - slippage_at_size - ev.fee_rate_pct
```
new:
```python
    # Re-check slippage and net EV at the ACTUAL recommended size, not the
    # flat screening size ev_engine used -- identical arithmetic to live,
    # INCLUDING the exit-leg fee live subtracts (WAVE 2, 2f; 0.0 on the
    # paper rows the engine builds, so no replay number moves today).
    slippage_at_size = slippage_fn(depth_capped_usd)
    net_ev_at_size = (
        (ev.raw_edge / ev.market_price)
        - slippage_at_size
        - ev.fee_rate_pct
        - getattr(ev, "expected_exit_fee_pct", 0.0)
    )
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave2_entry_sim_parity.py tests/test_parity_entry.py tests/test_haircut_on_a_stopless_book.py tests/test_gap_risk_sizing.py tests/test_backtest_stack.py tests/test_determinism.py -v` / Expected: PASS (5 new + 17 parity cases + all pre-existing; the determinism/stack replays are unchanged because engine rows carry `expected_exit_fee_pct=0.0` and `SIZE_STOPLESS_BOOKS_ON_PURE_KELLY` is False)
- [ ] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1806
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add backtest/entry_sim.py tests/test_wave2_entry_sim_parity.py tests/test_parity_entry.py
git commit -m "Wave 2 (2f): entry_sim threads the replay book's stop state into the haircut and charges the exit fee

evaluate_entry_sim takes execution_mode=REPLAY_BOOK_MODE ('paper', the
book the engine's positions are) and derives has_stop through
entry_manager._book_has_stop exactly as live does, instead of the
hard-coded True that agreed with live only while
SIZE_STOPLESS_BOOKS_ON_PURE_KELLY stayed False. net_ev_at_size now
subtracts expected_exit_fee_pct as live does. No replay number moves
today (engine rows carry no exit fee); the parity test now covers both.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `regimes.py` and the three reports split at `config.REGIME_BOUNDARIES`
**Files:** Create `regimes.py`; modify `config.py` (`WAVE 2` block: `REGIME_BOUNDARIES`), `cohort_monitor.py` (imports 84-94; `main` argparse 728-740; the all-time print at 795-798), `calibration_panel.py` (imports 62-66; `cohort_card` 525-532), `promotion_dossier.py` (imports 119-128; `_print_calibration` 454-517; `print_dossier` 561-580; argparse 586-600) / Tests `tests/test_wave2_regimes.py`, `tests/test_wave2_regime_reports.py`
**Interfaces:** Produces: `config.REGIME_BOUNDARIES: tuple = ()` (stamped in Task 7), `regimes.boundaries() -> tuple[date, ...]`, `regimes.segment_labels(bounds) -> list[str]` (`["all"]` with no boundaries; else `pre-<b0>`, `<bi>..<b(i+1) - 1 day>`, `from-<bn>`), `regimes.segment_index(day, bounds) -> int`, `regimes.regime_segments(rows, key=lambda r: r["target_date"], bounds=None) -> list[tuple[str, list]]` (every segment present, empty ones included), `cohort_monitor.main(["--no-regime-split"])`, `calibration_panel.render_regime_split_html(rows) -> str`, `calibration_panel.cohort_card(as_of=None, stations=None, regime_split=True)`, `promotion_dossier._print_calibration_stats(stats)`, `promotion_dossier._print_calibration(station_icao, since, until, regime_split=True)`, `promotion_dossier.print_dossier(station_icao, since=None, until=None, regime_split=True)`, `--no-regime-split` on the dossier CLI.

**Insertion points, chosen for the smallest diff.** Each report already has one place where "the rows" become "the totals": `cohort_monitor.main` prints `windows(rows)["all_time"]` (795); `calibration_panel.cohort_card` renders `windows(rows)` (525-532); `promotion_dossier._print_calibration` prints `live_calibration(entries)` (454-459). The split wraps exactly that call with `regimes.regime_segments(...)`. With `REGIME_BOUNDARIES = ()` every report's output is byte-identical to today (one segment labelled `all` is rendered through the pre-existing pooled path), which is what keeps every existing report test green until Task 7 stamps the date. The cohort monitor's trailing windows and kill criterion stay POOLED and say so: they are time-bounded already, and the kill criterion is a pre-committed rule whose window must not be redefined mid-flight.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wave2_regimes.py
"""
regimes.py: split any row list at config.REGIME_BOUNDARIES so a report can
print each side of a wave's deploy date separately (spec Principle 3).
"""
from datetime import date

import pytest

import config
import regimes

B1 = date(2026, 9, 21)
B2 = date(2026, 10, 5)


def _rows(*days):
    return [{"target_date": d, "i": i} for i, d in enumerate(days)]


def test_no_boundaries_is_one_segment_called_all(monkeypatch):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ())
    rows = _rows(date(2026, 9, 1), date(2026, 9, 30))
    assert regimes.boundaries() == ()
    assert regimes.segment_labels(()) == ["all"]
    assert regimes.regime_segments(rows) == [("all", rows)]


def test_one_boundary_splits_before_and_from(monkeypatch):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ("2026-09-21",))
    rows = _rows(date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22))
    assert regimes.boundaries() == (B1,)
    assert regimes.regime_segments(rows) == [
        ("pre-2026-09-21", rows[:1]),
        ("from-2026-09-21", rows[1:]),
    ]


def test_the_boundary_day_belongs_to_the_new_regime():
    assert regimes.segment_index(B1, (B1,)) == 1
    assert regimes.segment_index(date(2026, 9, 20), (B1,)) == 0


def test_two_boundaries_make_three_segments_and_keep_empty_ones():
    bounds = (B1, B2)
    assert regimes.segment_labels(bounds) == [
        "pre-2026-09-21", "2026-09-21..2026-10-04", "from-2026-10-05",
    ]
    rows = _rows(date(2026, 9, 25))
    assert regimes.regime_segments(rows, bounds=bounds) == [
        ("pre-2026-09-21", []),
        ("2026-09-21..2026-10-04", rows),
        ("from-2026-10-05", []),
    ]


def test_the_key_is_pluggable():
    class Entry:
        def __init__(self, d):
            self.target_date = d
    entries = [Entry(date(2026, 9, 20)), Entry(B1)]
    out = regimes.regime_segments(entries, key=lambda e: e.target_date, bounds=(B1,))
    assert [len(seg) for _, seg in out] == [1, 1]


def test_unsorted_or_duplicate_boundaries_are_refused(monkeypatch):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ("2026-10-05", "2026-09-21"))
    with pytest.raises(ValueError):
        regimes.boundaries()
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ("2026-09-21", "2026-09-21"))
    with pytest.raises(ValueError):
        regimes.boundaries()
```

```python
# tests/test_wave2_regime_reports.py
"""
cohort_monitor, calibration_panel and promotion_dossier each report each
side of config.REGIME_BOUNDARIES separately by default, and pool on
request (--no-regime-split / regime_split=False). With no boundaries the
output is the pre-Wave-2 output.
"""
from datetime import date

import pytest

import calibration_panel
import cohort_monitor
import config
import promotion_dossier
import storage
from models import Position

BOUNDARY = "2026-09-21"
BEFORE = date(2026, 9, 18)
AFTER = date(2026, 9, 23)


def _position(day, bucket_c, pid, station="WSSS", model_prob=0.55):
    return Position(
        position_id=pid, station_icao=station, target_date=day, bucket_c=bucket_c, side="YES",
        entry_price=0.30, size_usd=10.0, entry_time=f"{day}T22:00:00+00:00", status="open",
        high_water_mark=0.30, is_paper=True, execution_mode="paper", model_prob=model_prob,
    )


@pytest.fixture
def two_regimes(tmp_path, monkeypatch):
    """One settled winner before the boundary, one settled loser after it."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", (BOUNDARY,))
    storage._connect().close()
    for day, bucket, pid in ((BEFORE, 32, "w"), (AFTER, 31, "l")):
        storage.open_position(_position(day, bucket, pid))
        storage.close_position(pid, 0.30, f"{day}T10:00:00+00:00", "closed_resolution", "test")
        storage.save_settled_bucket("WSSS", day, 32, 30, 34, "test")


# --- cohort_monitor ----------------------------------------------------------

def test_the_monitor_prints_one_block_per_regime_by_default(two_regimes, capsys):
    cohort_monitor.main(["--station", "WSSS", "--as-of", "2026-09-24"])
    out = capsys.readouterr().out
    assert "regime pre-2026-09-21" in out
    assert "regime from-2026-09-21" in out
    assert "\nall time\n" not in out
    assert "POOLED across regimes" in out


def test_the_monitor_pools_on_request(two_regimes, capsys):
    cohort_monitor.main(["--station", "WSSS", "--as-of", "2026-09-24", "--no-regime-split"])
    out = capsys.readouterr().out
    assert "\nall time\n" in out
    assert "regime pre-" not in out


def test_the_monitor_is_unchanged_with_no_boundaries(two_regimes, monkeypatch, capsys):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ())
    cohort_monitor.main(["--station", "WSSS", "--as-of", "2026-09-24"])
    out = capsys.readouterr().out
    assert "\nall time\n" in out
    assert "regime" not in out


# --- calibration_panel -------------------------------------------------------

def test_the_cohort_card_renders_a_table_per_regime(two_regimes):
    html = calibration_panel.cohort_card(as_of=date(2026, 9, 24), stations=["WSSS"])
    assert "pre-2026-09-21" in html and "from-2026-09-21" in html
    assert "held" in html


def test_the_cohort_card_pools_on_request(two_regimes):
    html = calibration_panel.cohort_card(
        as_of=date(2026, 9, 24), stations=["WSSS"], regime_split=False)
    assert "pre-2026-09-21" not in html


def test_the_split_renders_nothing_without_boundaries(two_regimes, monkeypatch):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ())
    rows, _ = cohort_monitor.load_cohort(stations=["WSSS"])
    assert calibration_panel.render_regime_split_html(rows) == ""


def test_an_empty_regime_prints_no_number(two_regimes, monkeypatch):
    """Reporting rule 1: no empty book may print a number."""
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ("2026-09-21", "2026-10-05"))
    rows, _ = cohort_monitor.load_cohort(stations=["WSSS"])
    html = calibration_panel.render_regime_split_html(rows)
    assert "from-2026-10-05" in html
    assert "no rows" in html


# --- promotion_dossier -------------------------------------------------------

def test_the_dossier_scores_each_regime_separately(two_regimes, capsys):
    promotion_dossier._print_calibration("WSSS", None, None)
    out = capsys.readouterr().out
    assert "BEATS_MARKET -- regime pre-2026-09-21" in out
    assert "BEATS_MARKET -- regime from-2026-09-21" in out
    assert out.count("scored entries:") == 2


def test_the_dossier_pools_on_request(two_regimes, capsys):
    promotion_dossier._print_calibration("WSSS", None, None, regime_split=False)
    out = capsys.readouterr().out
    assert "BEATS_MARKET -- measured on the live book" in out
    assert out.count("scored entries:") == 1


def test_the_dossier_is_unchanged_with_no_boundaries(two_regimes, monkeypatch, capsys):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ())
    promotion_dossier._print_calibration("WSSS", None, None)
    out = capsys.readouterr().out
    assert "BEATS_MARKET -- measured on the live book" in out
    assert "regime" not in out
```

- [ ] **Step 2: Run tests to verify they fail** — Run: `pytest tests/test_wave2_regimes.py tests/test_wave2_regime_reports.py -v` / Expected: FAIL with `ModuleNotFoundError: No module named 'regimes'` and `AttributeError: module 'config' has no attribute 'REGIME_BOUNDARIES'`

- [ ] **Step 3: Write minimal implementation**

config.py `WAVE 2` block, appended:
```python

# REGIME BOUNDARIES. ISO dates, strictly increasing. Each is the day a wave
# first ran on the box, and every cohort report (cohort_monitor,
# calibration_panel's cohort card, promotion_dossier's BEATS_MARKET block)
# prints each side of every boundary separately by default -- pass
# --no-regime-split / regime_split=False for the pooled figure. The
# boundary day belongs to the NEW regime. See regimes.py.
#
# () on the branch; the Wave 2 deploy date is stamped here in the final
# commit before merge, and moved by a follow-up commit if the deploy slips.
REGIME_BOUNDARIES: tuple = ()
```

regimes.py (new):
```python
"""
regimes.py -- split rows at config.REGIME_BOUNDARIES.

A wave lands on one day (spec Principle 1), and the reports must not pool
rows across it: the pre-registered reads are "did the number move at the
boundary", which a pooled total cannot show. ONE implementation here so
cohort_monitor, calibration_panel and promotion_dossier segment the same
way; each keeps its own arithmetic and only changes WHICH rows it feeds it.

Segments are labelled by their edges -- "pre-2026-09-21", "2026-09-21..
2026-10-04", "from-2026-10-05" -- and the boundary day belongs to the NEW
regime, because the deploy runs before that day's entry window. Every
segment is returned, empty ones included, so a report shows the layout
even where a regime has no rows yet.

Pure: reads config, touches nothing else.
"""
from datetime import date, timedelta
from typing import Callable, List, Optional, Sequence, Tuple

import config


def boundaries() -> Tuple[date, ...]:
    """config.REGIME_BOUNDARIES as dates. Refuses an unsorted or repeated
    list rather than silently producing an empty middle segment."""
    out = tuple(date.fromisoformat(b) for b in config.REGIME_BOUNDARIES)
    if list(out) != sorted(set(out)):
        raise ValueError(
            f"config.REGIME_BOUNDARIES must be strictly increasing ISO dates, got {config.REGIME_BOUNDARIES!r}"
        )
    return out


def segment_labels(bounds: Sequence[date]) -> List[str]:
    if not bounds:
        return ["all"]
    labels = [f"pre-{bounds[0].isoformat()}"]
    for lo, hi in zip(bounds, bounds[1:]):
        labels.append(f"{lo.isoformat()}..{(hi - timedelta(days=1)).isoformat()}")
    labels.append(f"from-{bounds[-1].isoformat()}")
    return labels


def segment_index(day: date, bounds: Sequence[date]) -> int:
    """0 before the first boundary, k after the k-th (boundary day included)."""
    return sum(1 for b in bounds if day >= b)


def regime_segments(
    rows: Sequence,
    key: Optional[Callable] = None,
    bounds: Optional[Sequence[date]] = None,
) -> List[Tuple[str, list]]:
    """[(label, rows_in_segment), ...] in chronological order. `key` reads
    the row's date (default: row["target_date"]); `bounds` defaults to
    boundaries() and is explicit for tests."""
    if key is None:
        key = lambda r: r["target_date"]  # noqa: E731
    if bounds is None:
        bounds = boundaries()
    labels = segment_labels(bounds)
    buckets: List[list] = [[] for _ in labels]
    for row in rows:
        buckets[segment_index(key(row), bounds)].append(row)
    return list(zip(labels, buckets))
```

cohort_monitor.py imports (line 90-94) — old:
```python
import config
import ev_engine
import storage
```
new:
```python
import config
import ev_engine
import regimes
import storage
```

cohort_monitor.py `main` argparse (line 737-740) — old:
```python
    parser.add_argument("--reproduce", action="store_true",
                        help=f"score the published window {PUBLISHED_WINDOW[0]}..{PUBLISHED_WINDOW[1]} "
                             "and check it against the published totals")
    args = parser.parse_args(argv)
```
new:
```python
    parser.add_argument("--reproduce", action="store_true",
                        help=f"score the published window {PUBLISHED_WINDOW[0]}..{PUBLISHED_WINDOW[1]} "
                             "and check it against the published totals")
    parser.add_argument("--no-regime-split", action="store_true",
                        help="one pooled all-time block instead of one block per side of "
                             "config.REGIME_BOUNDARIES (the trailing windows are always pooled)")
    args = parser.parse_args(argv)
```

cohort_monitor.py `main` all-time print (line 795-798) — old:
```python
    window_summaries = windows(rows, as_of=as_of)
    _print_summary("all time", window_summaries["all_time"])
    for days in WINDOW_DAYS:
        _print_summary(f"trailing {days} days", window_summaries[f"trailing_{days}d"])
```
new:
```python
    window_summaries = windows(rows, as_of=as_of)
    # WAVE 2: a wave lands on one day, and the all-time figure must not pool
    # across it. The trailing windows and the kill criterion below stay
    # pooled: they are time-bounded already, and the kill criterion is a
    # pre-committed rule whose window is not redefined mid-flight.
    bounds = () if args.no_regime_split else regimes.boundaries()
    if not bounds:
        _print_summary("all time", window_summaries["all_time"])
    else:
        for label, segment in regimes.regime_segments(rows, bounds=bounds):
            _print_summary(f"regime {label}", summarize(segment))
        print("\n(trailing windows and the kill criterion are POOLED across regimes; "
              "--no-regime-split prints the pooled all-time block)")
    for days in WINDOW_DAYS:
        _print_summary(f"trailing {days} days", window_summaries[f"trailing_{days}d"])
```

calibration_panel.py imports (line 62-66) — old:
```python
import bucket_axis
import calibration
import cohort_monitor
import config
import entry_manager
import promotion_dossier
```
new:
```python
import bucket_axis
import calibration
import cohort_monitor
import config
import entry_manager
import promotion_dossier
import regimes
```

calibration_panel.py `cohort_card` (line 525-532) — old:
```python
def cohort_card(as_of=None, stations=None) -> str:
    """
    render_cohort_html() against the stored book -- the I/O half, so a
    generator needs one call and no knowledge of cohort_monitor's shape.
    """
    rows, _ = cohort_monitor.load_cohort(stations=stations)
    window_summaries = cohort_monitor.windows(rows, as_of=as_of)
    return render_cohort_html(window_summaries, cohort_monitor.kill_criterion(window_summaries))
```
new:
```python
def render_regime_split_html(rows) -> str:
    """
    One small table per side of config.REGIME_BOUNDARIES: n, station-days,
    staked, held and as-traded return with the held CI. Empty string when
    there are no boundaries, so a page without a wave renders as before.
    An empty regime prints "no rows", never a number (reporting rule 1).
    """
    bounds = regimes.boundaries()
    if not bounds:
        return ""
    parts = ["<div class='sub'>Per regime (config.REGIME_BOUNDARIES) &mdash; "
             "the boundary day belongs to the new regime.</div>",
             "<table><thead><tr><th>Regime</th><th>Rows</th><th>Days</th>"
             "<th>Staked</th><th>Held</th><th>As traded</th><th>Held CI</th>"
             "</tr></thead><tbody>"]
    for label, segment in regimes.regime_segments(rows, bounds=bounds):
        summary = cohort_monitor.summarize(segment)
        if summary is None:
            parts.append(
                f"<tr><td class='mono'>{html.escape(label)}</td>"
                f"<td colspan='6' class='sub'>no rows</td></tr>"
            )
            continue
        held = summary["scenarios"]["held"]["return_pct"]
        traded = summary["scenarios"]["as_traded"]["return_pct"]
        ci = summary["ci"]["held_return_pct"]
        ci_text = _EM_DASH if not ci else f"[{ci[0] * 100:+.1f}%, {ci[1] * 100:+.1f}%]"
        parts.append(
            "<tr>"
            f"<td class='mono'>{html.escape(label)}</td>"
            f"<td class='mono num'>{summary['n']}</td>"
            f"<td class='mono num'>{summary['n_days']}</td>"
            f"<td class='mono num'>{summary['staked_usd']:,.2f}</td>"
            f"<td class='mono num'>{_EM_DASH if held is None else f'{held * 100:+.1f}%'}</td>"
            f"<td class='mono num'>{_EM_DASH if traded is None else f'{traded * 100:+.1f}%'}</td>"
            f"<td class='mono num'>{ci_text}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def cohort_card(as_of=None, stations=None, regime_split: bool = True) -> str:
    """
    render_cohort_html() against the stored book -- the I/O half, so a
    generator needs one call and no knowledge of cohort_monitor's shape.

    WAVE 2: prefixed with render_regime_split_html() by default, so the
    dashboards show each side of a wave's deploy date; regime_split=False
    is the pooled card alone.
    """
    rows, _ = cohort_monitor.load_cohort(stations=stations)
    window_summaries = cohort_monitor.windows(rows, as_of=as_of)
    card = render_cohort_html(window_summaries, cohort_monitor.kill_criterion(window_summaries))
    if regime_split:
        card = render_regime_split_html(rows) + card
    return card
```

promotion_dossier.py imports (line 124-128) — old:
```python
import config
import paper_trading_report
import storage
```
new:
```python
import config
import paper_trading_report
import regimes
import storage
```

promotion_dossier.py `_print_calibration` (line 454-517) — old (head of the function):
```python
def _print_calibration(station_icao: str, since, until) -> None:
    _rule("BEATS_MARKET -- measured on the live book, not the backtest")

    entries, skipped = scorable_entries(station_icao, since=since, until=until)
    stats = live_calibration(entries)

    if stats is None:
        print("  nothing scorable.")
    else:
```
new:
```python
def _print_calibration_stats(stats: Optional[dict]) -> None:
    """One BEATS_MARKET block for one set of scored entries -- the body
    _print_calibration used to inline, lifted out so it can run once per
    regime (WAVE 2)."""
    if stats is None:
        print("  nothing scorable.")
    else:
```
(the `else:` body through the `print("  The GATE reads the latest backtest summary, ...")` call is unchanged and stays inside `_print_calibration_stats`), followed by:
```python


def _print_calibration(station_icao: str, since, until, regime_split: bool = True) -> None:
    entries, skipped = scorable_entries(station_icao, since=since, until=until)

    # WAVE 2: one block per side of config.REGIME_BOUNDARIES by default. The
    # Brier gap is exactly the kind of number that must not be pooled across
    # a deploy that changed what the model prices on.
    regime_bounds = regimes.boundaries() if regime_split else ()
    if regime_bounds:
        for label, segment in regimes.regime_segments(
            entries, key=lambda e: e["position"].target_date, bounds=regime_bounds,
        ):
            _rule(f"BEATS_MARKET -- regime {label}")
            _print_calibration_stats(live_calibration(segment))
    else:
        _rule("BEATS_MARKET -- measured on the live book, not the backtest")
        _print_calibration_stats(live_calibration(entries))

    if skipped:
```
(the `if skipped:` block and the `drift = bounds_drift(...)` block that follow are unchanged. The variable is named `regime_bounds`, not `bounds`, because the drift block below rebinds `bounds` in `for bounds, (first, last, n) in sorted(drift["windows"].items()):`.)

promotion_dossier.py `print_dossier` (line 561-580) — old:
```python
def print_dossier(station_icao: str, since=None, until=None) -> None:
```
new:
```python
def print_dossier(station_icao: str, since=None, until=None, regime_split: bool = True) -> None:
```
and the call inside it — old `    _print_calibration(station_icao, since, until)` → new `    _print_calibration(station_icao, since, until, regime_split=regime_split)`.

promotion_dossier.py argparse (line 596-600) — old:
```python
    parser.add_argument("--until", default=None,
                        help="Ignore target dates after this (YYYY-MM-DD).")
    args = parser.parse_args()

    print_dossier(args.station, _parse_date(args.since), _parse_date(args.until))
```
new:
```python
    parser.add_argument("--until", default=None,
                        help="Ignore target dates after this (YYYY-MM-DD).")
    parser.add_argument("--no-regime-split", action="store_true",
                        help="One pooled BEATS_MARKET block instead of one per side of "
                             "config.REGIME_BOUNDARIES.")
    args = parser.parse_args()

    print_dossier(args.station, _parse_date(args.since), _parse_date(args.until),
                  regime_split=not args.no_regime_split)
```

- [ ] **Step 4: Run tests to verify they pass** — Run: `pytest tests/test_wave2_regimes.py tests/test_wave2_regime_reports.py tests/test_cohort_monitor.py tests/test_calibration_panel.py tests/test_promotion_dossier.py -v` / Expected: PASS (6 + 10 new + all pre-existing; with `REGIME_BOUNDARIES = ()` every existing report test sees the pre-Wave-2 output)
- [ ] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1822
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add regimes.py config.py cohort_monitor.py calibration_panel.py promotion_dossier.py tests/test_wave2_regimes.py tests/test_wave2_regime_reports.py
git commit -m "Wave 2: regimes.py splits every cohort report at config.REGIME_BOUNDARIES

New regimes.regime_segments(rows, key, bounds) labels each side of every
boundary (the boundary day belongs to the new regime, empty segments
kept). cohort_monitor prints one all-time block per regime by default
(--no-regime-split pools; trailing windows and the kill criterion stay
pooled and say so), calibration_panel.cohort_card prefixes a per-regime
table (regime_split=False omits it), promotion_dossier prints one
BEATS_MARKET block per regime (--no-regime-split pools).
REGIME_BOUNDARIES is () until the deploy-day stamp, so every report is
unchanged today.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: `wave2_falsifier.py` — the pre-registered reads and the stop condition, read-only
**Files:** Create `wave2_falsifier.py` / Test `tests/test_wave2_falsifier.py`
**Interfaces:** Consumes: `storage.forecast_rows_in_error_sample` (Task 1), `calibration.corrected_error_rmse_from_dated`, `calibration.measured_error_spread_from_errors`, `calibration.priced_measured_spread` (Task 2), `cohort_monitor.cohort_rows`, `cohort_monitor._return_pct`, `cohort_monitor._bootstrap_ci`, `cohort_monitor._clusters`, `storage._row_to_position` / Produces: `wave2_falsifier.run(db_path, boundary_iso) -> dict` with keys `error_sd_by_station`, `priced_spread_by_station`, `held_before`, `held_after`, `held_difference_ci`, `stop_verdict`, `refusals`; `main(["--boundary", "2026-09-21", "--db", path])`

**Design.** Same shape as `wave1_falsifier.py`: `_ro_connect` (file URI + `mode=ro`), a `REQUIRED_SCHEMA` guard that names what is missing and exits 1, `run()` returning a dict, `_print()` rendering it. It never imports through `storage._connect()` (which issues DDL); every row comes from its own read-only SELECTs, and every statistic is a PURE function the production path also calls — `forecast_rows_in_error_sample` for (i), the Task 2 extractions for (ii), `cohort_monitor.cohort_rows` + `_return_pct` + `_bootstrap_ci` for (iii). The only arithmetic that lives here is the bootstrap of the DIFFERENCE between two cohorts (the stop condition), which resamples station-days of each side independently with `cohort_monitor`'s seed and iteration count. (iv) counts `entry_decisions` rows. Calibration-failure RETRIES (2d) are not counted: they are journal lines, not rows, and adding a counter to the daemon for a one-off read is not worth a schema. About 230 lines.

**The stop condition, precisely.** Spec: "if the 14-day held-to-settlement return on post-boundary paper rows is worse than the pre-boundary 14 days by more than the day-clustered CI, flip 2a and 2b off together." Read as: `after − before` on `_return_pct(rows, "held")`, station-day-clustered bootstrap CI of that difference; **STOP** when the CI's upper bound is below zero; **holding** otherwise; **NO VERDICT** when either side has no stake. "Paper rows" = `is_paper = 1 AND execution_mode = 'paper'` (simulation and manual_review rows are paper-booked but not the paper strategy). The before window is `[boundary − 14d, boundary − 1d]` and the after window `[boundary, boundary + 13d]` by `target_date`, through `cohort_rows(since=, until=)` (both inclusive).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave2_falsifier.py
"""The Wave 2 reads (i)-(iv) and the stop condition, on a fixture DB, through a read-only connection."""
import sqlite3
from datetime import date, timedelta

import pytest

import config
import storage
import wave2_falsifier
from models import EntryDecision, ObservedReading, PointForecast, Position

BOUNDARY = "2026-09-21"
B = date(2026, 9, 21)


def _forecast(day, temp, local_hour):
    fetched = config.local_day_bounds_utc("WSSS", day)[0] + timedelta(hours=local_hour)
    return PointForecast(station_icao="WSSS", source="open_meteo_ecmwf", target_date=day,
                         max_temp_c=temp, fetched_at=fetched.isoformat())


def _paper(pid, day, bucket):
    return Position(
        position_id=pid, station_icao="WSSS", target_date=day, bucket_c=bucket, side="YES",
        entry_price=0.30, size_usd=10.0, entry_time=f"{day}T22:00:00+00:00", status="open",
        high_water_mark=0.30, is_paper=True, execution_mode="paper",
    )


def _decision(rule_id, admission_edge):
    return EntryDecision(
        station_icao="WSSS", target_date=B, bucket_c=32, side="YES",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=0.0,
        available_depth_usd=None, slippage_at_size_pct=None, net_ev_at_size=None,
        approved=False, reason="r", station_maturity="mature", entry_price=0.05,
        rule_id=rule_id, admission_edge=admission_edge,
    )


def _seed_paper(days, bucket, prefix):
    for i, day in enumerate(days):
        pid = f"{prefix}{i}"
        storage.open_position(_paper(pid, day, bucket))
        storage.close_position(pid, 0.30, f"{day}T10:00:00+00:00", "closed_resolution", "t")
        storage.save_settled_bucket("WSSS", day, 32, 30, 34, "test")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.sqlite3")
    monkeypatch.setattr(config, "DB_PATH", path)
    storage._connect().close()
    # (i)/(ii): six dates, truth 32.0. Morning (05:00) forecasts err -1,0,+1,
    # -1,0,+1 -> sd sqrt(4/5) = 0.894; a 14:00 row at 32.0 on every day
    # halves the all-day error -> sd sqrt(1/5) = 0.447.
    source = config.get_station("WSSS").resolution_grade_source
    for i, err in enumerate([-1.0, 0.0, 1.0, -1.0, 0.0, 1.0]):
        day = date(2026, 9, 10) + timedelta(days=i)
        storage.save_observation(ObservedReading(station_icao="WSSS", target_date=day,
                                                 max_temp_c=32.0, source=source))
        storage.save_forecast(_forecast(day, 32.0 + err, 5))
        storage.save_forecast(_forecast(day, 32.0, 14))
    # (iv)
    storage.record_entry_decisions([_decision("0a2", -0.05), _decision("0a2", 0.01),
                                    _decision("kelly_nonpositive", -0.02)],
                                   book="paper", cycle_ts="2026-09-22T05:00:10+00:00", config_sha="s")
    storage.record_entry_decisions([_decision("kelly_nonpositive", -0.02)],
                                   book="paper", cycle_ts="2026-09-15T05:00:10+00:00", config_sha="s")
    return path


@pytest.fixture
def worse_after(db):
    """Before: three winning station-days (bucket 32 settles). After: three
    losing ones (bucket 31). Held return +233% -> -100%: STOP."""
    _seed_paper([B - timedelta(days=d) for d in (3, 2, 1)], 32, "w")
    _seed_paper([B + timedelta(days=d) for d in (0, 1, 2)], 31, "l")
    return db


@pytest.fixture
def same_after(db):
    _seed_paper([B - timedelta(days=d) for d in (3, 2, 1)], 32, "w")
    _seed_paper([B + timedelta(days=d) for d in (0, 1, 2)], 32, "x")
    return db


def test_read_i_morning_vs_all_day_sd(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    sd = out["error_sd_by_station"]["WSSS"]
    assert sd["morning"]["n"] == 6 and sd["all_day"]["n"] == 6
    assert sd["morning"]["sd"] == pytest.approx(0.8944, abs=1e-3)
    assert sd["all_day"]["sd"] == pytest.approx(0.4472, abs=1e-3)


def test_read_ii_priced_spread_and_the_floor_pin(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    ps = out["priced_spread_by_station"]["WSSS"]
    assert ps["source"] == "measured_error"          # 6 pairs: under the 15-residual RMSE floor
    assert ps["measured"] == pytest.approx(0.8944, abs=1e-3)
    assert ps["priced"] == 0.89                      # measured tier: no 0.70 floor (2b)
    assert ps["pinned_to_floor"] is False


def test_read_ii_reports_unmeasured_stations_as_none(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    assert out["priced_spread_by_station"]["EDDM"]["source"] is None
    assert out["priced_spread_by_station"]["EDDM"]["pinned_to_floor"] is None


def test_read_iii_stop_fires_when_after_is_worse_beyond_the_ci(worse_after):
    out = wave2_falsifier.run(worse_after, BOUNDARY)
    assert out["held_before"]["n"] == 3 and out["held_after"]["n"] == 3
    assert out["held_before"]["return_pct"] == pytest.approx(0.70 / 0.30)
    assert out["held_after"]["return_pct"] == pytest.approx(-1.0)
    lo, hi = out["held_difference_ci"]
    assert hi < 0
    assert out["stop_verdict"].startswith("STOP")


def test_read_iii_holds_when_nothing_moved(same_after):
    out = wave2_falsifier.run(same_after, BOUNDARY)
    assert out["held_difference_ci"] == (0.0, 0.0)
    assert out["stop_verdict"] == "holding"


def test_read_iii_has_no_verdict_without_rows_on_both_sides(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    assert out["held_before"]["n"] == 0
    assert out["stop_verdict"] == "NO VERDICT"


def test_read_iv_counts_negative_edge_refusals_by_rule(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    assert out["refusals"]["0a2_negative_after"] == 1
    assert out["refusals"]["0a2_negative_before"] == 0
    assert out["refusals"]["kelly_negative_after"] == 1
    assert out["refusals"]["kelly_negative_before"] == 1


def test_the_connection_is_read_only(db, monkeypatch):
    calls = []
    real = sqlite3.connect

    def _spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    wave2_falsifier.run(db, BOUNDARY)
    assert calls, "expected at least one sqlite3.connect call"
    for a, kw in calls:
        assert kw.get("uri") is True and "mode=ro" in a[0]


def test_a_pre_wave1_schema_is_refused_with_one_line(tmp_path, capsys):
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE positions (station_icao TEXT)")
    con.commit()
    con.close()
    assert wave2_falsifier.main(["--boundary", BOUNDARY, "--db", str(path)]) == 1
    assert "schema predates" in capsys.readouterr().out


def test_main_prints_the_verdict(worse_after, capsys):
    assert wave2_falsifier.main(["--boundary", BOUNDARY, "--db", worse_after]) == 0
    out = capsys.readouterr().out
    assert "STOP CONDITION" in out and "STOP" in out
    assert "WSSS" in out
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave2_falsifier.py -v` / Expected: FAIL with `ModuleNotFoundError: No module named 'wave2_falsifier'`

- [ ] **Step 3: Write minimal implementation**

```python
# wave2_falsifier.py
"""
wave2_falsifier.py -- the Wave 2 pre-registered reads and the stop condition
(spec: "Wave 2 -- correct the inputs, one day"), run read-only at the
boundary and again at boundary + 14 days, appended to memory.

    python wave2_falsifier.py --boundary 2026-09-21

Opens the database READ-ONLY (mode=ro), never through storage._connect()
(which issues DDL). Every statistic is a PURE function the production path
also runs, applied to rows this script read itself:

  (i)   per station, the sd of the per-date forecast error under the Wave 2
        fetch window (local 04:00-08:00) and under the pre-Wave-2 local-day
        window, from the SAME stored rows -- storage.forecast_rows_in_
        error_sample with window_enabled=True/False. The read: the two
        converge over 14 days as the afternoon rows age out of the sample.
  (ii)  per station, the spread the measured tier prices right now
        (calibration.corrected_error_rmse_from_dated, else
        measured_error_spread_from_errors, then priced_measured_spread) and
        whether it equals SPREAD_FLOOR_C exactly. The read: EDDM / RKPK /
        WSSS must NOT print 0.700.
  (iii) held-to-settlement return on PAPER rows (is_paper=1 AND
        execution_mode='paper'), 14 days before vs 14 days after the
        boundary, each with cohort_monitor's station-day-clustered CI, and
        the station-day-clustered CI of the DIFFERENCE (after - before).
        STOP CONDITION: the difference's CI lies entirely below zero ->
        flip ERROR_SAMPLE_FETCH_WINDOW_ENABLED and
        SPREAD_FLOOR_MEASURED_TIERS_EXEMPT off TOGETHER.
  (iv)  entry_decisions: 0a2 refusals with a negative admission_edge (2c
        firing) and kelly_nonpositive rows with a negative admission_edge
        (must be 0 after the boundary), 14 days either side. Calibration-
        failure RETRIES (2d) are journal lines, not rows, and are not
        counted.

A DB that predates Wave 1 (no entry_decisions, or a missing column) is
refused with one operator-facing line and exit 1.
"""
import argparse
import random
import sqlite3
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import calibration
import cohort_monitor
import config
import storage

WINDOW_DAYS = 14

REQUIRED_SCHEMA = {
    "forecasts": ("station_icao", "source", "target_date", "max_temp_c", "fetched_at"),
    "observations": ("station_icao", "target_date", "max_temp_c", "source"),
    "positions": ("station_icao", "target_date", "status", "is_paper", "execution_mode"),
    "settled_buckets": ("station_icao", "target_date", "bucket_c", "bucket_min_c",
                        "bucket_max_c", "bucket_unit", "bucket_step"),
    "entry_decisions": ("cycle_ts", "book", "rule_id", "admission_edge"),
}


class SchemaTooOldError(RuntimeError):
    """Raised when the target DB predates a migration this script needs."""


def _check_schema(con: sqlite3.Connection) -> None:
    for table, cols in REQUIRED_SCHEMA.items():
        table_info = con.execute(f"PRAGMA table_info({table})").fetchall()
        if not table_info:
            raise SchemaTooOldError(
                f"schema predates Wave 1: table {table!r} is missing -- run after the migration")
        present = {row[1] for row in table_info}
        missing = [c for c in cols if c not in present]
        if missing:
            raise SchemaTooOldError(
                f"schema predates Wave 1: column(s) {missing} missing from {table!r} -- "
                f"run after the migration")


def _ro_connect(db_path: str) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


# --- (i) and (ii): the error sample ----------------------------------------

def _forecast_rows(con, icao: str) -> List[Tuple[str, str, float, str]]:
    """forecast_rows_with_fetch_time()'s rows and exclusion rule, read-only."""
    excluded = set(config.FORECAST_SOURCES_EXCLUDED_BY_STATION.get(icao, ()))
    rows = con.execute(
        "SELECT target_date, fetched_at, max_temp_c, source FROM forecasts "
        "WHERE station_icao = ? AND max_temp_c IS NOT NULL", (icao,),
    ).fetchall()
    return [(str(r[0]), str(r[1]), float(r[2]), str(r[3])) for r in rows if str(r[3]) not in excluded]


def _truth(con, icao: str) -> Dict[str, float]:
    source = config.get_station(icao).resolution_grade_source
    rows = con.execute(
        "SELECT target_date, max_temp_c FROM observations "
        "WHERE station_icao = ? AND source = ? AND max_temp_c IS NOT NULL", (icao, source),
    ).fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


def _dated_errors(icao: str, rows, truth, window_enabled: Optional[bool]) -> List[Tuple[date, float]]:
    """forecast_error_samples_dated()'s pairing over rows already in hand."""
    by_date = storage.forecast_rows_in_error_sample(icao, rows, window_enabled=window_enabled)
    return sorted(
        (d, sum(t for _, t in fc) / len(fc) - truth[d.isoformat()])
        for d, fc in by_date.items() if d.isoformat() in truth
    )


def error_sd_by_window(con, icao: str) -> dict:
    rows, truth = _forecast_rows(con, icao), _truth(con, icao)
    out = {}
    for label, enabled in (("morning", True), ("all_day", False)):
        errors = [e for _, e in _dated_errors(icao, rows, truth, enabled)]
        out[label] = {"n": len(errors), "sd": statistics.stdev(errors) if len(errors) >= 2 else None}
    return out


def priced_spread(con, icao: str) -> dict:
    """What the measured tier would price for this station NOW, under the
    production flag, and whether it sits on SPREAD_FLOOR_C exactly."""
    dated = _dated_errors(icao, _forecast_rows(con, icao), _truth(con, icao), None)
    value, n = calibration.corrected_error_rmse_from_dated(dated)
    source = "corrected_error"
    if value is None:
        value, n = calibration.measured_error_spread_from_errors([e for _, e in dated])
        source = "measured_error"
    if value is None:
        return {"source": None, "measured": None, "priced": None, "n": n, "pinned_to_floor": None}
    priced = calibration.priced_measured_spread(value, icao)
    return {"source": source, "measured": value, "priced": priced, "n": n,
            "pinned_to_floor": priced == config.SPREAD_FLOOR_C}


# --- (iii): held-to-settlement, 14 days either side ------------------------

def _paper_rows(con, boundary: date) -> Tuple[List[dict], List[dict]]:
    before, after = [], []
    since, until = boundary - timedelta(days=WINDOW_DAYS), boundary + timedelta(days=WINDOW_DAYS - 1)
    for icao in sorted(config.STATIONS):
        positions = [storage._row_to_position(r) for r in con.execute(
            "SELECT * FROM positions WHERE station_icao = ? AND status != 'open' "
            "AND is_paper = 1 AND execution_mode = 'paper'", (icao,))]
        settled = {
            date.fromisoformat(str(r[0])): (int(r[1]), int(r[2]), int(r[3]), str(r[4]), int(r[5]))
            for r in con.execute(
                "SELECT target_date, bucket_c, bucket_min_c, bucket_max_c, bucket_unit, bucket_step "
                "FROM settled_buckets WHERE station_icao = ?", (icao,))
        }
        rows, _ = cohort_monitor.cohort_rows(positions, settled, since=since, until=until)
        before.extend(r for r in rows if r["target_date"] < boundary)
        after.extend(r for r in rows if r["target_date"] >= boundary)
    return before, after


def _held(rows: List[dict]) -> dict:
    return {
        "n": len(rows),
        "n_days": len(cohort_monitor._clusters(rows)),
        "return_pct": cohort_monitor._return_pct(rows, "held"),
        "ci": cohort_monitor._bootstrap_ci(rows, lambda r: cohort_monitor._return_pct(r, "held")),
    }


def held_difference_ci(before: List[dict], after: List[dict]) -> Optional[Tuple[float, float]]:
    """Station-day-clustered bootstrap CI of held(after) - held(before),
    resampling each side's clusters independently. Same seed, iterations
    and alpha as cohort_monitor._bootstrap_ci."""
    if not before or not after:
        return None
    b_clusters = list(cohort_monitor._clusters(before).values())
    a_clusters = list(cohort_monitor._clusters(after).values())
    rng = random.Random(cohort_monitor.BOOTSTRAP_SEED)
    draws: List[float] = []
    for _ in range(cohort_monitor.BOOTSTRAP_ITERATIONS):
        b = [row for _ in b_clusters for row in rng.choice(b_clusters)]
        a = [row for _ in a_clusters for row in rng.choice(a_clusters)]
        rb = cohort_monitor._return_pct(b, "held")
        ra = cohort_monitor._return_pct(a, "held")
        if rb is not None and ra is not None:
            draws.append(ra - rb)
    if not draws:
        return None
    draws.sort()
    lo = int(cohort_monitor.CI_ALPHA / 2 * (len(draws) - 1))
    hi = int((1 - cohort_monitor.CI_ALPHA / 2) * (len(draws) - 1))
    return draws[lo], draws[hi]


def stop_verdict(before: dict, after: dict, diff_ci) -> str:
    if before["return_pct"] is None or after["return_pct"] is None or diff_ci is None:
        return "NO VERDICT"
    if diff_ci[1] < 0:
        return ("STOP -- post-boundary held return is worse by more than the day-clustered CI: "
                "set ERROR_SAMPLE_FETCH_WINDOW_ENABLED and SPREAD_FLOOR_MEASURED_TIERS_EXEMPT "
                "to False TOGETHER")
    return "holding"


# --- (iv): refusal rows ----------------------------------------------------

def refusal_counts(con, boundary: date) -> dict:
    lo = (boundary - timedelta(days=WINDOW_DAYS)).isoformat()
    mid = boundary.isoformat()
    hi = (boundary + timedelta(days=WINDOW_DAYS)).isoformat()

    def count(rule_id: str, start: str, end: str) -> int:
        return con.execute(
            "SELECT COUNT(*) FROM entry_decisions WHERE rule_id = ? AND admission_edge < 0 "
            "AND book != 'paper_shadow' AND cycle_ts >= ? AND cycle_ts < ?",
            (rule_id, start, end),
        ).fetchone()[0]

    return {
        "0a2_negative_before": count("0a2", lo, mid),
        "0a2_negative_after": count("0a2", mid, hi),
        "kelly_negative_before": count("kelly_nonpositive", lo, mid),
        "kelly_negative_after": count("kelly_nonpositive", mid, hi),
    }


# --- assembly ----------------------------------------------------------------

def run(db_path: str, boundary_iso: str) -> dict:
    boundary = date.fromisoformat(boundary_iso)
    con = _ro_connect(db_path)
    try:
        _check_schema(con)
        stations = sorted(config.STATIONS)
        error_sd = {icao: error_sd_by_window(con, icao) for icao in stations}
        priced = {icao: priced_spread(con, icao) for icao in stations}
        before_rows, after_rows = _paper_rows(con, boundary)
        refusals = refusal_counts(con, boundary)
    finally:
        con.close()
    before, after = _held(before_rows), _held(after_rows)
    diff_ci = held_difference_ci(before_rows, after_rows)
    return {
        "boundary": boundary_iso,
        "error_sd_by_station": error_sd,
        "priced_spread_by_station": priced,
        "held_before": before,
        "held_after": after,
        "held_difference_ci": diff_ci,
        "stop_verdict": stop_verdict(before, after, diff_ci),
        "refusals": refusals,
    }


def _fmt(value, spec: str = ".3f") -> str:
    return "--" if value is None else format(value, spec)


def _print(out: dict) -> None:
    print(f"Wave 2 falsifier -- boundary {out['boundary']}, windows +/-{WINDOW_DAYS}d\n")
    print("(i) forecast error sd, morning window (04-08 local) vs all-day, per station:")
    for icao, sd in out["error_sd_by_station"].items():
        if sd["morning"]["n"] < 2 and sd["all_day"]["n"] < 2:
            continue
        print(f"     {icao}: morning {_fmt(sd['morning']['sd'])} (n={sd['morning']['n']})  "
              f"all-day {_fmt(sd['all_day']['sd'])} (n={sd['all_day']['n']})")
    print(f"\n(ii) priced measured spread per station (pinned == SPREAD_FLOOR_C {config.SPREAD_FLOOR_C:.3f}):")
    for icao, ps in out["priced_spread_by_station"].items():
        if ps["source"] is None:
            continue
        flag = "  <- PINNED TO THE FLOOR" if ps["pinned_to_floor"] else ""
        print(f"     {icao}: {ps['source']} measured {ps['measured']:.3f} priced {ps['priced']:.2f} "
              f"(n={ps['n']}){flag}")
    print("\n(iii) held-to-settlement on paper rows:")
    for label in ("held_before", "held_after"):
        h = out[label]
        ci = "" if not h["ci"] else f"  CI [{h['ci'][0]:+.3f}, {h['ci'][1]:+.3f}]"
        print(f"     {label:<11} n={h['n']} over {h['n_days']} station-days  "
              f"return {_fmt(h['return_pct'], '+.3f')}{ci}")
    d = out["held_difference_ci"]
    print(f"     after - before CI: {'--' if d is None else f'[{d[0]:+.3f}, {d[1]:+.3f}]'}")
    print(f"     STOP CONDITION: {out['stop_verdict']}")
    r = out["refusals"]
    print("\n(iv) entry_decisions with admission_edge < 0 (excl. paper_shadow):")
    print(f"     0a2               before {r['0a2_negative_before']}  after {r['0a2_negative_after']}")
    print(f"     kelly_nonpositive before {r['kelly_negative_before']}  after {r['kelly_negative_after']}  -> after must be 0 (2c)")
    print("     calibration-failure retries (2d): not counted -- journal lines, not rows")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--boundary", required=True, help="the Wave 2 deploy date, YYYY-MM-DD (config.REGIME_BOUNDARIES[-1])")
    parser.add_argument("--db", default=str(config.DB_PATH), help="database path (opened read-only)")
    args = parser.parse_args(argv)
    try:
        out = run(args.db, args.boundary)
    except SchemaTooOldError as exc:
        print(str(exc))
        return 1
    _print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave2_falsifier.py -v` / Expected: PASS (11 passed)
- [ ] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1833
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add wave2_falsifier.py tests/test_wave2_falsifier.py
git commit -m "Wave 2: wave2_falsifier.py prints the pre-registered reads and the stop condition read-only

Opens the DB mode=ro and composes the pure helpers the production path
runs: forecast_rows_in_error_sample with the window on/off (read i), the
Task 2 spread extractions and priced_measured_spread with the
SPREAD_FLOOR_C pin flag (read ii), cohort_monitor.cohort_rows and its
clustered bootstrap for held-to-settlement on paper rows 14 days either
side of the boundary plus a clustered CI of the difference and the STOP
verdict (read iii), and entry_decisions counts of negative-edge 0a2 and
kelly_nonpositive refusals (read iv). Calibration-failure retries are
journal lines and are not counted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Deploy-day stamp — `REGIME_BOUNDARIES`, merge, deploy, falsifier
**Files:** Modify `config.py` (`REGIME_BOUNDARIES` in the `WAVE 2` block) / Test `tests/test_wave2_regime_boundary_stamp.py`
**Interfaces:** Produces: `config.REGIME_BOUNDARIES = ("<deploy date>",)`

**Mechanics.** The branch carries `REGIME_BOUNDARIES: tuple = ()` through Tasks 1–6, so every report and every existing test sees pooled output until the stamp. On deploy day, BEFORE merging, the tuple is edited to the actual date, the stamp test below is run (it refuses a future date and a malformed one), the edit is committed as the branch's last commit, and the branch is merged with `--no-ff`. The deploy then happens the same day. If it cannot (a failed deploy, a slip past midnight UTC), a one-line follow-up commit moves the date — the boundary is the first day the box ran the new code, nothing else.

- [ ] **Step 1: Write the test (passes vacuously on `()`, fails on a bad stamp)**

```python
# tests/test_wave2_regime_boundary_stamp.py
"""
config.REGIME_BOUNDARIES is the list of days a wave first ran on the box.
Every entry must be an ISO date that has already happened (a boundary in
the future would split every report at a day with no rows on one side),
and the list must be strictly increasing. Vacuous while the tuple is
empty on the branch; load-bearing from the deploy-day stamp on.
"""
from datetime import date

import config
import regimes


def test_every_boundary_is_an_iso_date_not_in_the_future():
    today = config._now_utc().date()
    for raw in config.REGIME_BOUNDARIES:
        parsed = date.fromisoformat(raw)      # raises on a malformed stamp
        assert parsed <= today, f"boundary {raw} is in the future"
        assert raw == parsed.isoformat(), f"boundary {raw} is not canonical ISO"


def test_boundaries_are_strictly_increasing():
    parsed = [date.fromisoformat(b) for b in config.REGIME_BOUNDARIES]
    assert parsed == sorted(set(parsed))
    assert regimes.boundaries() == tuple(parsed)


def test_the_tuple_is_a_tuple_of_strings():
    assert isinstance(config.REGIME_BOUNDARIES, tuple)
    assert all(isinstance(b, str) for b in config.REGIME_BOUNDARIES)
```

- [ ] **Step 2: Run the test on the branch** — Run: `pytest tests/test_wave2_regime_boundary_stamp.py -v` / Expected: PASS (3 passed, vacuously on `()`)
- [ ] **Step 3: Commit the test**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add tests/test_wave2_regime_boundary_stamp.py
git commit -m "Wave 2: pin REGIME_BOUNDARIES to canonical, past, strictly increasing ISO dates

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: ON DEPLOY DAY, stamp the date** — config.py `WAVE 2` block — old:
```python
REGIME_BOUNDARIES: tuple = ()
```
new (substitute the real date; `2026-09-21` is the target):
```python
REGIME_BOUNDARIES: tuple = ("2026-09-21",)
```
Then run: `pytest tests/test_wave2_regime_boundary_stamp.py tests/test_wave2_regime_reports.py tests/test_wave2_regimes.py -v` / Expected: PASS. Then the full suite: `pytest -q` / Expected: all pass, count >= 1836 (the report tests monkeypatch the tuple, so nothing else moves).

- [ ] **Step 5: Final commit on the branch**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add config.py
git commit -m "Wave 2: stamp REGIME_BOUNDARIES with the deploy date 2026-09-21

Every cohort report now splits at this day by default. Move it with a
follow-up commit if the deploy slips: the boundary is the first day the
box ran the Wave 2 code.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Merge and push**

```bash
cd "C:/Users/user/Downloads/weather-forecast"
git checkout main
git merge --no-ff feat/wave2-correct-the-inputs -m "Merge feat/wave2-correct-the-inputs: correct the inputs, one day (2a-2f, regime split, wave2_falsifier)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
cd weather-forecast && pytest -q && cd ..
git push origin main
```

- [ ] **Step 7: Deploy (outside 05:00–08:00 SGT; no schema change, so the plain path)** — per `docs/superpowers/plans/...ec2...` / memory `ec2-deployment.md`: `~/deploy.sh` on the box (the `deploy_daemon.sh` path, no backup-and-stop), then `git log -1` on the box == `main`'s merge commit. Record the UTC time. If the box's first entry cycle on the new code lands on a different UTC date than the stamp, commit the correction (Step 5's message with the new date) and redeploy.

- [ ] **Step 8: Same-day falsifier and memory note** — on the box, read-only: `python wave2_falsifier.py --boundary 2026-09-21` (defaults `--db` to the production path; opens `mode=ro`). Expected on day 0: (i) morning and all-day sd both printed per station, differing; (ii) EDDM / RKPK / WSSS `priced` not equal to `0.700` and no `PINNED TO THE FLOOR` on a station with `corrected_error`; (iii) `held_after n=0` and `NO VERDICT`; (iv) `0a2 after` growing from the first cycle, `kelly_nonpositive after` = 0. Re-run at boundary + 14 days (~2026-10-05) for the stop condition and the convergence read. Append both runs to a memory note `wave2-correct-the-inputs.md` and update the spec's status line the way Wave 1's was.

---

## Self-review against the spec

| Spec requirement | Task |
|---|---|
| 2a: error sample restricted to `ERROR_SAMPLE_FETCH_WINDOW_LOCAL = (4, 8)`; storage.py day-filter comment corrected; on/off constant | Task 1 (`ERROR_SAMPLE_FETCH_WINDOW_ENABLED`; four docstrings; a test asserts the stale phrases are gone) |
| 2b: `SPREAD_FLOOR_C` only on unmeasured tiers; measured `corrected_error_rmse` priced as-is under `MAX_ERROR_RMSE_PER_BUCKET`; on/off constant; `spread_source` unchanged; engine.py:988 blindness stated | Task 2 (`SPREAD_FLOOR_MEASURED_TIERS_EXEMPT`, `MEASURED_SPREAD_MIN_C` with its sqrt(1/12) rationale; tests per tier; the blindness pinned by test and documented in the config note) |
| 2c: signed 0a2 on the calibrated basis, mirrored in entry_sim, rule_id kept; refusal at 0a2 not kelly_nonpositive; parity passes | Task 3a (`edge_misses_bar`, `SIGNED_ADMISSION_EDGE`; fact 3 explains why signed is right on both bases) |
| 2d: failed fit not cached, retried next cycle; test of fail-then-succeed | Task 3b (`RETRY_FAILED_CALIBRATION_FITS`) |
| 2e: total outage → `frozenset()` → mix guard refuses; `[]` vs `None` distinguished; rule_id named | Task 3c (`today_source_mix_for`, guard on `is not None`; rule_id `collection_gate`) |
| 2f: entry_sim haircut + exit-fee parity with `_book_has_stop` threaded; parity test covers it | Task 4 (`REPLAY_BOOK_MODE`, `execution_mode` parameter; new parity case + dedicated test) |
| `REGIME_BOUNDARIES` stamped with the deploy date in the final commit before merge; cohort_monitor / calibration_panel / promotion_dossier split by default, `--no-regime-split` pooled | Task 5 (`regimes.py`, three reports) + Task 7 (the stamp mechanics) |
| Pre-registered reads (i) morning vs all-day sd, (ii) priced sd ≠ 0.700, (iv) 0a2 counts; stop condition on 14d held-to-settlement with a day-clustered CI; read-only `mode=ro`; ~200 lines; fixture-DB tests | Task 6 (`wave2_falsifier.py`, ~230 lines; the c/lambda refit and the bought-side calibration gap reads from the spec are checkpoint analyses on `ev_snapshots` / stored `calibrated_prob`, not this script — they run at the Wave 2 + 14 checkpoint with the existing tooling) |
| One merge, one deploy; flags default on; never test on the box; commit trailer | Global Constraints; every task's commit block |

Names used consistently across tasks: `ERROR_SAMPLE_FETCH_WINDOW_LOCAL`, `ERROR_SAMPLE_FETCH_WINDOW_ENABLED`, `error_sample_fetch_bounds_utc`, `forecast_rows_in_error_sample`, `_forecast_rows_in_sample_window` (Task 1 → Task 6); `SPREAD_FLOOR_MEASURED_TIERS_EXEMPT`, `MEASURED_SPREAD_MIN_C`, `MEASURED_SPREAD_SOURCES`, `priced_measured_spread`, `corrected_error_rmse_from_dated`, `measured_error_spread_from_errors` (Task 2 → Task 6); `SIGNED_ADMISSION_EDGE`, `edge_misses_bar` (Task 3a → Task 4's import list); `RETRY_FAILED_CALIBRATION_FITS`; `REFUSE_ON_TOTAL_FORECAST_OUTAGE`, `today_source_mix_for`; `REPLAY_BOOK_MODE`; `REGIME_BOUNDARIES`, `regimes.boundaries/segment_labels/segment_index/regime_segments` (Task 5 → Task 7).
