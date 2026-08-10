"""
calibration.estimate_std_dev -- the width of the distribution every bucket
probability is cut from.

This function had NO direct test coverage, which is how a tier that loses
to a constant survived in it. Measured on 50 station-days scoring Brier
against settled buckets: the old forecast-variance tier fired for every
sample and scored 0.8040 against 0.7228 for a flat 1.0C. It was measuring
model DISAGREEMENT from two or three numbers, not forecast ERROR.

The tests below pin the properties that make the replacement safe, not the
exact numbers it currently returns.
"""

from datetime import date, timedelta

import pytest

import calibration
import config
import storage
from models import ObservedReading, PointForecast


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    monkeypatch.setattr(calibration, "_pooled_spread_cache", {})
    storage._connect().close()


def seed_pairs(icao, errors, start=date(2026, 7, 1)):
    """
    One forecast/observation pair per error, so
    storage.forecast_error_samples() returns exactly `errors`.
    """
    station = config.get_station(icao)
    truth = 32.0
    for i, err in enumerate(errors):
        target = start + timedelta(days=i)
        storage.save_observation(ObservedReading(
            station_icao=icao, target_date=target, max_temp_c=truth,
            source=station.resolution_grade_source,
        ))
        storage.save_forecast(PointForecast(
            station_icao=icao, source="open_meteo_ecmwf", target_date=target,
            max_temp_c=truth + err, fetched_at=f"{target.isoformat()}T00:00:00+00:00",
        ))


def _fc(t):
    return PointForecast(station_icao="WSSS", source="s", target_date=date(2026, 8, 10),
                         max_temp_c=t, fetched_at="")


def _obs(t, day=1):
    return ObservedReading(station_icao="WSSS", target_date=date(2026, 8, day),
                           max_temp_c=t, source="metar_daily_max")


# --------------------------------------------------------------------------
# Tier selection
# --------------------------------------------------------------------------

def test_ensemble_spread_wins_when_available():
    sd, src = calibration.estimate_std_dev([], [], ensemble_members=[31.0, 32.0, 33.0],
                                           station_icao="WSSS")
    assert src == "ensemble"
    assert sd == pytest.approx(1.0)


def test_station_with_enough_pairs_gets_its_own_measured_spread():
    seed_pairs("WSSS", [-1.0, 0.0, 1.0, -1.0, 1.0, 0.0])
    sd, src = calibration.estimate_std_dev([_fc(32.0)], [_obs(32.0)], station_icao="WSSS")

    assert src == "measured_error"
    assert config.SPREAD_FLOOR_C <= sd <= config.SPREAD_CEILING_C


def test_station_without_enough_pairs_falls_back_to_pooled():
    seed_pairs("WSSS", [-1.0, 0.0, 1.0, -1.0, 1.0, 0.0])       # enough
    seed_pairs("WMKK", [0.5, -0.5])                             # not enough
    sd, src = calibration.estimate_std_dev([_fc(32.0)], [_obs(32.0)], station_icao="WMKK")

    assert src == "pooled_error"
    assert config.SPREAD_FLOOR_C <= sd <= config.SPREAD_CEILING_C


def test_empty_database_falls_back_to_the_constant():
    sd, src = calibration.estimate_std_dev([_fc(32.0)], [_obs(32.0)], station_icao="WSSS")
    assert src == "fallback_default"
    assert sd == pytest.approx(
        min(max(config.POOLED_SPREAD_FALLBACK_C, config.SPREAD_FLOOR_C), config.SPREAD_CEILING_C)
    )


def test_without_a_station_the_measured_tier_is_unreachable():
    seed_pairs("WSSS", [-1.0, 0.0, 1.0, -1.0, 1.0, 0.0])
    _, src = calibration.estimate_std_dev([_fc(32.0)], [_obs(32.0)])
    assert src in ("pooled_error", "fallback_default")


# --------------------------------------------------------------------------
# The tier that was removed
# --------------------------------------------------------------------------

def test_disagreeing_forecasts_no_longer_widen_the_spread():
    """
    THE removed behaviour. Three point forecasts spanning 8C used to yield
    a ~4C spread -- a distribution so flat that every bucket looks
    mispriced. Model disagreement is not forecast error.
    """
    seed_pairs("WSSS", [-1.0, 0.0, 1.0, -1.0, 1.0, 0.0])
    wild = [_fc(28.0), _fc(32.0), _fc(36.0)]
    calm = [_fc(31.9), _fc(32.0), _fc(32.1)]

    sd_wild, src_wild = calibration.estimate_std_dev(wild, [_obs(32.0)], station_icao="WSSS")
    sd_calm, src_calm = calibration.estimate_std_dev(calm, [_obs(32.0)], station_icao="WSSS")

    assert src_wild == src_calm == "measured_error"
    assert sd_wild == sd_calm, "the spread must not depend on how much the models disagree"


def test_observed_history_variance_no_longer_drives_the_spread():
    """Weather variability is not forecast error either."""
    seed_pairs("WSSS", [-1.0, 0.0, 1.0, -1.0, 1.0, 0.0])
    volatile = [_obs(25.0, 1), _obs(35.0, 2), _obs(28.0, 3)]

    _, src = calibration.estimate_std_dev([_fc(32.0)], volatile, station_icao="WSSS")
    assert src == "measured_error"


