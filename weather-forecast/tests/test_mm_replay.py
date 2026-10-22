"""
Three no-network tests on hand-built rows for mm_replay.py's pure pieces:
touch-vs-through fills, the METAR pull window, and settlement P&L sign.
"""
from datetime import datetime, timedelta

import mm_replay


def _row(seen_at, bid, ask, trade=None):
    return {"seen_at": seen_at, "dt": datetime.fromisoformat(seen_at), "best_bid": bid, "best_ask": ask, "last_trade": trade}


def test_touch_no_fill_through_fills():
    # mid = 0.50, half_spread = 0.02 -> our_bid = 0.48, our_ask = 0.52.
    # State 2 here just touches our_bid (best_ask == 0.48): no fill.
    rows_touch = [
        _row("2026-09-28T00:00:00+00:00", 0.49, 0.51),
        _row("2026-09-28T00:00:15+00:00", 0.47, 0.48),
    ]
    fills = mm_replay.simulate(rows_touch, [], half_spread=0.02, pull_s=0)
    assert fills == []

    # State 2 trades THROUGH our_bid (best_ask == 0.47 < 0.48): fills.
    rows_through = [
        _row("2026-09-28T00:00:00+00:00", 0.49, 0.51),
        _row("2026-09-28T00:00:15+00:00", 0.46, 0.47),
    ]
    fills = mm_replay.simulate(rows_through, [], half_spread=0.02, pull_s=0)
    assert len(fills) == 1
    assert fills[0]["side"] == "buy"
    assert fills[0]["price"] == 0.48
    assert fills[0]["shares"] == mm_replay.QUOTE_SHARES


def test_pull_window_blocks_fill():
    rows_through = [
        _row("2026-09-28T00:00:00+00:00", 0.49, 0.51),
        _row("2026-09-28T00:00:15+00:00", 0.46, 0.47),
    ]
    metar_first_seen = [datetime.fromisoformat("2026-09-28T00:00:00+00:00")]
    # 15s after the METAR landed, well inside a 120s pull window.
    fills = mm_replay.simulate(rows_through, metar_first_seen, half_spread=0.02, pull_s=120)
    assert fills == []

    # Same book, pull window disabled -> fills again.
    fills = mm_replay.simulate(rows_through, metar_first_seen, half_spread=0.02, pull_s=0)
    assert len(fills) == 1

    # METAR lands BETWEEN two book rows 20 min apart (change-only rows):
    # the quote was pulled before the through-print, so no fill.
    rows_gap = [
        _row("2026-09-28T00:00:00+00:00", 0.49, 0.51),
        _row("2026-09-28T00:20:00+00:00", 0.46, 0.47),
    ]
    between = [datetime.fromisoformat("2026-09-28T00:10:00+00:00")]
    assert mm_replay.simulate(rows_gap, between, half_spread=0.02, pull_s=120) == []


def test_settlement_pnl_sign_on_winning_buy():
    rows = [_row("2026-09-28T00:00:00+00:00", 0.40, 0.42)]
    fill = {
        "dt": datetime.fromisoformat("2026-09-28T00:00:00+00:00"), "side": "buy",
        "price": 0.41, "shares": 10, "icao": "WSSS", "target_date": "2026-09-28",
        "bucket": 30, "_rows": rows, "_dts": [r["dt"] for r in rows],
    }
    settled = {("WSSS", "2026-09-28"): 30}  # bucket 30 won
    result = mm_replay.score([fill], settled)
    # payoff 1.0, bought at 0.41, 10 shares -> +5.90
    assert abs(result["settlement_pnl"] - 5.9) < 1e-9
    assert result["unsettled"] == 0

    # Losing bucket -> negative, same magnitude of stake.
    settled_lose = {("WSSS", "2026-09-28"): 31}
    result_lose = mm_replay.score([fill], settled_lose)
    assert abs(result_lose["settlement_pnl"] - (-4.1)) < 1e-9
