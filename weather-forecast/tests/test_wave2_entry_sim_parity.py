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
