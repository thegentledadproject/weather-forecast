"""
tests/test_honest_fills.py

Honest fills (plan P6/P7, 2026-09-24 gap audit):

  1. an entry takes its ask, depth and slippage from ONE /book snapshot and
     refuses books that are crossed, empty on the ask side, stale, or whose
     ask has moved against the priced one.
"""
import time
from datetime import date

import pytest

import config
import entry_manager
from clients import market_client
from clients.market_client import get_entry_book as real_get_entry_book  # before conftest's shim
from models import EVResult


def _book(asks, bids=(), ts=None):
    book = {
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
    }
    if ts is not None:
        book["timestamp"] = str(int(ts * 1000))
    return book


# Trimmed from a real /book captured 2026-09-24 (KBKF 72F NO): asks come back
# best-LAST, bids best-last, prices and sizes as strings, ms timestamp.
REAL_SHAPE = {
    "market": "0x29be57d3", "asset_id": "114445568125",
    "timestamp": "1790265943981", "hash": "25d0fa90",
    "bids": [{"price": "0.01", "size": "3181.31"}, {"price": "0.68", "size": "20"},
             {"price": "0.69", "size": "5"}],
    "asks": [{"price": "0.99", "size": "167"}, {"price": "0.8", "size": "54.99"},
             {"price": "0.76", "size": "25"}, {"price": "0.75", "size": "5"}],
    "min_order_size": "5", "tick_size": "0.01", "neg_risk": True,
}


@pytest.fixture
def one_book(monkeypatch):
    """Patch get_order_book to serve `holder['book']` and count the fetches."""
    holder = {"book": None, "calls": 0}

    def _fetch(token_id, timeout=10):
        holder["calls"] += 1
        return holder["book"]

    monkeypatch.setattr(market_client, "get_order_book", _fetch)
    monkeypatch.setattr(market_client, "get_entry_book", real_get_entry_book)
    return holder


def test_real_shape_parses_to_best_ask_depth_and_walk(one_book):
    one_book["book"] = dict(REAL_SHAPE, timestamp=str(int(time.time() * 1000)))
    snap = market_client.get_entry_book("tok")
    assert snap.refusal is None
    assert snap.best_ask == pytest.approx(0.75)
    # within 10% of 0.75 (<= 0.825): 5@0.75 + 25@0.76 + 54.99@0.80
    assert snap.depth_usd() == pytest.approx(5 * 0.75 + 25 * 0.76 + 54.99 * 0.80)
    # $10 walks 5@0.75 ($3.75) then $6.25 at 0.76
    shares = 5 + 6.25 / 0.76
    assert snap.slippage(10.0) == pytest.approx((10.0 / shares - 0.75) / 0.75)
    assert one_book["calls"] == 1


def test_book_timestamp_is_read_in_seconds():
    assert market_client.book_timestamp(REAL_SHAPE) == pytest.approx(1790265943.981)
    assert market_client.book_timestamp({}) is None


@pytest.mark.parametrize("book,code", [
    (None, "no_book"),
    (_book(asks=[], bids=[(0.40, 10)]), "empty_ask"),
    (_book(asks=[(0.40, 10)], bids=[(0.40, 10)]), "crossed"),
    (_book(asks=[(0.40, 10)], bids=[(0.45, 10)]), "crossed"),
])
def test_unusable_books_are_refused_with_a_code(one_book, book, code):
    one_book["book"] = book
    snap = market_client.get_entry_book("tok")
    assert snap.refusal == code
    assert snap.depth_usd() is None


def test_a_book_older_than_the_threshold_is_stale(one_book):
    one_book["book"] = _book(asks=[(0.40, 100)], bids=[(0.38, 100)],
                             ts=time.time() - config.BOOK_MAX_AGE_S - 5)
    assert market_client.get_entry_book("tok").refusal == "stale"


