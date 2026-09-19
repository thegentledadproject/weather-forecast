"""
Wave 1: EntryDecision -> Position -> positions row -> Position, for the five
deciding numbers. The chain is only as good as its weakest hop, so every hop
is exercised here: the dataclass fields exist, storage writes and reads them,
and executor.open_position copies them off the decision.
"""
from datetime import date

import pytest

import config
import executor
import storage
from models import EntryDecision, Position


@pytest.fixture(autouse=True)
def temp_db(tmp_db):
    return tmp_db


def _position(**overrides) -> Position:
    base = dict(
        position_id="WSSS:2026-09-17:32:YES:t", station_icao="WSSS",
        target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        entry_price=0.30, size_usd=10.0, entry_time="2026-09-17T00:00:00+00:00",
        status="open", is_paper=True,
        model_prob=0.42, raw_edge=0.12, net_ev_at_size=0.08,
        calibrated_prob=0.37, calibration_source="pooled_isotonic",
        admission_edge=0.07, sizing_edge=0.07, kelly_size_preclamp_usd=23.5,
    )
    base.update(overrides)
    return Position(**base)


def _decision(**overrides) -> EntryDecision:
    base = dict(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        kelly_fraction_raw=0.2, kelly_fraction_applied=0.05,
        recommended_size_usd=10.0, available_depth_usd=200.0,
        slippage_at_size_pct=0.01, net_ev_at_size=0.08,
        approved=True, reason="test", station_maturity="mature",
        entry_price=0.30, token_id="tok-1", model_prob=0.42, raw_edge=0.12,
        calibrated_prob=0.37, calibration_source="pooled_isotonic",
        admission_edge=0.07, sizing_edge=0.07, kelly_size_preclamp_usd=23.5,
        rule_id="approved",
    )
    base.update(overrides)
    return EntryDecision(**base)


def test_entry_decision_defaults_are_unknown_not_zero():
    d = EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=0.0,
        available_depth_usd=None, slippage_at_size_pct=None, net_ev_at_size=None,
        approved=False, reason="x", station_maturity="mature",
    )
    assert d.calibrated_prob is None
    assert d.calibration_source == "uncalibrated"
    assert d.admission_edge is None and d.sizing_edge is None
    assert d.kelly_size_preclamp_usd is None
    assert d.rule_id == "unspecified"


def test_position_round_trips_the_five_fields():
    storage.open_position(_position())

    (loaded,) = storage.load_open_positions("WSSS")

    assert loaded.calibrated_prob == pytest.approx(0.37)
    assert loaded.calibration_source == "pooled_isotonic"
    assert loaded.admission_edge == pytest.approx(0.07)
    assert loaded.sizing_edge == pytest.approx(0.07)
    assert loaded.kelly_size_preclamp_usd == pytest.approx(23.5)


def test_position_round_trips_none_as_none():
    storage.open_position(_position(
        calibrated_prob=None, calibration_source=None, admission_edge=None,
        sizing_edge=None, kelly_size_preclamp_usd=None,
    ))
    (loaded,) = storage.load_open_positions("WSSS")
    assert loaded.calibrated_prob is None
    assert loaded.calibration_source is None
    assert loaded.kelly_size_preclamp_usd is None


def test_closed_history_loader_reads_them_too():
    storage.open_position(_position())
    storage.close_position("WSSS:2026-09-17:32:YES:t", 0.5, "2026-09-18T00:00:00+00:00",
                           "closed_resolution", "market_resolved")
    (loaded,) = storage.load_position_history("WSSS")
    assert loaded.sizing_edge == pytest.approx(0.07)


def test_executor_copies_the_five_fields_onto_the_position(monkeypatch):
    monkeypatch.setitem(executor.EXECUTION_MODE, "WSSS", "paper")

    executor.open_position(_decision())

    (stored,) = storage.load_open_positions("WSSS")
    assert stored.calibrated_prob == pytest.approx(0.37)
    assert stored.calibration_source == "pooled_isotonic"
    assert stored.admission_edge == pytest.approx(0.07)
    assert stored.sizing_edge == pytest.approx(0.07)
    assert stored.kelly_size_preclamp_usd == pytest.approx(23.5)
    # Untouched by this wave.
    assert stored.model_prob == pytest.approx(0.42)
    assert stored.net_ev_at_size == pytest.approx(0.08)
