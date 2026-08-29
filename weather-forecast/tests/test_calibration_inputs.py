"""
tests/test_calibration_inputs.py

Regression tests for live-finding #3 (fixed 2026-08-02): the scheduler's
trading path calibrated on climate-monitor seed observations ONLY --
storage rows, including the settlement-grade METAR daily maxima ingested
since the resolution-source fix, never reached the estimate that actually
trades. pipeline.gather_observations() is now the single shared assembly
(seeds + stored, deduped by source rank) used by both pipeline.run() and
scheduler._run_full_cycle().
"""

from datetime import date

import config
import pipeline
import storage
from clients import climate_monitor_client
from models import ObservedReading



def test_gather_observations_merges_and_dedupes(monkeypatch):
    station = config.get_station("WSSS")
    day = date(2026, 8, 1)

    seed = ObservedReading("WSSS", day, 30.0, "seed_data")
    seed_only_day = ObservedReading("WSSS", date(2026, 7, 30), 30.5, "seed_data")
    metar = ObservedReading("WSSS", day, 33.0, config.RESOLUTION_GRADE_OBSERVATION_SOURCE)
    openmeteo = ObservedReading("WSSS", day, 31.6, "openmeteo_recent_actual")
    stored_only_day = ObservedReading("WSSS", date(2026, 7, 31), 32.0, config.RESOLUTION_GRADE_OBSERVATION_SOURCE)

    monkeypatch.setattr(
        climate_monitor_client, "load_recent_observations",
        lambda st, days=30: [seed, seed_only_day],
    )
    monkeypatch.setattr(
        storage, "load_observations_since",
        lambda icao, cutoff: [metar, openmeteo, stored_only_day],
    )

    obs = pipeline.gather_observations(station, date(2026, 8, 2))
    by_day = {o.target_date: o for o in obs}

    # Stored METAR wins the contested day; seed- and stored-only days survive.
    assert len(obs) == 3
    assert by_day[day].source == config.RESOLUTION_GRADE_OBSERVATION_SOURCE
    assert by_day[day].max_temp_c == 33.0
    assert by_day[date(2026, 7, 30)].source == "seed_data"
    assert by_day[date(2026, 7, 31)].source == config.RESOLUTION_GRADE_OBSERVATION_SOURCE


def test_trading_path_calibrates_on_the_shared_observation_assembly(monkeypatch):
    """
    The estimate the trading cycle prices must take its observations from
    pipeline.gather_observations -- seeds PLUS everything stored, deduped
    by source rank -- and never from climate_monitor_client directly.
    Seeds-only wiring was finding #3: the trading path calibrated blind to
    every settlement-grade METAR reading the system had collected.

    This was an AST guard on scheduler._run_full_cycle until 2026-08-30,
    when the cycle's two calibrations were collapsed into one inside
    pipeline.run(). The property is unchanged and now lives where the
    single calibrate() call does; that the scheduler prices exactly what
    run() returned is pinned in tests/test_cycle_calibration.py.
    """
    assembled = [ObservedReading("WSSS", date(2026, 8, 28), 32.5, "metar_daily_max")]

    monkeypatch.setattr(pipeline, "gather_observations", lambda station, target_date: assembled)
    monkeypatch.setattr(pipeline, "gather_forecasts", lambda station: [])
    monkeypatch.setattr(pipeline, "ensemble_spread_for", lambda station, target_date: [])
    monkeypatch.setattr(pipeline, "gather_same_day_signal", lambda station: "none")

    def _no_direct_seeds(station, days=30):
        raise AssertionError(
            "the trading path read climate-monitor seeds directly -- that is "
            "the seeds-only wiring of finding #3"
        )

    monkeypatch.setattr(climate_monitor_client, "load_recent_observations", _no_direct_seeds)

    seen = {}
    real_calibrate = pipeline.calibrate

    def _spy(**kwargs):
        seen.update(kwargs)
        return real_calibrate(**kwargs)

    monkeypatch.setattr(pipeline, "calibrate", _spy)

    pipeline.run(station_icao="WSSS", target_date=date(2026, 8, 29))

    assert seen.get("observations") == assembled
