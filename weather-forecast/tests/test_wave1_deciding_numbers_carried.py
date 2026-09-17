"""
Wave 1: the numbers an EntryDecision carries are the numbers its gates
compared -- not a recomputation, and not the raw ones under a new name.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from clients import market_client
from models import EVResult


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", True)


def _ev(calibrated=None, source="uncalibrated", model_prob=0.55, price=0.35, side="YES"):
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side=side,
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02,
        net_ev_per_dollar=(model_prob - price) / price - 0.03, spread_source="ensemble",
        market_bid=price - 0.02, calibrated_prob=calibrated, calibration_source=source,
    )


def test_calibrated_numbers_are_carried_verbatim(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    ev = _ev(calibrated=0.50, source="pooled_isotonic")

    d = entry_manager.evaluate_entry(ev, "TOK", min_net_ev=0.15)

    assert d.approved and d.rule_id == "approved"
    assert d.calibrated_prob == pytest.approx(0.50)
    assert d.calibration_source == "pooled_isotonic"
    assert d.admission_edge == pytest.approx(entry_manager.admission_edge(ev))
    assert d.sizing_edge == pytest.approx(entry_manager.sizing_edge(ev))
    assert d.admission_edge == pytest.approx(0.15)      # calibrated - price, not raw
    assert d.raw_edge == pytest.approx(0.20)            # raw stays raw
    assert d.model_prob == pytest.approx(0.55)


def test_uncalibrated_numbers_fall_back_to_raw_and_say_so(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert d.calibrated_prob is None
    assert d.calibration_source == "uncalibrated"
    assert d.admission_edge == pytest.approx(0.20)
    assert d.sizing_edge == pytest.approx(0.20)


def test_a_pre_sizing_veto_carries_the_numbers_but_no_size(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(price=0.90, model_prob=0.95), "TOK", min_net_ev=0.15)
    assert d.rule_id == "00"
    assert d.admission_edge == pytest.approx(0.05)
    assert d.kelly_size_preclamp_usd is None


def test_the_0a2_veto_records_the_edge_it_compared(monkeypatch):
    """Calibrated 0.36 vs price 0.35 is 0.01 -- under MIN_ABS_RAW_EDGE (0.03) -- while raw is 0.20."""
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(calibrated=0.36, source="station_isotonic"), "TOK", min_net_ev=0.15)
    assert d.rule_id == "0a2"
    assert d.admission_edge == pytest.approx(0.01)


def test_preclamp_size_equals_the_paper_recommended_size(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert d.approved
    assert d.kelly_size_preclamp_usd == pytest.approx(d.recommended_size_usd)
    assert d.kelly_size_preclamp_usd > config.LIVE_TRADE_SIZE_USD


def test_preclamp_size_survives_the_live_clamp(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert d.approved
    assert d.recommended_size_usd == pytest.approx(config.LIVE_TRADE_SIZE_USD)
    assert d.kelly_size_preclamp_usd > config.LIVE_TRADE_SIZE_USD


def test_preclamp_size_is_depth_capped_where_depth_is_known(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 20.0)
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert d.kelly_size_preclamp_usd == pytest.approx(20.0 * config.MAX_DEPTH_UTILIZATION_PCT)


def test_preclamp_helper_rounds_like_the_approval_size():
    assert entry_manager.preclamp_size_usd(12.3456, None) == 12.35
    assert entry_manager.preclamp_size_usd(12.3456, 8.0) == round(8.0 * config.MAX_DEPTH_UTILIZATION_PCT, 2)


def test_collection_gate_decision_is_stamped():
    ev = _ev(calibrated=0.5, source="pooled_isotonic")
    d = entry_manager.collection_only_decision(ev, "TOK", "Collection-only: test")
    assert d.rule_id == "collection_gate"
    assert d.calibrated_prob == pytest.approx(0.5)
    assert d.admission_edge == pytest.approx(0.15)


def test_budget_sites_relabel_but_keep_the_numbers(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    scaled = entry_manager.apply_portfolio_budget([d], max_total_usd=d.recommended_size_usd / 2)
    assert scaled[0].approved and scaled[0].rule_id == "budget_scaled"
    assert scaled[0].kelly_size_preclamp_usd == pytest.approx(d.kelly_size_preclamp_usd)
    exhausted = entry_manager.apply_portfolio_budget([d], max_total_usd=10.0, existing_exposure_usd=10.0)
    assert not exhausted[0].approved and exhausted[0].rule_id == "budget_exhausted"
