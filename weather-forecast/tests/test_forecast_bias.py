"""
tests/test_forecast_bias.py

Forecast-bias measurement, correction, and the gate that decides whether
the correction can be trusted (added 2026-08-09).

Background: the collection-first gate counted settlement-grade
observations and, at 5, let a station trade. It never checked the thing
the count was a proxy for. Measured across the live registry that day,
WSSS ran +0.07C against RCSS -1.80, WMKK -1.76, RKSI -1.48 -- and at
whole-degree buckets a 1.7C bias misplaces the model's probability mass
by about two buckets, on every bucket, every cycle, in one direction.
Nothing measured it and nothing corrected it.

These tests pin three things: the statistic, the correction actually
moving the central estimate, and the gate refusing a bias that is too
noisy to correct with.
"""

from datetime import date

import pytest

import calibration
import config
import entry_manager
import storage
from models import ObservedReading, PointForecast


def _fc(temp, source="model_a", day=date(2026, 8, 10)):
    return PointForecast(
        station_icao="WSSS", source=source, target_date=day,
        max_temp_c=temp, fetched_at="2026-08-09T00:00:00+00:00", raw_note="",
    )


def _obs(temp, day=date(2026, 8, 9)):
    return ObservedReading("WSSS", day, temp, "metar_daily_max")


# --- the statistic ---------------------------------------------------------

def test_bias_stats_mean_and_standard_error():
    bias, n, se = calibration.bias_stats([1.0, 2.0, 3.0])
    assert bias == pytest.approx(2.0)
    assert n == 3
    # sd = 1.0, se = 1/sqrt(3)
    assert se == pytest.approx(0.577, abs=0.001)


def test_bias_stats_single_sample_has_no_measurable_precision():
    # A mean exists; a standard error does not. None must never be read
    # downstream as "zero error" -- the gate treats it as unknown.
    bias, n, se = calibration.bias_stats([1.5])
    assert (bias, n, se) == (1.5, 1, None)


def test_bias_stats_empty():
    assert calibration.bias_stats([]) == (None, 0, None)


# --- the correction --------------------------------------------------------

def test_correction_shifts_the_forecast_term_by_the_measured_bias():
    forecasts = [_fc(30.0), _fc(31.0)]  # mean 30.5
    uncorrected = calibration.blend_central_estimate(forecasts, [], 31.4)
    # Forecasts run 1.5C COOL of settled truth (negative bias) -> the
    # corrected estimate must come out WARMER, by exactly that much.
    corrected = calibration.blend_central_estimate(forecasts, [], 31.4, forecast_bias_c=-1.5)
    assert uncorrected == pytest.approx(30.5)
    assert corrected == pytest.approx(32.0)


def test_correction_leaves_the_observed_term_alone():
    # Observed readings are settled truth: only the forecast side of the
    # blend may be corrected. forecast mean 30.0 -> 31.0 after a -1.0
    # bias; observed mean 34.0 is untouched.
    forecasts = [_fc(30.0)]
    observations = [_obs(34.0)]
    got = calibration.blend_central_estimate(
        forecasts, observations, 31.4, forecast_bias_c=-1.0, forecast_weight=0.4,
    )
    assert got == pytest.approx(0.4 * 31.0 + 0.6 * 34.0)


# --- the blend weight ------------------------------------------------------

def test_blend_weight_defaults_to_config():
    forecasts, observations = [_fc(30.0)], [_obs(34.0)]
    w = config.FORECAST_BLEND_WEIGHT_DEFAULT
    got = calibration.blend_central_estimate(forecasts, observations, 31.4)
    assert got == pytest.approx(round(w * 30.0 + (1 - w) * 34.0, 1))


def test_blend_weight_is_per_station():
    # Singapore is the one station that genuinely wants persistence weight;
    # everyone else takes the forecast-heavy default.
    assert config.forecast_blend_weight("WSSS") == 0.40
    assert config.forecast_blend_weight("WMKK") == config.FORECAST_BLEND_WEIGHT_DEFAULT
    assert config.forecast_blend_weight("NOT_A_STATION") == config.FORECAST_BLEND_WEIGHT_DEFAULT


