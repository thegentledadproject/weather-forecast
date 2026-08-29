"""
tests/test_ensemble_spread_collection.py

The ensemble spread was fetched on every cycle, used once, and discarded.
calibration.estimate_std_dev() returns the "ensemble" tier before it ever
reaches measured_error_spread(), and the fetch succeeds for every station on
every cycle -- so the measured tier is unreachable live and nothing was
recorded that could score the two tiers against each other.

pipeline.ensemble_spread_for() is the single shared assembly that fetches AND
records, mirroring pipeline.gather_observations(). The trading cycle must go
through it, or the record has holes on exactly the cycles that trade. Since
2026-08-30 there is only ONE call site to check: scheduler._run_full_cycle
prices the estimate pipeline.run() returns instead of building a second one.
"""

import statistics
from datetime import date
import pytest

import config
import pipeline
import storage
from clients import openmeteo_client



@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))


def test_fetched_members_are_returned_and_recorded(db, monkeypatch):
    members = [30.1, 30.9, 31.4, 32.2]
    monkeypatch.setattr(openmeteo_client, "get_ensemble_spread", lambda station: members)
    station = config.get_station("WSSS")

    returned = pipeline.ensemble_spread_for(station, date(2026, 8, 29))

    assert returned == members
    assert storage.load_ensemble_spreads("WSSS") == {
        date(2026, 8, 29): (pytest.approx(statistics.stdev(members)), 4)
    }


def test_recorded_spread_is_unclamped(db, monkeypatch):
    """
    SPREAD_FLOOR_C lifts several stations' ensembles, so a clamped record
    would hide the quantity the tier comparison exists to measure.
    """
    members = [31.0, 31.05, 31.1]  # stdev ~0.05, far under SPREAD_FLOOR_C
    monkeypatch.setattr(openmeteo_client, "get_ensemble_spread", lambda station: members)

    pipeline.ensemble_spread_for(config.get_station("WSSS"), date(2026, 8, 29))

    recorded, _ = storage.load_ensemble_spreads("WSSS")[date(2026, 8, 29)]
    assert recorded < config.SPREAD_FLOOR_C
    assert recorded == pytest.approx(statistics.stdev(members))


@pytest.mark.parametrize("members", [None, [], [31.4]])
def test_nothing_is_recorded_when_there_is_no_dispersion_to_record(db, monkeypatch, members):
    """A single member (or none) has no stdev -- record nothing, not a zero."""
    monkeypatch.setattr(openmeteo_client, "get_ensemble_spread", lambda station: members)

    returned = pipeline.ensemble_spread_for(config.get_station("WSSS"), date(2026, 8, 29))

    assert returned == members
    assert storage.load_ensemble_spreads("WSSS") == {}


def test_a_storage_failure_does_not_break_the_cycle(db, monkeypatch):
    """Collection is a side effect. It must never take a trading cycle down."""
    members = [30.1, 30.9, 31.4]
    monkeypatch.setattr(openmeteo_client, "get_ensemble_spread", lambda station: members)

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(storage, "save_ensemble_spread", boom)

    assert pipeline.ensemble_spread_for(config.get_station("WSSS"), date(2026, 8, 29)) == members


def test_trading_path_records_the_ensemble_it_calibrates_on(monkeypatch):
    """
    The estimate the trading cycle prices must take its ensemble members
    from pipeline.ensemble_spread_for -- which RECORDS them -- and not
    straight from openmeteo_client.get_ensemble_spread, which records
    nothing. The cycle that trades is the one whose spread the
    measured-vs-ensemble comparison needs.

    This was an AST guard on scheduler._run_full_cycle until 2026-08-30,
    when the cycle's two calibrations were collapsed into one inside
    pipeline.run(). The property is unchanged and now lives where the
    single calibrate() call does; that the scheduler prices exactly what
    run() returned is pinned in tests/test_cycle_calibration.py.
    """
    members = [30.1, 30.4, 31.2]
    recorded = []

    monkeypatch.setattr(
        pipeline, "ensemble_spread_for",
        lambda station, target_date: recorded.append(members) or members,
    )
    monkeypatch.setattr(pipeline, "gather_forecasts", lambda station: [])
    monkeypatch.setattr(pipeline, "gather_observations", lambda station, target_date: [])
    monkeypatch.setattr(pipeline, "gather_same_day_signal", lambda station: "none")

    def _no_direct_fetch(station, timeout=10):
        raise AssertionError(
            "the trading path fetched ensemble members directly from "
            "openmeteo_client -- those are never recorded"
        )

    monkeypatch.setattr(openmeteo_client, "get_ensemble_spread", _no_direct_fetch)

    seen = {}
    real_calibrate = pipeline.calibrate

    def _spy(**kwargs):
        seen.update(kwargs)
        return real_calibrate(**kwargs)

    monkeypatch.setattr(pipeline, "calibrate", _spy)

    pipeline.run(station_icao="WSSS", target_date=date(2026, 8, 29))

    assert seen.get("ensemble_members") == members
    assert recorded, "pipeline.ensemble_spread_for was never reached"
