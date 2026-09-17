"""
Wave 1 (spec 1d): the paper shadow pass needs the entry path evaluated AS
PAPER for a station whose executor.EXECUTION_MODE is live -- through an
explicit parameter, never by writing that dict. And it needs the paper EV
table for the same cycle without re-fetching the book:
ev_engine.reprice_for_mode must equal compute_ev_table run in that mode.
"""
import dataclasses
from datetime import date

import pytest

import config
import entry_manager
import ev_engine
import executor
import storage
from clients import market_client
from models import CalibratedEstimate, EVResult, MarketQuote


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})


def _ev():
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        model_prob=0.55, market_price=0.35, raw_edge=0.20,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02, net_ev_per_dollar=0.54,
        spread_source="ensemble", market_bid=0.33,
    )


def test_helpers_default_to_the_module_dict_and_accept_an_override():
    assert entry_manager._execution_mode("WSSS") == "live"
    assert entry_manager._execution_mode("WSSS", "paper") == "paper"
    assert entry_manager._candidate_is_paper("WSSS") is False
    assert entry_manager._candidate_is_paper("WSSS", "paper") is True
    assert entry_manager._book_has_stop("WSSS") is True
    assert entry_manager._book_has_stop("WSSS", "paper") is False
    assert entry_manager.live_size_cap_usd("WSSS") == config.LIVE_TRADE_SIZE_USD
    assert entry_manager.live_size_cap_usd("WSSS", "paper") is None


def test_override_sizes_as_paper_and_never_touches_the_dict(monkeypatch):
    reads = []
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: reads.append(kw) or [])

    as_paper = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15, execution_mode="paper")
    assert executor.EXECUTION_MODE == {"WSSS": "live"}
    as_live = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)

    assert as_live.recommended_size_usd == pytest.approx(config.LIVE_TRADE_SIZE_USD)
    assert as_paper.recommended_size_usd > config.LIVE_TRADE_SIZE_USD
    assert as_paper.recommended_size_usd == pytest.approx(as_paper.kelly_size_preclamp_usd)
    # The paper override reads the PAPER book for the cap/cooldown counts.
    paper_reads = [kw["is_paper"] for kw in reads[:2]]
    assert paper_reads == [True, True]


def test_override_equals_actually_being_in_that_mode(monkeypatch):
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    via_override = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15, execution_mode="paper")
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    via_dict = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert dataclasses.asdict(via_override) == dataclasses.asdict(via_dict)


def test_decide_portfolio_entries_threads_the_override(monkeypatch):
    reads = []
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: reads.append(kw) or [])
    monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.1, 20, 0.1))
    monkeypatch.setattr(entry_manager, "resolution_obs_count", lambda icao: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY)
    monkeypatch.setattr(entry_manager, "forecast_bias_source_mix", lambda icao: None)
    monkeypatch.setattr(entry_manager, "station_error_width_ratio", lambda icao: None)
    token_map = {32: {"yes_token_id": "y", "no_token_id": "n"}}

    decisions = entry_manager.decide_portfolio_entries([_ev()], token_map, min_net_ev=0.15, execution_mode="paper")

    assert decisions[0].approved and decisions[0].recommended_size_usd > config.LIVE_TRADE_SIZE_USD
    assert all(kw.get("is_paper") is True for kw in reads)
    assert executor.EXECUTION_MODE == {"WSSS": "live"}


# --- ev_engine.reprice_for_mode ------------------------------------------

def _estimate():
    return CalibratedEstimate(station_icao="WSSS", target_date=date(2026, 9, 17),
                              central_estimate_c=32.0, std_dev_c=1.0, monsoon_phase="southwest",
                              spread_source="ensemble")


def _token_map():
    return {b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in (31, 32, 33)}


def _quotes():
    return {
        31: MarketQuote(bucket_c=31, yes_price=0.20, no_price=0.81, yes_bid=0.19, no_bid=0.80),
        32: MarketQuote(bucket_c=32, yes_price=0.35, no_price=0.66, yes_bid=0.33, no_bid=0.64),
        33: MarketQuote(bucket_c=33, yes_price=None, no_price=None),   # unpriced bucket
    }


def _probs():
    return {31: 0.25, 32: 0.55, 33: 0.20}


def test_reprice_for_mode_equals_compute_ev_table_in_that_mode():
    live = ev_engine.compute_ev_table(_estimate(), _token_map(), quotes=_quotes(),
                                      model_probs=_probs(), execution_mode="live")
    paper = ev_engine.compute_ev_table(_estimate(), _token_map(), quotes=_quotes(),
                                       model_probs=_probs(), execution_mode="paper")

    repriced = ev_engine.reprice_for_mode(live, "paper")

    assert [dataclasses.asdict(r) for r in repriced] == [dataclasses.asdict(r) for r in paper]
    # And it really changed something: live pays an exit fee, paper does not.
    priced = [r for r in live if r.market_price is not None]
    assert all(r.expected_exit_fee_pct > 0 for r in priced)
    assert all(r.expected_exit_fee_pct == 0.0 for r in repriced if r.market_price is not None)


def test_reprice_is_pure_and_leaves_the_input_alone():
    live = ev_engine.compute_ev_table(_estimate(), _token_map(), quotes=_quotes(),
                                      model_probs=_probs(), execution_mode="live")
    before = [dataclasses.asdict(r) for r in live]
    ev_engine.reprice_for_mode(live, "paper")
    assert [dataclasses.asdict(r) for r in live] == before


def test_reprice_round_trips():
    live = ev_engine.compute_ev_table(_estimate(), _token_map(), quotes=_quotes(),
                                      model_probs=_probs(), execution_mode="live")
    back = ev_engine.reprice_for_mode(ev_engine.reprice_for_mode(live, "paper"), "live")
    assert [dataclasses.asdict(r) for r in back] == [dataclasses.asdict(r) for r in live]