def test_wsss_override_is_persistence_heavy_not_a_specific_magic_number():
    """
    The property that is actually measured, asserted as a property.

    On n=9 the per-station optimum bootstraps anywhere from 0.00 to 0.70, so
    pinning an exact value would be pinning noise -- but the DIRECTION is
    well-supported: WSSS beats the forecast-heavy default about 93% of the
    time. This fails if someone drifts the override back toward the default
    (which is what the 0.50 that shipped on 2026-08-10 was), while leaving
    room to retune within the range the data actually supports.
    """
    wsss = config.forecast_blend_weight("WSSS")
    assert wsss < config.FORECAST_BLEND_WEIGHT_DEFAULT, (
        "the WSSS override exists to weight persistence MORE than the default"
    )
    assert 0.2 <= wsss <= 0.5, (
        f"WSSS weight {wsss} is outside the range its 9 samples support"
    )


def test_calibrate_uses_the_station_weight():
    forecasts, observations = [_fc(30.0)], [_obs(34.0)]
    wsss = calibration.calibrate(
        station=config.get_station("WSSS"), target_date=date(2026, 8, 10),
        forecasts=forecasts, observations=observations,
    )
    wmkk = calibration.calibrate(
        station=config.get_station("WMKK"), target_date=date(2026, 8, 10),
        forecasts=forecasts, observations=observations,
    )
    w_wsss = config.forecast_blend_weight("WSSS")
    assert wsss.central_estimate_c == pytest.approx(
        round(w_wsss * 30.0 + (1 - w_wsss) * 34.0, 1)
    )
    w = config.FORECAST_BLEND_WEIGHT_DEFAULT
    assert wmkk.central_estimate_c == pytest.approx(round(w * 30.0 + (1 - w) * 34.0, 1))
    # The forecast-heavy station must land closer to the forecast.
    assert abs(wmkk.central_estimate_c - 30.0) < abs(wsss.central_estimate_c - 30.0)


def test_correction_is_no_longer_diluted_away():
    # The defect this fixes: at w=0.4 a -1.66C measured bias moved the
    # estimate by only +0.66C. At the new default it must carry ~85% of
    # its measured size into the final number.
    forecasts, observations = [_fc(30.0)], [_obs(30.0)]
    base = calibration.blend_central_estimate(forecasts, observations, 31.4)
    corrected = calibration.blend_central_estimate(
        forecasts, observations, 31.4, forecast_bias_c=-1.66,
    )
    shift = corrected - base
    assert shift == pytest.approx(1.66 * config.FORECAST_BLEND_WEIGHT_DEFAULT, abs=0.06)
    assert shift > 1.0  # the old 0.4 blend produced 0.66


def test_calibrate_records_the_bias_it_applied(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_FORECAST_BIAS_CORRECTION", True)
    est = calibration.calibrate(
        station=config.get_station("WSSS"), target_date=date(2026, 8, 10),
        forecasts=[_fc(30.0)], observations=[], forecast_bias_c=-1.2,
    )
    assert est.forecast_bias_c == pytest.approx(-1.2)
    assert est.central_estimate_c == pytest.approx(31.2)
    assert "bias-corrected" in est.notes


def test_kill_switch_disables_the_correction(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_FORECAST_BIAS_CORRECTION", False)
    est = calibration.calibrate(
        station=config.get_station("WSSS"), target_date=date(2026, 8, 10),
        forecasts=[_fc(30.0)], observations=[], forecast_bias_c=-1.2,
    )
    assert est.forecast_bias_c == 0.0
    assert est.central_estimate_c == pytest.approx(30.0)


# --- the gate --------------------------------------------------------------

GRADUATED_OBS = 99  # comfortably past MIN_RESOLUTION_OBS_BEFORE_ENTRY


def test_gate_passes_a_well_measured_bias():
    reason = entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS, bias_n=9, bias_stderr=0.22, enforce_bias_quality=True,
    )
    assert reason is None


def test_gate_blocks_too_few_pairs():
    reason = entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS,
        bias_n=config.MIN_BIAS_PAIRS_BEFORE_ENTRY - 1, bias_stderr=0.1,
        enforce_bias_quality=True,
    )
    assert reason is not None and "forecast/observation pair" in reason


