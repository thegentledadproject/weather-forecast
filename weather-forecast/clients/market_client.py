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
  - Read-only quote lookup for ONE outcome token. There is deliberately
    NO two-sided "fetch the whole bucket" helper: a caller wanting both
    sides asks for both token ids, so the code never has a place where
    one side could be inferred from the other. (The previous two-sided
    helper also could not have caught a re-introduced inversion -- a NO
    price derived as `1 - yes` sums to exactly 1.00 and sails through any
    yes+no consistency check. The real guards are structural: each side
    is fetched from its own token id, and config.MAX_PLAUSIBLE_RAW_EDGE
    vetoes the absurd edges a price error produces.)
  - This module does NOT place orders -- that's executor.py's job.
    Keeping price-reading and order-placement in different modules
    means a bug here can't accidentally cause a bad trade.

WHICH SIDE OF THE BOOK: ENTRIES PAY THE ASK, EXITS RECEIVE THE BID
-------------------------------------------------------------------
There is no such thing as "the price" of a token, and this module used to
pretend there was. get_token_price() returned the BID under a neutral
name, and BOTH the entry funnel and the exit monitor were built on it --
so entries were valued at a price they could not get, by the width of the
spread, while sizing and slippage were already being computed off the
ASKS by get_available_depth_usd()/estimate_slippage(). It has been
replaced by four explicitly-named functions:

    get_token_bid()             raw best bid   -- what a sale receives
    get_token_ask()             raw best ask   -- what a purchase pays
    get_entry_price_for_side()  the ask, for entry EV/sizing/limit price
    get_current_price_for_side() the bid, for marking an open position

Pick by what the caller is about to do, never by which one is available.
The API's own `side` parameter is the opposite of what it sounds like
(`side=buy` returns the BID); that inversion is now confined to
_fetch_quote() and documented on both wrappers.

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
Polymarket's public CLOB REST API is the real backing for this client.
The /price endpoint and its bid/ask semantics WERE exercised against
production on 2026-08-10: on a book with max bid 0.179 and min ask 0.180,
/price?side=buy returned 0.179 and /price?side=sell returned 0.180. The
same pull confirmed /book carries min_order_size (5 shares) and a
per-token tick_size (0.001 on that market, not the 0.01 base).

Still unexercised against production: the /book parsing in
get_available_depth_usd()/estimate_slippage() below, whose bid/ask level
field names are written against the documented shape. Everything here
fails soft (returns None) so callers degrade gracefully rather than crash.