def test_a_fresh_book_passes_and_no_timestamp_falls_back_to_fetch_time(one_book):
    one_book["book"] = _book(asks=[(0.40, 100)], bids=[(0.38, 100)], ts=time.time() - 60)
    assert market_client.get_entry_book("tok").refusal is None
    one_book["book"] = _book(asks=[(0.40, 100)], bids=[(0.38, 100)])  # no timestamp
    assert market_client.get_entry_book("tok").refusal is None
    # ...and the fetch-time fallback does age it
    assert market_client.book_quality_problem(
        _book(asks=[(0.40, 100)]), fetched_at=0.0, now=config.BOOK_MAX_AGE_S + 1
    ) == "stale"


def test_an_ask_worse_than_the_priced_one_is_refused_a_better_one_is_not(one_book):
    one_book["book"] = _book(asks=[(0.42, 100)], ts=time.time())
    assert market_client.get_entry_book("tok", decided_ask=0.40).refusal == "ask_moved"
    assert market_client.get_entry_book("tok", decided_ask=0.42).refusal is None
    assert market_client.get_entry_book("tok", decided_ask=0.45).refusal is None


def test_the_entry_book_fallback_matches_the_literal_the_backtest_reads():
    from backtest import fill_model
    assert market_client._FALLBACK_SLIPPAGE_PCT == fill_model.FALLBACK_SLIPPAGE_PCT


def _ev(price=0.30, model_prob=0.432):
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 8, 20), bucket_c=32, side="YES",
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.0, fee_rate_pct=0.0,
        net_ev_per_dollar=(model_prob - price) / price,
    )


def _no_legacy_calls(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("entry path must not fetch a second book")
    monkeypatch.setattr(market_client, "get_available_depth_usd", _boom)
    monkeypatch.setattr(market_client, "estimate_slippage", _boom)


def test_evaluate_entry_sizes_off_one_snapshot(one_book, monkeypatch):
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)
    _no_legacy_calls(monkeypatch)
    one_book["book"] = _book(asks=[(0.30, 100_000)], bids=[(0.29, 100)], ts=time.time())

    decision = entry_manager.evaluate_entry(_ev(), token_id="tok", min_net_ev=-9.0)

    assert one_book["calls"] == 1
    assert decision.available_depth_usd == pytest.approx(30_000.0)
    assert decision.slippage_at_size_pct == pytest.approx(0.0)


def test_evaluate_entry_refuses_a_crossed_book_under_the_depth_rule(one_book, monkeypatch):
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)
    _no_legacy_calls(monkeypatch)
    one_book["book"] = _book(asks=[(0.30, 1000)], bids=[(0.31, 100)], ts=time.time())

    decision = entry_manager.evaluate_entry(_ev(), token_id="tok", min_net_ev=-9.0)

    assert not decision.approved
    assert decision.rule_id == "depth"
    assert "crossed" in decision.reason


def test_evaluate_entry_refuses_when_the_ask_moved_up(one_book, monkeypatch):
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)
    _no_legacy_calls(monkeypatch)
    one_book["book"] = _book(asks=[(0.33, 1000)], bids=[(0.29, 100)], ts=time.time())

    decision = entry_manager.evaluate_entry(_ev(price=0.30), token_id="tok", min_net_ev=-9.0)

    assert not decision.approved
    assert "ask_moved" in decision.reason


def test_exit_side_depth_warns_on_a_crossed_book_but_still_answers(one_book, capsys):
    one_book["book"] = _book(asks=[(0.30, 10)], bids=[(0.31, 100)], ts=time.time())
    assert market_client.get_bid_depth_usd("tok") == pytest.approx(31.0)
    assert "crossed" in capsys.readouterr().out


def test_executor_refuses_a_crossed_book_at_submission_with_a_code(one_book):
    import executor
    from types import SimpleNamespace
    one_book["book"] = _book(asks=[(0.30, 1000)], bids=[(0.31, 100)], ts=time.time())
    spec = SimpleNamespace(notional_usd=1.0, pad_cost_pct=0.0)
    decision = SimpleNamespace(recommended_size_usd=1.0, token_id="tok", entry_price=0.30,
                               available_depth_usd=300.0, slippage_at_size_pct=0.0,
                               net_ev_at_size=0.2, min_net_ev=0.1)
    out = {}
    ok, note = executor._resolved_size_ok(spec, decision, out=out)
    assert not ok
    assert out["refusal_code"] == "resolved_book_crossed"
