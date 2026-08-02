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

import ast
from datetime import date
from pathlib import Path

import config
import pipeline
import storage
from clients import climate_monitor_client
from models import ObservedReading

PKG = Path(__file__).resolve().parent.parent


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


def test_scheduler_trading_path_uses_shared_assembly():
    """
    AST guard on scheduler._run_full_cycle: its calibrate() call must take
    observations from pipeline.gather_observations, and must not hand
    climate_monitor_client.load_recent_observations to calibrate directly
    -- that exact wiring was finding #3.
    """
    src = (PKG / "scheduler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_run_full_cycle"
    )
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]

    def called_name(call):
        f = call.func
        if isinstance(f, ast.Attribute):
            parts = [f.attr]
            while isinstance(f.value, ast.Attribute):
                f = f.value
                parts.append(f.attr)
            if isinstance(f.value, ast.Name):
                parts.append(f.value.id)
            return ".".join(reversed(parts))
        return f.id if isinstance(f, ast.Name) else ""

    calibrate_calls = [c for c in calls if called_name(c) == "calibrate"]
    assert calibrate_calls, "calibrate() call not found in _run_full_cycle"
    obs_kw = next(kw for kw in calibrate_calls[0].keywords if kw.arg == "observations")
    assert isinstance(obs_kw.value, ast.Call)
    assert called_name(obs_kw.value) == "pipeline.gather_observations", (
        "scheduler's calibrate() must source observations via "
        "pipeline.gather_observations -- seeds-only wiring was finding #3"
    )
