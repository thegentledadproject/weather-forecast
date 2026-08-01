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
  - get_token_price(): read-only price lookup for ONE outcome token,
    used both for entry EV calculation (ev_engine.py) and for exit
    monitoring (position_manager.py). get_current_price_for_side() is a
    thin wrapper over it. There is deliberately NO two-sided "fetch the
    whole bucket" helper: a caller wanting both sides asks for both token
    ids, so the code never has a place where one side could be inferred
    from the other. (The previous two-sided helper also could not have
    caught a re-introduced inversion -- a NO price derived as `1 - yes`
    sums to exactly 1.00 and sails through any yes+no consistency check.
    The real guards are structural: each side is fetched from its own
    token id, and config.MAX_PLAUSIBLE_RAW_EDGE vetoes the absurd edges a
    price error produces.)
  - This module does NOT place orders -- that's executor.py's job.
    Keeping price-reading and order-placement in different modules
    means a bug here can't accidentally cause a bad trade.

NEVER DERIVE ONE SIDE'S PRICE FROM THE OTHER'S
----------------------------------------------
YES and NO on a Polymarket bucket are SEPARATELY QUOTED tokens linked
by the NegRisk conversion mechanism -- they are not required to sum to
exactly $1, and neither is derivable from the other. An earlier version
of this module fetched one token, labelled it "yes_price", and returned
`1 - price` as the NO price. Callers were already passing each side's
OWN token id, so every NO position was recorded at `1 - reality`, which
manufactured "edges" of 0.88 and EVs of +1298% that sailed straight
through the entry gates into real money. Every function below fetches
each token id it is given, directly, and returns exactly what the book
said for that token. If a price is missing, callers get None -- never
an inferred substitute.

IMPLEMENTATION NOTE
--------------------
Polymarket's public CLOB REST API is the intended real backing for
this client. The endpoint/shape below is written against Polymarket's
documented CLOB API structure but has NOT been exercised against a
live pull in this environment (network to Polymarket's API was not
available for testing here) -- treat get_token_price() as needing
a real smoke-test against production before position_manager.py is
trusted with real capital. Fails soft (returns None) so callers
degrade gracefully rather than crash.

DEPENDENCIES
------------
requests   (pip install requests)
models.py (local)
"""

from datetime import datetime, timezone
from typing import Optional

import requests

CLOB_API_BASE = "https://clob.polymarket.com"
CLOB_BOOK_ENDPOINT = f"{CLOB_API_BASE}/book"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_token_price(token_id: str, timeout: int = 10) -> Optional[float]:
    """
    Fetch the current CLOB price for ONE outcome token -- the single
    primitive every other price lookup in this module is built on.

    The price returned is the price of the token id passed in, exactly
    as quoted. No complement arithmetic, no assumption about whether
    this token is the YES or the NO side of its bucket: that is the
    caller's business, and the token id already encodes it.

    Returns None on any failure (network, HTTP error, missing/unparseable
    "price" field) so callers fail soft rather than act on a guess.
    """
    try:
        resp = requests.get(
            f"{CLOB_API_BASE}/price",
            params={"token_id": token_id, "side": "buy"},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        return float(payload.get("price"))
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"[market_client] get_token_price failed for token {token_id}: {exc}")
        return None


def get_current_price_for_side(token_id: str, side: str) -> Optional[float]:
    """
    The current live price for one Position's own side, or None on failure.
    This is the single call position_manager.py needs per open position.

    CONTRACT: `token_id` IS that side's own token -- the NO token for a
    NO position, the YES token for a YES position. It is NOT a canonical
    YES token from which the other side gets derived. YES and NO are
    independently quoted (NegRisk), so `1 - yes_price` is NOT the NO
    price; deriving it that way is exactly the bug that recorded every
    NO position at `1 - reality`.

    `side` is kept purely for call-site signature compatibility and for
    the failure log message -- it never changes the value returned.
    """
    price = get_token_price(token_id)
    if price is None:
        print(f"[market_client] no live {side.upper()} price available for token {token_id} this cycle")
    return price


def get_order_book(market_token_id: str, timeout: int = 10) -> Optional[dict]:
    """
    Fetch the full order book (bids/asks with sizes) for one outcome
    token. Returns Polymarket's raw book shape -- {"bids": [...],
    "asks": [...]} with each entry roughly {"price": ..., "size": ...}
    -- or None on failure. Used by estimate_slippage() to model the
    real cost of a trade rather than assuming a flat percentage.

    NOTE: like get_token_price, this is written against Polymarket's
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
