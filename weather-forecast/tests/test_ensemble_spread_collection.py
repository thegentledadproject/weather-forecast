"""
tests/test_ensemble_spread_collection.py

The ensemble spread was fetched on every cycle, used once, and discarded.
calibration.estimate_std_dev() returns the "ensemble" tier before it ever
reaches measured_error_spread(), and the fetch succeeds for every station on
every cycle -- so the measured tier is unreachable live and nothing was
recorded that could score the two tiers against each other.

pipeline.ensemble_spread_for() is the single shared assembly that fetches AND
records, mirroring pipeline.gather_observations(). Both call sites (pipeline.run
and scheduler._run_full_cycle) must go through it, or the record has holes on
exactly the cycles that trade.
"""

import ast
import statistics
from datetime import date
from pathlib import Path

import pytest

import config
import pipeline
import storage
from clients import openmeteo_client

PKG = Path(__file__).resolve().parent.parent


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


def test_scheduler_trading_path_records_the_ensemble_it_calibrates_on():
    """
    AST guard on scheduler._run_full_cycle: its calibrate() call must take
    ensemble_members from pipeline.ensemble_spread_for, not straight from
    openmeteo_client.get_ensemble_spread -- the latter records nothing, and
    the trading cycle is the one whose spread the comparison needs.
    """
    tree = ast.parse((PKG / "scheduler.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_run_full_cycle"
    )

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

    calibrate_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and called_name(n) == "calibrate"
    ]
    assert calibrate_calls, "calibrate() call not found in _run_full_cycle"
    kw = next(k for k in calibrate_calls[0].keywords if k.arg == "ensemble_members")
    assert isinstance(kw.value, ast.Call)
    assert called_name(kw.value) == "pipeline.ensemble_spread_for", (
        "scheduler's calibrate() must source ensemble members via "
        "pipeline.ensemble_spread_for so the cycle that trades is recorded"
    )
