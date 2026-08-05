"""
tests/test_depth_capture.py

Regression tests for the depth-capture gap (found 2026-08-05): the
piggyback snapshot capture in ev_engine stored depth from
MarketQuote.yes/no_depth_usd, which the quote path never populates --
so every captured row had depth NULL and the backtest's observed_median
depth regime could never become viable. The hook now fetches real book
depth via market_client.get_available_depth_usd on every Nth capture
pass (depth passes), fail-soft per token.
"""

from datetime import date

import ev_engine
from clients import market_client
from models import MarketQuote

import backtest.price_store as price_store


TOKEN_MAP = {32: {"yes_token_id": "tok-yes", "no_token_id": "tok-no"}}
QUOTES = {32: MarketQuote(bucket_c=32, yes_price=0.30, no_price=0.70)}


def _run_captures(monkeypatch, n_passes, depth_fn):
    saved = []
    monkeypatch.setattr(ev_engine, "_capture_pass_count", 0)
    monkeypatch.setattr(price_store, "upsert_token", lambda **kw: None)
    monkeypatch.setattr(price_store, "save_snapshot", lambda **kw: saved.append(kw))
    monkeypatch.setattr(market_client, "get_available_depth_usd", depth_fn)
    for _ in range(n_passes):
        ev_engine._capture_snapshots("WSSS", date(2026, 8, 5), TOKEN_MAP, QUOTES)
    return saved


def test_depth_fetched_only_on_depth_passes(monkeypatch):
    calls = []

    def fake_depth(token_id, **kw):
        calls.append(token_id)
        return 480.0

    saved = _run_captures(monkeypatch, ev_engine.DEPTH_CAPTURE_EVERY_N_PASSES + 1, fake_depth)

    # N+1 passes -> depth passes at index 0 and N only: 2 passes x 2 sides.
    assert len(calls) == 4
    # Every row on a depth pass carries the fetched depth; rows between are NULL.
    per_pass = [saved[i : i + 2] for i in range(0, len(saved), 2)]
    assert all(r["depth_usd"] == 480.0 for r in per_pass[0])
    assert all(r["depth_usd"] is None for pass_rows in per_pass[1:-1] for r in pass_rows)
    assert all(r["depth_usd"] == 480.0 for r in per_pass[-1])
    # Prices still captured on every pass.
    assert len(saved) == (ev_engine.DEPTH_CAPTURE_EVERY_N_PASSES + 1) * 2


def test_depth_fetch_failure_never_breaks_capture(monkeypatch):
    def broken_depth(token_id, **kw):
        raise RuntimeError("book endpoint down")

    saved = _run_captures(monkeypatch, 1, broken_depth)

    # Depth pass with a dead book endpoint: prices still saved, depth NULL.
    assert len(saved) == 2
    assert all(r["depth_usd"] is None for r in saved)
