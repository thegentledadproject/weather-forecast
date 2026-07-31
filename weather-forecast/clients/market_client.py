"""
clients/market_client.py

PURPOSE
-------
Pulls live Polymarket prices for a station's temperature-bracket
market -- both YES and NO per bucket, since (per the mechanism
analysis) these are separately-quoted tokens linked by the NegRisk
conversion mechanism, not derived from each other. This is what turns
position_manager.py's exit checks from theoretical into real: profit
and stop-loss decisions are only as good as the live price feed
they're checked against.

Two responsibilities, kept separate from execution:
  - get_bucket_prices(): read-only price lookup, used both for entry
    EV calculation (ev_engine.py, not yet built) and for exit
    monitoring (position_manager.py, this update)
  - This module does NOT place orders -- that's executor.py's job.
    Keeping price-reading and order-placement in different modules
    means a bug here can't accidentally cause a bad trade.

IMPLEMENTATION NOTE
--------------------
Polymarket's public CLOB REST API is the intended real backing for
this client. The endpoint/shape below is written against Polymarket's
documented CLOB API structure but has NOT been exercised against a
live pull in this environment (network to Polymarket's API was not
available for testing here) -- treat get_bucket_prices() as needing
a real smoke-test against production before position_manager.py is
trusted with real capital. Fails soft (returns None) so callers
degrade gracefully rather than crash.

DEPENDENCIES
------------
requests   (pip install requests)
models.py (local)
"""

from datetime import datetime, timezone
from typing import Optional, Dict

import requests

CLOB_API_BASE = "https://clob.polymarket.com"
CLOB_BOOK_ENDPOINT = f"{CLOB_API_BASE}/book"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_bucket_prices(market_token_id: str, timeout: int = 10) -> Optional[Dict[str, float]]:
    """
    Fetch the current best bid/ask (used here as a proxy for YES price)
    for one outcome token. Returns {"yes_price": float, "no_price": float}
    or None on failure.

    market_token_id identifies a single outcome's YES token on
    Polymarket's CLOB -- callers (position_manager.py) look this up per
    open Position from whatever token-id mapping was recorded at entry
    time (executor.py's responsibility to record when a trade is placed).
    """
    try:
        resp = requests.get(
            f"{CLOB_API_BASE}/price",
            params={"token_id": market_token_id, "side": "buy"},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        yes_price = float(payload.get("price"))
        return {"yes_price": yes_price, "no_price": round(1 - yes_price, 4)}
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"[market_client] get_bucket_prices failed for token {market_token_id}: {exc}")
        return None


def get_current_price_for_side(market_token_id: str, side: str) -> Optional[float]:
    """
    Convenience wrapper: returns the current price relevant to a
    specific Position's side ("YES" or "NO"), or None on failure.
    This is the single call position_manager.py needs per open position.
    """
    prices = get_bucket_prices(market_token_id)
    if prices is None:
        return None
    return prices["yes_price"] if side.upper() == "YES" else prices["no_price"]


def get_order_book(market_token_id: str, timeout: int = 10) -> Optional[dict]:
    """
    Fetch the full order book (bids/asks with sizes) for one outcome
    token. Returns Polymarket's raw book shape -- {"bids": [...],
    "asks": [...]} with each entry roughly {"price": ..., "size": ...}
    -- or None on failure. Used by estimate_slippage() to model the
    real cost of a trade rather than assuming a flat percentage.

    NOTE: like get_bucket_prices, this is written against Polymarket's
    documented CLOB API shape but has not been exercised against a
    live pull in this environment. Confirm the exact field names
    against a real response before trusting the parsing below.
    """
    try:
        resp = requests.get(
            CLOB_BOOK_ENDPOINT,
            params={"token_id": market_token_id},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[market_client] get_order_book failed for token {market_token_id}: {exc}")
        return None


def get_available_depth_usd(market_token_id: str, max_price_impact_pct: float = 0.10, timeout: int = 10) -> Optional[float]:
    """
    Sum the dollar value of ask-side liquidity available before price
    impact exceeds max_price_impact_pct above the top-of-book price.
    Used by entry_manager.py to cap position size against real,
    current liquidity rather than sizing purely off the Kelly formula
    and hoping the book can absorb it.

    Returns None if the book is unavailable -- callers should treat
    that as "unknown depth," not "zero depth," and decide accordingly
    (entry_manager.py treats it as a reason to skip the trade rather
    than assume either extreme).
    """
    book = get_order_book(market_token_id, timeout=timeout)
    if not book or "asks" not in book or not book["asks"]:
        return None

    try:
        asks = sorted(book["asks"], key=lambda level: float(level["price"]))
        top_price = float(asks[0]["price"])
        impact_ceiling = top_price * (1 + max_price_impact_pct)

        depth_usd = 0.0
        for level in asks:
            price = float(level["price"])
            if price > impact_ceiling:
                break
            depth_usd += float(level["size"]) * price
        return depth_usd
    except (KeyError, ValueError) as exc:
        print(f"[market_client] get_available_depth_usd parse failed for token {market_token_id}: {exc}")
        return None


def estimate_slippage(market_token_id: str, size_usd: float, timeout: int = 10) -> float:
    """
    Estimate the cost (as a fraction of trade value) of buying
    size_usd worth of an outcome token, by walking the ask side of the
    live order book rather than assuming a flat slippage number.

    Falls back to a conservative flat estimate if the book is
    unavailable or too thin to parse -- this is a real gap (see
    ev_engine.py's notes) and callers should treat the fallback value
    as a rough floor, not a confident estimate.
    """
    FALLBACK_SLIPPAGE_PCT = 0.05  # conservative default when book data is missing

    book = get_order_book(market_token_id, timeout=timeout)
    if not book or "asks" not in book or not book["asks"]:
        return FALLBACK_SLIPPAGE_PCT

    try:
        asks = sorted(book["asks"], key=lambda level: float(level["price"]))
        remaining = size_usd
        cost_accum = 0.0
        shares_accum = 0.0
        top_price = float(asks[0]["price"])

        for level in asks:
            price = float(level["price"])
            level_size_usd = float(level["size"]) * price
            fill_usd = min(remaining, level_size_usd)
            if fill_usd <= 0:
                continue
            shares = fill_usd / price
            cost_accum += fill_usd
            shares_accum += shares
            remaining -= fill_usd
            if remaining <= 0:
                break

        if shares_accum == 0 or remaining > 0:
            # Order size exceeds visible book depth -- can't fill it
            # cleanly, so treat as high-slippage rather than guess.
            return FALLBACK_SLIPPAGE_PCT

        avg_fill_price = cost_accum / shares_accum
        slippage_pct = (avg_fill_price - top_price) / top_price
        return max(slippage_pct, 0.0)
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        print(f"[market_client] estimate_slippage parse failed for token {market_token_id}: {exc}")
        return FALLBACK_SLIPPAGE_PCT
