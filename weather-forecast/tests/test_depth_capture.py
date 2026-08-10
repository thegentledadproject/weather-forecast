"""
tests/test_depth_capture.py

Regression tests for the depth-capture gap (found 2026-08-05): the
piggyback snapshot capture in ev_engine stored depth from
MarketQuote.yes/no_depth_usd, which the quote path never populates --
so every captured row had depth NULL and the backtest's observed_median
depth regime could never become viable. The hook now fetches real book
depth via market_client.get_available_depth_usd on every Nth capture
pass (depth passes), fail-soft per token.

The pass counter is keyed PER STATION (_capture_pass_count_by_station),
not a single shared global -- with 13 registered stations, one shared
counter would rotate depth capture ACROSS stations instead of counting
each station's own Nth pass, dropping per-token depth coverage far
below backtest MIN_DEPTH_COVERAGE=0.5 (see ev_engine.py's
DEPTH_CAPTURE_EVERY_N_PASSES docstring). test_two_stations_rotate_independently
covers that directly.
"""

from datetime import date

import ev_engine
from clients import market_client
from models import MarketQuote

import backtest.price_store as price_store


TOKEN_MAP = {32: {"yes_token_id": "tok-yes", "no_token_id": "tok-no"}}
# yes_price/no_price are ASKS (what an entry pays); yes_bid/no_bid are the
# other side. Capture stores the bid in `price` and the ask in `ask_price`,
# so a quote with no bid has nothing to put in the NOT NULL price column and
# is skipped -- which is why this fixture has to carry both.
QUOTES = {
    32: MarketQuote(
        bucket_c=32, yes_price=0.31, no_price=0.71, yes_bid=0.30, no_bid=0.70,
    )
}


def _run_captures(monkeypatch, n_passes, depth_fn):
    saved = []
    monkeypatch.setattr(ev_engine, "_capture_pass_count_by_station", {})
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


def test_two_stations_rotate_independently(monkeypatch):
    """
    A shared global counter would treat "pass 0 for WSSS, pass 1 for
    WMKK" as passes 0 and 1 of one rotation -- only one of the two
    stations would ever land on a depth pass. Per-station counters mean
    EVERY station gets its own depth pass on ITS OWN first (and every
    Nth) capture, regardless of what other stations did in between.
    """
    saved = []
    calls = []
    monkeypatch.setattr(ev_engine, "_capture_pass_count_by_station", {})
    monkeypatch.setattr(price_store, "upsert_token", lambda **kw: None)
    monkeypatch.setattr(price_store, "save_snapshot", lambda **kw: saved.append(kw))
    monkeypatch.setattr(
        market_client, "get_available_depth_usd",
        lambda token_id, **kw: (calls.append(token_id), 480.0)[1],
    )

    # Interleave: WSSS pass 0, WMKK pass 0, WSSS pass 1, WMKK pass 1.
    # Both stations' pass 0 is a depth pass under per-station counting.
    ev_engine._capture_snapshots("WSSS", date(2026, 8, 5), TOKEN_MAP, QUOTES)
    ev_engine._capture_snapshots("WMKK", date(2026, 8, 5), TOKEN_MAP, QUOTES)
    ev_engine._capture_snapshots("WSSS", date(2026, 8, 5), TOKEN_MAP, QUOTES)
    ev_engine._capture_snapshots("WMKK", date(2026, 8, 5), TOKEN_MAP, QUOTES)

    # Each station's own pass 0 (2 sides) fetched depth -> 4 calls total,
    # not the 2 a shared/rotating counter would produce.
    assert len(calls) == 4
    assert ev_engine._capture_pass_count_by_station == {"WSSS": 2, "WMKK": 2}