DEPENDENCIES
------------
requests   (pip install requests)
models.py (local)
"""

from datetime import datetime, timezone
from typing import Optional

import requests

import config

CLOB_API_BASE = "https://clob.polymarket.com"
CLOB_BOOK_ENDPOINT = f"{CLOB_API_BASE}/book"

# Token ids CLOB has answered 404 ("no orderbook exists") for, so each
# unseeded far-tail bucket is logged once per process rather than once
# per scan cycle. In-memory on purpose: a token can gain an orderbook
# any time, and a restart re-checking them all is correct behavior.
_no_orderbook_seen = set()

# Tokens a ghost book has been reported for, so a market stuck in that state
# is logged once rather than on every fetch of every scan cycle. In-memory on
# purpose: the snapshot clears on its own, and a restart re-checking is right.
_ghost_book_seen = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_quote(token_id: str, book_side: str, timeout: int = 10) -> Optional[float]:
    """
    One side of the book for ONE outcome token. The shared primitive under
    get_token_bid() and get_token_ask().

    `book_side` is the API's own `side` parameter, and its meaning is the
    opposite of what the word suggests -- see those two wrappers. Callers
    must not pass it directly; use the named wrappers, which is the whole
    point of making this private.

    The value returned is the quote for the token id passed in, exactly as
    the book gave it. No complement arithmetic, no assumption about whether
    this token is the YES or the NO side of its bucket: that is the
    caller's business, and the token id already encodes it.

    Returns None on any failure (network, HTTP error, missing/unparseable
    "price" field) so callers fail soft rather than act on a guess.

    A 404 is a KNOWN market condition, not an error: CLOB answers
    "No orderbook exists for the requested token id" for bucket markets
    that are listed on Gamma but were never seeded with orders -- the
    far-tail temperature buckets sit in this state all day, every day
    (confirmed against the live API 2026-08-02). Those are logged once
    per token per process instead of once per cycle, so 20 dead tails
    don't bury one real failure in the journal.
    """
    try:
        resp = requests.get(
            f"{CLOB_API_BASE}/price",
            params={"token_id": token_id, "side": book_side},
            timeout=timeout,
        )
        if resp.status_code == 404:
            if token_id not in _no_orderbook_seen:
                _no_orderbook_seen.add(token_id)
                print(
                    f"[market_client] no orderbook for token {token_id} -- listed but unseeded "
                    f"(normal for far-tail buckets); treating as no-quote, not logging again."
                )
            return None
        resp.raise_for_status()
        payload = resp.json()
        return float(payload.get("price"))
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"[market_client] {book_side}-side quote failed for token {token_id}: {exc}")
        return None


def get_token_bid(token_id: str, timeout: int = 10) -> Optional[float]:
    """
    The best BID: the highest price anyone is currently offering to pay.
    This is what you RECEIVE if you sell, so it is the correct price for
    marking an open position and for every exit decision.

    NOTE THE API PARAMETER. `side=buy` returns the BID, not the ask --
    `side` names the side of the BOOK being read (the buy orders), not
    what the caller intends to do. Verified against the live API
    2026-08-10: on a book with max bid 0.179 and min ask 0.180,
    /price?side=buy returned 0.179 and /price?side=sell returned 0.180.
    """
    return _fetch_quote(token_id, "buy", timeout=timeout)


def get_token_ask(token_id: str, timeout: int = 10) -> Optional[float]:
    """
    The best ASK: the lowest price anyone is currently willing to sell at.
    This is what you PAY to buy, so it is the correct price for entry EV,
    entry sizing, and the limit price on an entry order.

    See get_token_bid() for why this passes `side=sell`.
    """
    return _fetch_quote(token_id, "sell", timeout=timeout)


def get_entry_price_for_side(token_id: str, side: str) -> Optional[float]:
    """
    The price an ENTRY on this token would actually pay -- the ask.

    THIS IS THE FIX FOR A REAL, MEASURED MISPRICING.
    -------------------------------------------------
    Every entry-side price in this system used to come from the BID. The
    bid became EVResult.market_price, then raw_edge = model_prob - price,
    then EntryDecision.entry_price, then the limit price on the order --
    while a buy actually fills at the ASK. The entire entry funnel was
    valuing trades at a price it could not get.

    The error is the spread, and the spread is not small relative to the
    bar it was crossing: on the live WSSS book (bid 0.29 / ask 0.31) that
    is 0.02 of overstated edge against a MIN_ABS_RAW_EDGE of 0.03 -- two
    thirds of the minimum edge the system demands was spread it never
    modelled. Worse, the module was already internally inconsistent about
    it: get_available_depth_usd() and estimate_slippage() have always
    walked the ASKS, so sizing and slippage were computed against one side
    of the book and the edge against the other.

    EXPECT THE APPROVAL RATE TO DROP once this is live. That is the
    correction working, not a regression -- those trades were being
    approved on an edge that did not exist.

    CONTRACT: `token_id` IS that side's own token -- the NO token for a NO
    entry, the YES token for a YES entry. It is NOT a canonical YES token
    from which the other side gets derived. YES and NO are independently
    quoted (NegRisk), so `1 - yes_price` is NOT the NO price; deriving it
    that way is exactly the bug that recorded every NO position at
    `1 - reality`.

    `side` is used only for the failure log message -- it never changes
    which value is returned.
    """
    price = get_token_ask(token_id)
    if price is None:
        print(f"[market_client] no live {side.upper()} ask available for token {token_id} this cycle")
    return price


def get_current_price_for_side(token_id: str, side: str) -> Optional[float]:
    """
    The current live price of one OPEN Position's own side -- the bid.
    This is the single call position_manager.py needs per open position.

    DELIBERATELY THE BID, AND ALREADY CORRECT. An open position is marked
    at what it could be sold for, which is the bid; using the ask here
    would inflate every unrealized P&L by the spread and would make the
    stop-loss and take-profit fire off a price the position cannot
    actually realize. Entries use get_entry_price_for_side() instead --
    the two sides of the book are not interchangeable, which is the whole
    reason they are now two separate functions.

    CONTRACT on `token_id`: identical to get_entry_price_for_side() --
    that side's own token, never derived from the other side.

    `side` is used only for the failure log message.
    """
    price = get_token_bid(token_id)
    if price is None:
        print(f"[market_client] no live {side.upper()} bid available for token {token_id} this cycle")
    return price


def get_bid_depth_usd(market_token_id: str, max_price_impact_pct: float = 0.10, timeout: int = 10) -> Optional[float]:
    """
    Sum the dollar value of BID-side liquidity available before price impact
    exceeds max_price_impact_pct BELOW the top-of-book bid. What a SALE can
    realistically clear into.

    THE MIRROR OF get_available_depth_usd(), AND THE DISTINCTION IS THE POINT.
    That function sums the ASKS, because it sizes an ENTRY, which buys. An
    exit sells, and the two sides of a thin prediction-market book are not
    interchangeable -- the same token can carry a $10,000 ask stack and a $5
    bid. Using the ask figure to model a sale would overstate every modelled
    exit, in the direction that flatters it.

    Impact runs DOWNWARD here (top_bid * (1 - impact)) where the ask-side
    function runs upward, for the same reason: a seller's price moves against
    them as they consume bids.

    Returns None if the book is unavailable or carries no bids -- "unknown
    depth", never "zero depth". price_store records that as NULL and
    fill_model charges the live fallback slippage for it, exactly as it does
    for an unknown ask book.
    """
    book = get_order_book(market_token_id, timeout=timeout)
    if not book or "bids" not in book or not book["bids"]:
        return None

    try:
        bids = sorted(book["bids"], key=lambda level: float(level["price"]), reverse=True)
        top_price = float(bids[0]["price"])
        impact_floor = top_price * (1 - max_price_impact_pct)

        depth_usd = 0.0
        for level in bids:
            price = float(level["price"])
            if price < impact_floor:
                break
            depth_usd += float(level["size"]) * price
        return depth_usd
    except (KeyError, ValueError) as exc:
        print(f"[market_client] get_bid_depth_usd parse failed for token {market_token_id}: {exc}")
        return None


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
        book = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[market_client] get_order_book failed for token {market_token_id}: {exc}")
        return None

    # A ghost book is treated exactly like a failed fetch, and the check
    # lives HERE rather than in each caller so depth, slippage and
    # wallet_client's tick/minimum lookup are all covered by one guard.
    # Everything downstream already handles None as "unusable", so this
    # fails closed: an entry refuses to size, an order refuses to build.
    if is_ghost_book(book):
        if market_token_id not in _ghost_book_seen:
            _ghost_book_seen.add(market_token_id)
            print(
                f"[market_client] GHOST BOOK for token {market_token_id}: both sides "
                f"pinned at the extremes, which is a stale snapshot, not a market "
                f"(py-clob-client issue #180). Treating as no book. Logged once per "
                f"token per process."
            )
        return None
    return book


def is_ghost_book(book: Optional[dict]) -> bool:
    """
    Whether this book is the stale "ghost" snapshot rather than a market.

    See config.GHOST_BOOK_BID_MAX for the upstream bug. True only when BOTH
    sides are pinned at their extremes at the same time -- a real far-tail
    bucket (bid 0.000 / ask 0.001) and a real near-resolved one (bid 0.998 /
    ask 1.000) each trip one bound and never both.

    A book missing a side entirely is NOT a ghost. That is an ordinary thin
    market, already handled as zero depth by the callers, and calling it a
    ghost would suppress the far-tail buckets this system legitimately
    quotes every day.
    """
    if not book:
        return False
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return False
    try:
        best_bid = max(float(b["price"]) for b in bids)
        best_ask = min(float(a["price"]) for a in asks)
    except (KeyError, TypeError, ValueError):
        return False
    return best_bid <= config.GHOST_BOOK_BID_MAX and best_ask >= config.GHOST_BOOK_ASK_MIN


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