def test_gate_blocks_a_noisy_bias_even_with_enough_pairs():
    # RCSS's real numbers on 2026-08-09: n=3, sd 2.16 -> se 1.25. Even
    # given the pairs, a bias this loosely pinned cannot be corrected for.
    reason = entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS, bias_n=9, bias_stderr=1.25, enforce_bias_quality=True,
    )
    assert reason is not None and "standard error" in reason


def test_gate_blocks_unmeasurable_bias():
    reason = entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS, bias_n=None, bias_stderr=None, enforce_bias_quality=True,
    )
    assert reason is not None and "could not be measured" in reason


def test_gate_bias_check_is_opt_in():
    # Replays that never modelled the bias must keep their old meaning
    # rather than silently rejecting every candidate.
    assert entry_manager.collection_only_reason("WSSS", GRADUATED_OBS) is None


def test_observation_count_still_gates_first():
    # The count check must run BEFORE the bias check: a station with no
    # history should say so, not complain about unmeasurable bias.
    reason = entry_manager.collection_only_reason(
        "WSSS", 0, bias_n=None, bias_stderr=None, enforce_bias_quality=True,
    )
    assert reason is not None and "observation(s)" in reason


def test_forecast_error_samples_query(tmp_path, monkeypatch):
    """
    The SQL itself, against a real sqlite file -- the one piece of this
    that no amount of stubbing exercises. Pins the three things the query
    decides: per-date forecast MEAN (not one row per forecast), only the
    station's own settlement source, and no forecast fetched outside the
    station's own local target day.

    WSSS (UTC+8) rather than a synthetic station: the window is a fact
    about the station's timezone, so these rows now need a real one. Its
    2026-08-01 runs 2026-07-31T16:00Z to 2026-08-01T16:00Z.
    """
    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE observations (station_icao TEXT, target_date TEXT, max_temp_c REAL, source TEXT)")
    conn.execute("CREATE TABLE forecasts (station_icao TEXT, source TEXT, target_date TEXT, "
                 "max_temp_c REAL, fetched_at TEXT, raw_note TEXT)")
    # Aug 1: settled 32.0; forecasts 30.0 + 31.0 -> mean 30.5 -> error -1.5
    conn.execute("INSERT INTO observations VALUES ('WSSS','2026-08-01',32.0,'metar_daily_max')")
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','a','2026-08-01',30.0,'2026-07-31T22:00:00+00:00','')")
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','b','2026-08-01',31.0,'2026-08-01T05:00:00+00:00','')")
    # Fetched two days LATE -- it has seen the day it "forecasts". Excluded.
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','c','2026-08-01',32.0,'2026-08-03T00:00:00+00:00','')")
    # Aug 2: settled 30.0; forecast 30.0 -> error 0.0
    conn.execute("INSERT INTO observations VALUES ('WSSS','2026-08-02',30.0,'metar_daily_max')")
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','a','2026-08-02',30.0,'2026-08-02T00:00:00+00:00','')")
    # Aug 3's observation is from the wrong source -- not settlement truth.
    conn.execute("INSERT INTO observations VALUES ('WSSS','2026-08-03',25.0,'open_meteo')")
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','a','2026-08-03',31.0,'2026-08-03T05:00:00+00:00','')")
    conn.commit()
    conn.close()

    assert sorted(storage.forecast_error_samples("WSSS", "metar_daily_max")) == [-1.5, 0.0]