# --------------------------------------------------------------------------
# The clamp
# --------------------------------------------------------------------------

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


def test_a_huge_measured_spread_is_capped():
    seed_pairs("WSSS", [-6.0, 6.0, -5.0, 5.0, -6.0, 6.0])
    sd, _ = calibration.estimate_std_dev([_fc(32.0)], [_obs(32.0)], station_icao="WSSS")
    assert sd == pytest.approx(config.SPREAD_CEILING_C)


def test_ensemble_spread_is_clamped_too():
    """Ensembles are known to be under-dispersive at short lead times."""
    sd, src = calibration.estimate_std_dev([], [], ensemble_members=[32.0, 32.001],
                                           station_icao="WSSS")
    assert src == "ensemble"
    assert sd == pytest.approx(config.SPREAD_FLOOR_C)


# --------------------------------------------------------------------------
# The pool
# --------------------------------------------------------------------------

def test_pool_centres_each_station_before_pooling():
    """
    Two stations with tight but very differently-BIASED errors must pool to
    a tight spread. Pooling raw errors would fold the between-station bias
    difference into what is meant to be a within-station spread.
    """
    seed_pairs("WSSS", [3.0, 3.1, 2.9, 3.0, 3.1, 2.9])      # bias +3, spread tiny
    seed_pairs("WMKK", [-3.0, -3.1, -2.9, -3.0, -3.1, -2.9])  # bias -3, spread tiny

    pooled, n = calibration.pooled_error_spread()

    assert n > 0
    assert pooled < 1.0, (
        f"pooled spread {pooled:.2f} folded in the 6C bias gap between stations"
    )


def test_pooled_cache_is_keyed_on_the_database(tmp_path, monkeypatch):
    """
    A replay repoints config.DB_PATH at a throwaway db. A cache keyed on
    time alone would serve the LIVE pool into that replay -- lookahead, and
    of the worst kind, since the live db holds every day the replay is
    pretending not to know.
    """
    seed_pairs("WSSS", [2.0, -2.0, 2.0, -2.0, 2.0, -2.0])
    wide, _ = calibration.pooled_error_spread()

    other = tmp_path / "replay.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", str(other))
    storage._connect().close()
    seed_pairs("WSSS", [0.1, -0.1, 0.1, -0.1, 0.1, -0.1])
    narrow, _ = calibration.pooled_error_spread()

    assert narrow != wide, "the pooled spread leaked across databases"


# --------------------------------------------------------------------------
# Downstream
# --------------------------------------------------------------------------

def test_pooled_error_counts_as_low_confidence():
    """
    A station without its own measured spread must clear the doubled edge
    bar. Under the old chain every station got "forecast_variance", which
    never tripped this despite being the least trustworthy number in the
    model.
    """
    assert "pooled_error" in config.LOW_CONFIDENCE_SPREAD_SOURCES
    assert "fallback_default" in config.LOW_CONFIDENCE_SPREAD_SOURCES
    assert "measured_error" not in config.LOW_CONFIDENCE_SPREAD_SOURCES
    assert "ensemble" not in config.LOW_CONFIDENCE_SPREAD_SOURCES


def test_calibrate_passes_the_station_through():
    seed_pairs("WSSS", [-1.0, 0.0, 1.0, -1.0, 1.0, 0.0])
    est = calibration.calibrate(
        station=config.get_station("WSSS"), target_date=date(2026, 8, 10),
        forecasts=[_fc(32.0)], observations=[_obs(32.0)],
    )
    assert est.spread_source == "measured_error", (
        "calibrate() must pass station_icao, or the measured tier is dead code"
    )


# --------------------------------------------------------------------------
# The replay path
# --------------------------------------------------------------------------

def test_replay_never_reads_the_measured_record():
    """
    Both measured tiers read the WHOLE stored error record, so either one
    inside a replay prices a simulated Aug-3 tick using Aug-10 errors. This
    is the spread-side twin of the engine pinning forecast_bias_c at 0.0.
    """
    seed_pairs("WSSS", [-2.0, 2.0, -2.0, 2.0, -2.0, 2.0])   # a wide record exists
    sd, src = calibration.estimate_std_dev(
        [_fc(32.0)], [_obs(32.0)], station_icao="WSSS", allow_measured=False,
    )

    assert src == "replay_constant"
    assert sd == pytest.approx(
        min(max(config.POOLED_SPREAD_FALLBACK_C, config.SPREAD_FLOOR_C),
            config.SPREAD_CEILING_C)
    )


def test_replay_constant_is_not_penalised_as_low_confidence():
    """
    It is a real measured number. Marking it low-confidence would make every
    replayed entry clear a doubled edge bar that live never faces -- a
    live/replay divergence in the one direction the backtest exists to rule
    out.
    """
    assert "replay_constant" not in config.LOW_CONFIDENCE_SPREAD_SOURCES


def test_engine_opts_out_of_the_measured_spread():
    """Structural: the opt-out is only useful if the engine actually passes it."""
    import ast
    from pathlib import Path

    from backtest import engine

    tree = ast.parse(Path(engine.__file__).read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "calibrate"
    ]
    assert calls, "no calibration.calibrate() call found in the engine"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert "allow_measured_spread" in kw, (
            f"engine.py:{call.lineno} calls calibrate() without opting out of the "
            f"measured spread -- that is lookahead"
        )
        assert kw["allow_measured_spread"].value is False
