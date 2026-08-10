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
    station's own settlement source, and no forecast fetched after the day
    it claims to predict.
    """
    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE observations (station_icao TEXT, target_date TEXT, max_temp_c REAL, source TEXT)")
    conn.execute("CREATE TABLE forecasts (station_icao TEXT, source TEXT, target_date TEXT, "
                 "max_temp_c REAL, fetched_at TEXT, raw_note TEXT)")
    # Aug 1: settled 32.0; forecasts 30.0 + 31.0 -> mean 30.5 -> error -1.5
    conn.execute("INSERT INTO observations VALUES ('X','2026-08-01',32.0,'metar_daily_max')")
    conn.execute("INSERT INTO forecasts VALUES ('X','a','2026-08-01',30.0,'2026-07-31T00:00:00+00:00','')")
    conn.execute("INSERT INTO forecasts VALUES ('X','b','2026-08-01',31.0,'2026-08-01T05:00:00+00:00','')")
    # Fetched two days LATE -- it has seen the day it "forecasts". Excluded.
    conn.execute("INSERT INTO forecasts VALUES ('X','c','2026-08-01',32.0,'2026-08-03T00:00:00+00:00','')")
    # Aug 2: settled 30.0; forecast 30.0 -> error 0.0
    conn.execute("INSERT INTO observations VALUES ('X','2026-08-02',30.0,'metar_daily_max')")
    conn.execute("INSERT INTO forecasts VALUES ('X','a','2026-08-02',30.0,'2026-08-02T00:00:00+00:00','')")
    # Aug 3's observation is from the wrong source -- not settlement truth.
    conn.execute("INSERT INTO observations VALUES ('X','2026-08-03',25.0,'open_meteo')")
    conn.execute("INSERT INTO forecasts VALUES ('X','a','2026-08-03',31.0,'2026-08-03T00:00:00+00:00','')")
    conn.commit()
    conn.close()

    assert sorted(storage.forecast_error_samples("X", "metar_daily_max")) == [-1.5, 0.0]


def test_forecast_bias_stats_fails_closed(monkeypatch):
    def _boom(icao, source):
        raise RuntimeError("db down")
    monkeypatch.setattr(storage, "forecast_error_samples", _boom)
    assert entry_manager.forecast_bias_stats("WSSS") == (None, None, None)
    # ... and that unknown must keep the station out of the market.
    assert entry_manager.collection_only_reason(
        "WSSS", GRADUATED_OBS, bias_n=None, bias_stderr=None, enforce_bias_quality=True,
    ) is not None