def test_forecast_error_samples_skips_excluded_sources(tmp_path, monkeypatch):
    """
    THE BIAS MUST BE FITTED ON THE MEAN THAT IS ACTUALLY BLENDED.

    A source kept out of the forecast term but left in this average has the
    correction chasing an error the estimate no longer contains. For RKSI/GFS
    (3-7C cold, the case the exclusion list was added for) that would drag the
    corrected estimate the wrong way by roughly half the gap.
    """
    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    monkeypatch.setattr(
        config, "FORECAST_SOURCES_EXCLUDED_BY_STATION", {"WSSS": ("cold_model",)}
    )
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE observations (station_icao TEXT, target_date TEXT, max_temp_c REAL, source TEXT)")
    conn.execute("CREATE TABLE forecasts (station_icao TEXT, source TEXT, target_date TEXT, "
                 "max_temp_c REAL, fetched_at TEXT, raw_note TEXT)")
    # Settled 32.0. Good source says 31.0 (error -1.0); the excluded one says
    # 26.0, which would drag the pair's mean to 28.5 and the error to -3.5.
    conn.execute("INSERT INTO observations VALUES ('WSSS','2026-08-01',32.0,'metar_daily_max')")
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','good','2026-08-01',31.0,'2026-07-31T22:00:00+00:00','')")
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','cold_model','2026-08-01',26.0,'2026-07-31T22:00:00+00:00','')")
    # A date whose ONLY forecast is the excluded source drops out entirely --
    # it does not fall back the way config.blendable_forecasts() does.
    conn.execute("INSERT INTO observations VALUES ('WSSS','2026-08-02',30.0,'metar_daily_max')")
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','cold_model','2026-08-02',24.0,'2026-08-01T22:00:00+00:00','')")
    conn.commit()
    conn.close()

    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [-1.0]


