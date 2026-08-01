"""
ev_engine.py

PURPOSE
-------
Combines Layer 1 (CalibratedEstimate from calibration.py/probability.py)
with Layer 2 (live market prices from market_client.py) into real,
per-bucket EV numbers -- replacing the "naive market" assumption used
in earlier hand-calculated illustrative examples with actual order-book
data.

    net_ev_per_dollar = (model_prob - market_price) / market_price
                         - estimated_slippage_pct - fee_rate_pct

Also runs the book-level dislocation check discussed in the mechanism
analysis: sums YES prices across the full bracket and compares to
$1, with an explicit reminder that a small deviation is expected
friction (NegRisk conversion cost), not automatically exploitable
edge -- see NOTES below.

STATION -> TOKEN-ID MAPPING
---------------------------
Every function here takes an explicit `token_map` argument:

    token_map = {
        27: {"yes_token_id": "...", "no_token_id": "..."},
        28: {"yes_token_id": "...", "no_token_id": "..."},
        ...
    }

Building that mapping (fetching a station's current Polymarket event,
enumerating its per-bucket markets, and recording each outcome's
token_id) is now handled by market_discovery.py, which hits
Polymarket's Gamma API -- run_for_station() below calls
market_discovery.discover_token_map() automatically so callers no
longer need to populate token_map by hand. compute_ev_table() still
accepts a hand-built or cached token_map directly for callers who
already have one.

FEE RATE
--------
DEFAULT_FEE_RATE_PCT is set to 0.0 because Polymarket's current fee
structure was not verified against live documentation in this build.
Confirm the actual fee schedule before trusting net_ev_per_dollar for
a real capital decision -- this default should not be read as "fees
are zero."

DEPENDENCIES
------------
config.py, models.py, probability.py (local)
clients/market_client.py (local)
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from models import CalibratedEstimate, MarketQuote, EVResult
from probability import bucket_probabilities
from clients import market_client
import config
import market_discovery

DEFAULT_FEE_RATE_PCT = 0.0  # UNVERIFIED -- see module docstring
DEFAULT_TRADE_SIZE_USD = 50.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_market_quotes(token_map: Dict[int, dict]) -> Dict[int, MarketQuote]:
    """
    Pull live YES/NO prices for every bucket in token_map. Buckets
    whose price fetch fails are still included in the result with
    None prices, so downstream EV computation can report "no price
    available" explicitly rather than silently dropping the bucket.

    Each side is fetched from its OWN token id -- yes_token_id for YES,
    no_token_id for NO. Neither is ever derived from the other: the two
    are independently quoted (NegRisk) and `1 - yes_price` is not the NO
    price. See clients/market_client.py's module docstring for what that
    assumption cost the last time it was made.
    """
    quotes = {}
    for bucket_c, ids in token_map.items():
        yes_price = market_client.get_current_price_for_side(ids["yes_token_id"], "YES")
        no_price = market_client.get_current_price_for_side(ids["no_token_id"], "NO")
        # Advisory cross-check only -- see market_client's module docstring
        # for why this is a log line and not a gate: a derived (inverted)
        # price sums to exactly 1.00 and would pass, so the real guards
        # are the per-token fetch and MAX_PLAUSIBLE_RAW_EDGE. What a large
        # residual here DOES catch is a token-map mix-up or a stale side.
        if yes_price is not None and no_price is not None and abs(yes_price + no_price - 1.0) > 0.10:
            print(
                f"[ev_engine] SANITY: bucket {bucket_c} yes+no = {yes_price + no_price:.2f} "
                f"-- sides may be stale or the token map mismapped; edge veto is the backstop."
            )
        quotes[bucket_c] = MarketQuote(
            bucket_c=bucket_c,
            yes_price=yes_price,
            no_price=no_price,
            fetched_at=_now_iso(),
        )
    return quotes


def compute_ev_table(
    estimate: CalibratedEstimate,
    token_map: Dict[int, dict],
    trade_size_usd: float = DEFAULT_TRADE_SIZE_USD,
    fee_rate_pct: float = DEFAULT_FEE_RATE_PCT,
) -> List[EVResult]:
    """
    Core entry point. For every bucket with a token_map entry, compute
    EV for BOTH the YES side and the NO side, using the model's
    calibrated probability and a live market quote + real slippage
    estimate from the order book.

    Returns one EVResult per (bucket, side) -- 2x len(token_map)
    entries. Buckets with no live price available get an EVResult
    with net_ev_per_dollar=None rather than being silently skipped,
    so callers can see what couldn't be evaluated this cycle.
    """
    model_probs = {b.bucket_c: b.probability for b in bucket_probabilities(estimate)}
    quotes = fetch_market_quotes(token_map)

    results = []
    for bucket_c, ids in token_map.items():
        model_prob = model_probs.get(bucket_c, 0.0)
        quote = quotes[bucket_c]

        for side, price, token_id in [
            ("YES", quote.yes_price, ids["yes_token_id"]),
            ("NO", quote.no_price, ids["no_token_id"]),
        ]:
            side_model_prob = model_prob if side == "YES" else (1 - model_prob)

            if price is None:
                results.append(EVResult(
                    station_icao=estimate.station_icao,
                    target_date=estimate.target_date,
                    bucket_c=bucket_c,
                    side=side,
                    model_prob=side_model_prob,
                    market_price=None,
                    raw_edge=None,
                    estimated_slippage_pct=0.0,
                    fee_rate_pct=fee_rate_pct,
                    net_ev_per_dollar=None,
                    notes="No live price available this cycle.",
                ))
                continue

            slippage = market_client.estimate_slippage(token_id, trade_size_usd)
            raw_edge = side_model_prob - price
            net_ev = (raw_edge / price) - slippage - fee_rate_pct if price > 0 else None

            results.append(EVResult(
                station_icao=estimate.station_icao,
                target_date=estimate.target_date,
                bucket_c=bucket_c,
                side=side,
                model_prob=side_model_prob,
                market_price=price,
                raw_edge=raw_edge,
                estimated_slippage_pct=slippage,
                fee_rate_pct=fee_rate_pct,
                net_ev_per_dollar=net_ev,
            ))

    return results


def book_dislocation(token_map: Dict[int, dict]) -> Optional[float]:
    """
    Sum of YES prices across the full bracket, minus $1. Per the
    mechanism analysis: Polymarket's NegRisk Convert mechanism links
    these buckets, so this should normally sit close to zero. A large
    deviation is a real (rare) signal; a small one (a few cents) is
    more likely just the documented cost-of-conversion friction than
    exploitable edge -- do not treat any nonzero value here as
    automatically tradeable without accounting for that.

    Returns None if any bucket's YES price couldn't be fetched, since
    a partial sum isn't meaningful.
    """
    quotes = fetch_market_quotes(token_map)
    prices = [q.yes_price for q in quotes.values()]
    if any(p is None for p in prices):
        return None
    return round(sum(prices) - 1.0, 4)


def best_opportunities(
    results: List[EVResult],
    min_net_ev: float = 0.15,
    min_price: float = 0.03,
) -> List[EVResult]:
    """
    Filter to EVResults clearing a minimum net-EV bar, sorted best
    first. Mirrors risk_manager's EV-threshold concept from the
    holistic framework -- noise-level mispricings that don't survive
    costs shouldn't surface as "opportunities."

    min_price matters more than it looks: when market_price is very
    close to zero, (model_prob - price) / price explodes into
    triple/quadruple-digit percentages even for a small absolute
    disagreement -- confirmed during testing against a converged
    market, where a stale model showed >10,000% "EV" on a near-zero
    bucket. That's almost always a signal the model itself is stale
    (e.g. a morning calibration still being used late in the day,
    well past the edge-decay window) rather than real, actionable
    edge. Filtering out sub-min_price buckets here is a blunt but
    honest guard against surfacing those artifacts as opportunities.
    """
    viable = [
        r for r in results
        if r.net_ev_per_dollar is not None
        and r.net_ev_per_dollar >= min_net_ev
        and r.market_price is not None
        and r.market_price >= min_price
    ]
    return sorted(viable, key=lambda r: r.net_ev_per_dollar, reverse=True)


def run_for_station(
    estimate: CalibratedEstimate,
    trade_size_usd: float = DEFAULT_TRADE_SIZE_USD,
    fee_rate_pct: float = DEFAULT_FEE_RATE_PCT,
) -> List[EVResult]:
    """
    Convenience wrapper: runs market_discovery automatically instead
    of requiring a hand-built token_map. This is the function to call
    once discovery is trusted; compute_ev_table() remains available
    directly for callers who already have a token_map (e.g. cached
    from a previous discovery run, to avoid re-hitting Gamma every
    scan cycle).
    """
    station = config.get_station(estimate.station_icao)
    token_map = market_discovery.discover_token_map(
        station, estimate.target_date, config.BUCKET_MIN_C, config.BUCKET_MAX_C
    )
    if not token_map:
        print(f"[ev_engine] no token map discovered for {station.icao} on {estimate.target_date} -- cannot compute EV.")
        return []
    return compute_ev_table(estimate, token_map, trade_size_usd, fee_rate_pct)


def print_ev_table(results: List[EVResult]) -> None:
    """Human-readable console output for one EV computation cycle."""
    print(f"\n{'Bucket':>7} {'Side':>4} {'Model p':>8} {'Mkt price':>10} {'Raw edge':>9} {'Slip':>6} {'Net EV/$':>9}")
    for r in sorted(results, key=lambda x: (x.bucket_c, x.side)):
        if r.net_ev_per_dollar is None:
            print(f"{r.bucket_c:>6}° {r.side:>4} {r.model_prob:>7.1%} {'n/a':>10} {'--':>9} {'--':>6} {'--':>9}  ({r.notes})")
        else:
            print(
                f"{r.bucket_c:>6}° {r.side:>4} {r.model_prob:>7.1%} "
                f"{r.market_price:>9.3f}c {r.raw_edge:>+8.1%} "
                f"{r.estimated_slippage_pct:>5.1%} {r.net_ev_per_dollar:>+8.1%}"
            )
    print()