def test_stations_without_an_exclusion_are_untouched(tmp_path, monkeypatch):
    """The SQL gains a clause only where a station has an exclusion."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    monkeypatch.setattr(
        config, "FORECAST_SOURCES_EXCLUDED_BY_STATION", {"OTHER": ("a",)}
    )
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE observations (station_icao TEXT, target_date TEXT, max_temp_c REAL, source TEXT)")
    conn.execute("CREATE TABLE forecasts (station_icao TEXT, source TEXT, target_date TEXT, "
                 "max_temp_c REAL, fetched_at TEXT, raw_note TEXT)")
    conn.execute("INSERT INTO observations VALUES ('WSSS','2026-08-01',32.0,'metar_daily_max')")
    conn.execute("INSERT INTO forecasts VALUES ('WSSS','a','2026-08-01',30.0,'2026-07-31T22:00:00+00:00','')")
    conn.commit()
    conn.close()

    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [-2.0]


# --- which sources reach the blend ----------------------------------------

def test_rksi_blends_ecmwf_alone():
    """
    Measured over Aug 6-14: ecmwf mean +0.08C sd 0.78, gfs -4.84 sd 1.24
    (all nine errors negative), wwis +1.67 sd 1.80. Pinned by station name
    because this is a claim about RKSI, not about a mechanism.
    """
    assert config.forecast_source_is_blended("RKSI", "open_meteo_ecmwf")
    assert not config.forecast_source_is_blended("RKSI", "open_meteo_gfs")
    assert not config.forecast_source_is_blended("RKSI", "wwis")


def test_the_exclusion_is_one_station_not_a_policy():
    """
    The sweep found the full source pool is already the best POOLED variant
    (RMSE 1.022 against 1.136 for ecmwf+gfs), and WWIS is the single best
    source at RJTT (1.50) while being the noisiest at RKSI. Neither source is
    bad in general, so neither may be excluded in general.
    """
    for icao in ("WSSS", "WMKK", "RJTT", "ZGGG", "ZSPD", "RKPK", "ZBAA", "ZGSZ"):
        assert config.forecast_source_is_blended(icao, "open_meteo_gfs")
        assert config.forecast_source_is_blended(icao, "wwis")


def test_dropping_only_the_broken_source_is_not_what_this_does():
    """
    THE TRAP THIS ENTRY EXISTS TO AVOID.

    GFS ran 4.84C cold at RKSI and WWIS 1.67C warm, so they partly cancelled
    in the three-way mean. Excluding GFS alone exposes WWIS and makes the
    station worse (RMSE 0.85 -> 0.93, bucket hit 44% -> 22%). The blend must
    come out as ECMWF alone, not as the pair.
    """
    forecasts = [
        _fc(33.8, source="open_meteo_ecmwf"),
        _fc(28.4, source="open_meteo_gfs"),
        _fc(39.0, source="wwis"),
    ]
    kept = config.blendable_forecasts("RKSI", forecasts)

    assert [f.source for f in kept] == ["open_meteo_ecmwf"]
    # Truth that day was 35.0. The three-way mean lands at 33.7 by luck;
    # ecmwf+wwis at 36.4; ECMWF alone at 33.8, which is what it forecast.
    assert calibration.blend_central_estimate(kept, [], 30.0) == pytest.approx(33.8)


def test_an_empty_blendable_set_does_not_fall_back_to_excluded_sources():
    """
    On a cycle where only excluded sources returned, RKSI gets NO forecast
    term. Falling back would blend exactly the GFS+WWIS pair measured as the
    worst combination there, and would apply a bias correction fitted on
    ECMWF-only means to a set it was never measured on.
    """
    only_excluded = [_fc(28.4, source="open_meteo_gfs"), _fc(39.0, source="wwis")]
    assert config.blendable_forecasts("RKSI", only_excluded) == []
    # The estimate falls to the observed term rather than to a bad forecast.
    assert calibration.blend_central_estimate(
        [], [_obs(31.0)], 30.0
    ) == pytest.approx(31.0)


def test_both_paths_that_build_a_forecast_term_consult_the_exclusion():
    """
    WIRING GUARD, not a behavioural test of the replay.

    Three places assemble or measure the forecast mean -- pipeline.gather_forecasts
    (live), backtest.engine._entry_pass (replay), storage.forecast_error_samples
    (the bias fitted on it). An exclusion applied in some but not all of them is
    a silent live/replay divergence, and the replay is what the maturity gate
    reads. The behaviour is pinned above for the live and bias paths; the replay
    is only reachable through a full run, so this asserts the call is still there.
    """
    import ast
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent
    wanted = {
        "pipeline.py": "blendable_forecasts",
        "backtest/engine.py": "forecast_source_is_blended",
        "storage.py": "FORECAST_SOURCES_EXCLUDED_BY_STATION",
    }
    missing = []
    for rel, name in wanted.items():
        src = (pkg / rel).read_text(encoding="utf-8")
        names = {
            node.attr for node in ast.walk(ast.parse(src)) if isinstance(node, ast.Attribute)
        }
        if name not in names:
            missing.append(f"{rel} no longer references config.{name}")
    assert not missing, missing


def test_forecast_bias_stats_fails_closed(monkeypatch):
    def _boom(icao, source):
        raise RuntimeError("db down")
    monkeypatch.setattr(storage, "forecast_error_samples_dated", _boom)
    assert entry_manager.forecast_bias_stats("WSSS") == (None, None, None)
    # ... and that unknown must keep the station out of the market.
    assert entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS, bias_n=None, bias_stderr=None, enforce_bias_quality=True,
    ) is not None


# --- the per-station override (2026-08-20) ---------------------------------
#
# VHHH's bias is not noisy, it is unmeasurable: HKO publishes its
# settlement-grade climate record a month at a time, weeks late, so the
# station's observations and its stored forecasts cover disjoint date
# ranges and forecast_error_samples returns nothing at all. It cannot
# collect its way out the way every other gated station can, so the
# operator exempted it. These pin what the exemption may and may not do.

def test_override_lets_an_exempt_station_through_the_bias_check(monkeypatch):
    monkeypatch.setattr(config, "BIAS_GATE_OVERRIDE_STATIONS", {"WSSS"})
    monkeypatch.setattr(config, "LIVE_TRADING_STATIONS", set())
    assert entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS, bias_n=0, bias_stderr=None, enforce_bias_quality=True,
    ) is None


def test_override_does_not_skip_the_observation_count(monkeypatch):
    # The count is the floor the override must never lower: a station with
    # no settlement-grade history is blind, exempt or not.
    monkeypatch.setattr(config, "BIAS_GATE_OVERRIDE_STATIONS", {"WSSS"})
    monkeypatch.setattr(config, "LIVE_TRADING_STATIONS", set())
    reason = entry_manager.collection_only_reason(
        "WSSS", 0, bias_n=0, bias_stderr=None, enforce_bias_quality=True,
    )
    assert reason is not None and "observation(s)" in reason


def test_override_refuses_to_apply_to_a_real_money_station(monkeypatch):
    # "Trade this station on an unmeasured bias" must never be true of
    # money. A station in both sets resolves to GATED, not to armed.
    monkeypatch.setattr(config, "BIAS_GATE_OVERRIDE_STATIONS", {"WSSS"})
    monkeypatch.setattr(config, "LIVE_TRADING_STATIONS", {"WSSS"})
    reason = entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS, bias_n=0, bias_stderr=None, enforce_bias_quality=True,
    )
    assert reason is not None and "forecast/observation pair" in reason


def test_override_does_not_leak_to_unlisted_stations(monkeypatch):
    monkeypatch.setattr(config, "BIAS_GATE_OVERRIDE_STATIONS", {"VHHH"})
    monkeypatch.setattr(config, "LIVE_TRADING_STATIONS", set())
    reason = entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS, bias_n=0, bias_stderr=None, enforce_bias_quality=True,
    )
    assert reason is not None and "forecast/observation pair" in reason


def test_the_shipped_override_set_is_empty():
    # It held VHHH for a few hours on 2026-08-20 and then stopped needing
    # to: forecast_bias_stats() falls back to a bias measured against
    # market settlement, so VHHH satisfies the gate instead of skipping it.
    # A non-empty set here means somebody is trading a bias measured by no
    # method at all, which is a different and much weaker claim.
    assert config.BIAS_GATE_OVERRIDE_STATIONS == set()
    assert not config.bias_gate_is_overridden("VHHH")


def test_vhhh_is_paper_only():
    assert "VHHH" not in config.LIVE_TRADING_STATIONS
    assert not config.live_mode_is_permitted("VHHH", "live")
    assert not config.live_mode_is_permitted("VHHH", "simulation")


# --- the settlement-derived fallback ---------------------------------------

def test_settlement_grade_pairs_win_when_they_exist(monkeypatch):
    # A temperature from the record the market settles on beats a
    # 1-degree bucket, so a station with both must keep using the former.
    # Three same-day errors so the recency weighting is a no-op and the
    # assertion stays about WHICH sample won, not about the weighting.
    day = date(2026, 8, 20)
    monkeypatch.setattr(storage, "forecast_error_samples_dated",
                        lambda i, s: [(day, -1.0), (day, -1.4), (day, -1.2)])
    import bucket_bias
    monkeypatch.setattr(bucket_bias, "derived_bias_stats",
                        lambda i: (99.0, 500, 0.001))
    bias, n, stderr = entry_manager.forecast_bias_stats("WSSS")
    assert n == 3 and bias == pytest.approx(-1.2)


def test_an_empty_settlement_grade_sample_falls_back(monkeypatch):
    # VHHH's real situation: forecast_error_samples returns [] because its
    # observations and its forecasts cover disjoint date ranges.
    monkeypatch.setattr(storage, "forecast_error_samples_dated", lambda i, s: [])
    import bucket_bias
    monkeypatch.setattr(bucket_bias, "derived_bias_stats", lambda i: (-1.027, 15, 0.232))
    assert entry_manager.forecast_bias_stats("VHHH") == (-1.027, 15, 0.232)


def test_the_fallback_still_gates_when_it_measures_nothing(monkeypatch):
    # No settlement-grade pairs AND no recorded settlements is "unmeasured",
    # and must reach the gate as such rather than as an unbiased zero.
    monkeypatch.setattr(storage, "forecast_error_samples_dated", lambda i, s: [])
    import bucket_bias
    monkeypatch.setattr(bucket_bias, "derived_bias_stats", lambda i: (None, 0, None))
    bias, n, stderr = entry_manager.forecast_bias_stats("VHHH")
    assert n == 0
    reason = entry_manager.collection_only_reason(
        "VHHH", GRADUATED_OBS, bias_n=n, bias_stderr=stderr, enforce_bias_quality=True)
    assert reason is not None and "forecast/observation pair" in reason


def test_a_broken_fallback_reads_as_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(storage, "forecast_error_samples_dated", lambda i, s: [])
    import bucket_bias

    def boom(icao):
        raise RuntimeError("market data store is gone")

    monkeypatch.setattr(bucket_bias, "derived_bias_stats", boom)
    assert entry_manager.forecast_bias_stats("VHHH") == (None, None, None)


# --- recency weighting (2026-08-27) ----------------------------------------
#
# The estimator was a flat mean over all history, so a stale sample kept
# full weight forever. These pin the properties that make the weighted form
# safe to swap in, not the particular half-life.

from datetime import timedelta as _td


def test_equal_ages_reproduce_the_unweighted_statistic():
    # If every sample is the same age the weighting must be a no-op --
    # otherwise swapping estimators would move every station's bias for no
    # reason. n must come back as the ROW COUNT here, which is the case
    # floating point quietly broke first time round.
    day = date(2026, 8, 20)
    errs = [-1.0, -1.4, -1.2, -0.8]
    w = calibration.bias_stats_weighted([(day, e) for e in errs], day, 14.0)
    u = calibration.bias_stats(errs)
    assert w[0] == u[0]
    assert w[1] == u[1] == 4
    assert w[2] == pytest.approx(u[2], abs=0.06)   # /n vs /(n-1) in the sd


def test_a_recent_sample_outweighs_an_old_one():
    as_of = date(2026, 8, 27)
    dated = [(as_of, 0.0), (as_of - _td(days=28), -2.0)]
    bias, _, _ = calibration.bias_stats_weighted(dated, as_of, 14.0)
    # 28 days is two half-lives, so the old sample carries 1/4 the weight:
    # (0*1 + -2*0.25) / 1.25 = -0.4, against an unweighted -1.0.
    assert bias == pytest.approx(-0.4, abs=0.001)
    assert calibration.bias_stats([0.0, -2.0])[0] == pytest.approx(-1.0)


def test_effective_n_falls_as_the_sample_ages():
    # The gate reads this n. A sample dominated by one recent day carries
    # less information than its row count claims, and must say so.
    as_of = date(2026, 8, 27)
    fresh = [(as_of - _td(days=i), 0.2) for i in range(10)]
    stale = [(as_of, 0.2)] + [(as_of - _td(days=90), 0.2) for _ in range(9)]
    assert calibration.bias_stats_weighted(fresh, as_of, 14.0)[1] == 9
    assert calibration.bias_stats_weighted(stale, as_of, 14.0)[1] <= 2


def test_a_future_dated_sample_is_not_weighted_up():
    # 0.5 ** negative is > 1, which would hand a bad row more influence
    # than any real observation. Clamp to today instead.
    as_of = date(2026, 8, 27)
    dated = [(as_of + _td(days=30), 5.0), (as_of, 0.0)]
    bias, _, _ = calibration.bias_stats_weighted(dated, as_of, 14.0)
    assert bias == pytest.approx(2.5)


def test_an_empty_sample_is_unmeasured_not_zero():
    assert calibration.bias_stats_weighted([], date(2026, 8, 27), 14.0) == (None, 0, None)
