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
Verified against https://docs.polymarket.com/polymarket-learn/trading/fees
on 2026-08-05: makers pay nothing, but TAKERS pay a fee on every trade,
computed as fee = shares * rate * p * (1 - p), where the rate for the
Weather category is 0.05. This bot takes liquidity (it walks the book),
so the taker schedule applies. Expressed as a fraction of the dollars
spent buying at price p, the fee is rate * (1 - p) -- price-dependent,
NOT flat: ~3.2% of notional at a 35c entry, ~4.5% at 10c. That is the
same order of magnitude as MIN_ABS_RAW_EDGE, so ignoring it materially
overstates EV on exactly the cheap long-shot entries this system likes.

fee_rate_pct=None (the default) therefore computes the real schedule
per candidate at that candidate's own price. Passing an explicit float
retains the old flat-rate behavior -- the backtest engine does this so
fee sensitivity stays an explicit, cache-keyed run parameter there.

Exit-side taker fees (paying the schedule again when selling before
resolution) are NOT modeled here; resolution redemptions are fee-free.

DEPENDENCIES
------------
config.py, models.py, probability.py (local)
clients/market_client.py (local)
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from models import CalibratedEstimate, MarketQuote, EVResult
from probability import bucket_probabilities
from clients import market_client
import bucket_axis
import config
import market_discovery

# Weather-category taker fee rate, verified 2026-08-05 -- see the FEE RATE
# section of the module docstring for the source and the full formula.
POLYMARKET_WEATHER_TAKER_FEE_RATE = 0.05

# None means "compute the real price-dependent taker schedule per candidate"
# (taker_fee_pct_of_notional below). An explicit float is a flat override,
# kept for the backtest engine's fee-sensitivity runs.
DEFAULT_FEE_RATE_PCT = None
DEFAULT_TRADE_SIZE_USD = 50.0


def taker_fee_pct_of_notional(price: Optional[float]) -> float:
    """
    Polymarket taker fee as a fraction of the dollars spent buying at
    `price`: fee = shares * rate * p * (1-p) and notional = shares * p,
    so fee / notional = rate * (1 - p). Returns 0.0 for a missing or
    non-positive price -- those candidates carry net_ev_per_dollar=None
    and are never sized, so the fee number is informational there.
    """
    if price is None or price <= 0:
        return 0.0
    return POLYMARKET_WEATHER_TAKER_FEE_RATE * (1.0 - price)

# Kill switch for piggybacked snapshot capture (see _capture_snapshots()
# below): when True, every run_for_station() cycle also writes this
# cycle's live YES/NO prices (and depth, where known) into
# backtest/price_store.py's tables, so high-fidelity history accumulates
# for free during normal paper-trading operation -- valuable because the
# CLOB /prices-history endpoint is only coarse-grained for resolved
# markets (see backtest/price_history_client.py's module docstring).
# Flip to False to disable capture entirely without touching call sites.
ENABLE_SNAPSHOT_CAPTURE = True

# Order-book depth is NOT in the quotes run_for_station() already holds --
# it costs one /book call per side token. Fetching it every cycle would
# roughly double the cycle's market-client traffic for data whose only
# consumer is the backtest's fill model, so depth is captured on every
# Nth capture pass instead (rows between depth passes store depth NULL,
# exactly as before). The first month of capture stored depth on ZERO
# rows -- MarketQuote.yes/no_depth_usd is never populated by the quote
# path -- which kept the backtest's observed_median depth regime
# permanently non-viable. Counter is per-process; a daemon restart just
# makes the next pass a depth pass.
#
# Counted PER STATION, not globally. With one shared counter and 13
# registered stations, "every 3rd pass" stops meaning "every 3rd pass for
# this station" and starts meaning "every 3rd station" -- the depth pass
# rotates around the registry, so any individual token gets depth on
# roughly 1-in-3-of-1-in-13 passes. That drops per-token depth coverage
# far below backtest MIN_DEPTH_COVERAGE=0.5 and quietly regresses commit
# 421cd52, which existed precisely to fix zero-depth capture.
DEPTH_CAPTURE_EVERY_N_PASSES = 3
_capture_pass_count_by_station: Dict[str, int] = {}


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

    PRICED OFF THE ASK, NOT THE BID. These quotes feed compute_ev_table()
    -> raw_edge -> EntryDecision.entry_price -> the limit price on the
    order, i.e. they are ENTRY prices, and an entry pays the ask. This
    used to call get_current_price_for_side(), which returns the bid: the
    whole entry funnel was valuing trades at a price it could not get, by
    the width of the spread. See market_client.get_entry_price_for_side()
    for the measurement and for why the approval rate should be expected
    to fall now that it is corrected.
    """
    quotes = {}
    for bucket_c, ids in token_map.items():
        yes_price = market_client.get_entry_price_for_side(ids["yes_token_id"], "YES")
        no_price = market_client.get_entry_price_for_side(ids["no_token_id"], "NO")
        # Advisory cross-check only -- see market_client's module docstring
        # for why this is a log line and not a gate: a derived (inverted)
        # price sums to exactly 1.00 and would pass, so the real guards
        # are the per-token fetch and MAX_PLAUSIBLE_RAW_EDGE. What a large
        # residual here DOES catch is a token-map mix-up or a stale side.
        #
        # Both sides are now asks, so the pair sums to slightly ABOVE 1.00
        # by the two spreads rather than straddling it. The 0.10 tolerance
        # absorbs that comfortably on any book worth trading; a residual
        # large enough to trip it is still a real mix-up, not spread.
        if yes_price is not None and no_price is not None and abs(yes_price + no_price - 1.0) > 0.10:
            print(
                f"[ev_engine] SANITY: bucket {bucket_c} yes+no = {yes_price + no_price:.2f} "
                f"-- sides may be stale or the token map mismapped; edge veto is the backstop."
            )
        # The BID for each side as well. Not used for any trading decision
        # -- it exists so save_market_snapshot() can record the bid in the
        # column that has always held bids, instead of quietly starting to
        # write asks there the moment entry pricing moved to the ask side.
        # That would have corrupted the backtest's price history invisibly:
        # same column, same source string, different meaning by date.
        quotes[bucket_c] = MarketQuote(
            bucket_c=bucket_c,
            yes_price=yes_price,
            no_price=no_price,
            fetched_at=_now_iso(),
            yes_bid=market_client.get_token_bid(ids["yes_token_id"]),
            no_bid=market_client.get_token_bid(ids["no_token_id"]),
        )
    return quotes


def compute_ev_table(
    estimate: CalibratedEstimate,
    token_map: Dict[int, dict],
    trade_size_usd: float = DEFAULT_TRADE_SIZE_USD,
    fee_rate_pct: Optional[float] = DEFAULT_FEE_RATE_PCT,
    quotes: Optional[Dict[int, MarketQuote]] = None,
    model_probs: Optional[Dict[int, float]] = None,
) -> List[EVResult]:
    """
    Core entry point. For every bucket with a token_map entry, compute
    EV for BOTH the YES side and the NO side, using the model's
    calibrated probability and a live market quote + real slippage
    estimate from the order book.

    quotes: optional pre-fetched {bucket_c: MarketQuote} map. Pass this
    when the caller already fetched quotes this cycle (e.g.
    run_for_station(), which also needs them for snapshot capture) to
    avoid re-hitting the market client a second time. If omitted,
    quotes are fetched here as before.

    model_probs: optional pre-computed {bucket_c: probability}. The
    trading path passes this because the model's support must match the
    LIVE event's bucket range and the station's edge mode (see
    run_for_station_with_map) -- computing it here from the module-level
    default bounds would price a 27-37 book against a 25-35 model and
    hand every bucket outside the overlap a phantom edge. Omitted, the
    legacy default-bounds computation runs exactly as before, which is
    what station-agnostic callers and old tests expect.

    Returns one EVResult per (bucket, side) -- 2x len(token_map)
    entries. Buckets with no live price available get an EVResult
    with net_ev_per_dollar=None rather than being silently skipped,
    so callers can see what couldn't be evaluated this cycle.
    """
    if model_probs is None:
        model_probs = {
            b.bucket_c: b.probability
            for b in bucket_probabilities(
                estimate, axis=bucket_axis.for_station(config.get_station(estimate.station_icao))
            )
        }
    if quotes is None:
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
                    fee_rate_pct=fee_rate_pct if fee_rate_pct is not None else 0.0,
                    net_ev_per_dollar=None,
                    spread_source=estimate.spread_source,
                    notes="No live price available this cycle.",
                ))
                continue

            slippage = market_client.estimate_slippage(token_id, trade_size_usd)
            raw_edge = side_model_prob - price
            # Flat override if the caller passed one; otherwise the real
            # price-dependent taker schedule at THIS candidate's price.
            # Stored on the EVResult, so entry_manager's at-size re-check
            # deducts the same fee without recomputing it.
            fee_pct = fee_rate_pct if fee_rate_pct is not None else taker_fee_pct_of_notional(price)
            net_ev = (raw_edge / price) - slippage - fee_pct if price > 0 else None

            results.append(EVResult(
                station_icao=estimate.station_icao,
                target_date=estimate.target_date,
                bucket_c=bucket_c,
                side=side,
                model_prob=side_model_prob,
                market_price=price,
                raw_edge=raw_edge,
                estimated_slippage_pct=slippage,
                fee_rate_pct=fee_pct,
                net_ev_per_dollar=net_ev,
                spread_source=estimate.spread_source,
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
    min_price: float = config.EV_MIN_PRICE_SCREEN,
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


@dataclass
class StationEVRun:
    """
    Everything one station's EV cycle produced, kept together on purpose.

    The token map and the bounds derived from it are OUTPUTS of the cycle,
    not incidental scratch data: whatever else acts on this cycle's EV
    table (entry sizing needs each candidate's token id) must use the SAME
    map the probabilities were computed against. Re-discovering it
    downstream is how the two silently disagree -- a second Gamma call can
    land after the event updates, or be seeded with different fallback
    bounds, and then the EV table and the tokens it is sized against
    describe different books.

    veto_reason is non-empty exactly when the station-day was refused
    (no map, or a malformed one); ev_results is then empty by construction.
    """
    station_icao: str
    target_date: date
    token_map: Dict[int, dict] = field(default_factory=dict)
    bucket_min_c: Optional[int] = None
    bucket_max_c: Optional[int] = None
    ev_results: List[EVResult] = field(default_factory=list)
    veto_reason: str = ""


def run_for_station_with_map(
    estimate: CalibratedEstimate,
    trade_size_usd: float = DEFAULT_TRADE_SIZE_USD,
    fee_rate_pct: Optional[float] = DEFAULT_FEE_RATE_PCT,
) -> StationEVRun:
    """
    One station's full EV cycle, returning the token map and live bucket
    bounds alongside the EV table (see StationEVRun for why they travel
    together). run_for_station() below is this function's ev_results.

    ORDER MATTERS, AND IT IS DISCOVERY FIRST. The event's real bucket
    window is a live fact that drifts seasonally (Singapore moved 25-35
    -> 27-37 between July and August 2026 while config still said 25/35),
    so the model's support is derived from the discovered map rather than
    asserted from config:

      1. discover the token map (station bounds seed only the edge-label
         fallback in parse_bucket_label -- they filter nothing)
      2. derive live bounds; a missing or malformed map VETOES the
         station-day outright. No trading on a book we can't enumerate:
         every bucket absent from the map would otherwise carry
         model_prob 0.0 and hand its NO side a ~0.20 phantom edge that
         clears MAX_PLAUSIBLE_RAW_EDGE and both EV windows.
      3. log loudly on drift -- config is the stale side, the live event
         is the market being traded -- and PROCEED on the live bounds
      4. compute bucket probabilities on those bounds and the station's
         own bucket_edge_mode, then price them against the book
    """
    station = config.get_station(estimate.station_icao)

    token_map = market_discovery.discover_token_map(
        station, estimate.target_date, station.bucket_min_c, station.bucket_max_c
    )
    if not token_map:
        print(
            f"[ev_engine] no token map discovered for {station.icao} on {estimate.target_date} "
            f"-- cannot compute EV, skipping this station-day."
        )
        return StationEVRun(
            station_icao=station.icao,
            target_date=estimate.target_date,
            veto_reason="no token map discovered",
        )

    bounds = market_discovery.derive_bucket_bounds(token_map)
    if bounds is None:
        discovered = sorted(token_map)
        print(
            f"[ev_engine] VETOED {station.icao} on {estimate.target_date}: discovered bucket map is "
            f"MALFORMED -- {len(discovered)} bucket(s) {discovered} (expected "
            f"{config.EXPECTED_BUCKET_COUNT} contiguous). A partial or gappy map is not a smaller "
            f"opportunity set, it is a fabricated one: every bucket the event lists but the map "
            f"lacks gets model_prob 0.0, whose NO side shows a ~20c edge that clears every risk "
            f"gate. Not trading this station today; check parse_bucket_label against the live event."
        )
        return StationEVRun(
            station_icao=station.icao,
            target_date=estimate.target_date,
            token_map=token_map,
            veto_reason=f"malformed bucket map ({len(discovered)} bucket(s): {discovered})",
        )

    bucket_min, bucket_max = bounds
    if (bucket_min, bucket_max) != (station.bucket_min_c, station.bucket_max_c):
        print(
            f"[ev_engine] BOUNDS DRIFT for {station.icao}: live event lists {bucket_min}-{bucket_max}°C, "
            f"config.STATIONS says {station.bucket_min_c}-{station.bucket_max_c}°C. The LIVE event is "
            f"what is being traded, so pricing proceeds on {bucket_min}-{bucket_max}; config is the "
            f"stale side and its cross-check bounds want updating."
        )

    model_probs = {
        b.bucket_c: b.probability
        for b in bucket_probabilities(
            estimate, bucket_min, bucket_max, axis=bucket_axis.for_station(station)
        )
    }

    quotes = fetch_market_quotes(token_map)
    _capture_snapshots(station.icao, estimate.target_date, token_map, quotes)
    ev_results = compute_ev_table(
        estimate, token_map, trade_size_usd, fee_rate_pct,
        quotes=quotes, model_probs=model_probs,
    )
    return StationEVRun(
        station_icao=station.icao,
        target_date=estimate.target_date,
        token_map=token_map,
        bucket_min_c=bucket_min,
        bucket_max_c=bucket_max,
        ev_results=ev_results,
    )


def run_for_station(
    estimate: CalibratedEstimate,
    trade_size_usd: float = DEFAULT_TRADE_SIZE_USD,
    fee_rate_pct: Optional[float] = DEFAULT_FEE_RATE_PCT,
) -> List[EVResult]:
    """
    Convenience wrapper: runs market_discovery automatically instead
    of requiring a hand-built token_map. This is the function to call
    once discovery is trusted; compute_ev_table() remains available
    directly for callers who already have a token_map (e.g. cached
    from a previous discovery run, to avoid re-hitting Gamma every
    scan cycle).

    Returns only the EV table, unchanged for every existing caller.
    Anything that ALSO needs this cycle's token map -- entry sizing does,
    since it must size against the same book the EV was computed on --
    should call run_for_station_with_map() and read both off the result
    rather than re-discovering the map for itself.
    """
    return run_for_station_with_map(estimate, trade_size_usd, fee_rate_pct).ev_results


def _capture_snapshots(
    station_icao: str,
    target_date,
    token_map: Dict[int, dict],
    quotes: Dict[int, MarketQuote],
) -> None:
    """
    Best-effort piggyback capture: writes this cycle's already-fetched
    YES/NO prices (and depth, where the quote carries it) into
    backtest/price_store.py's market_tokens/price_snapshots tables, so
    live paper-trading cycles double as free historical-data collection
    (see ENABLE_SNAPSHOT_CAPTURE above for why this matters).

    Imports backtest.price_store/backtest.settings LAZILY (only inside
    this function) so a missing/broken `backtest` package can never
    break importing ev_engine.py itself -- and the whole body is wrapped
    in a broad except so a failure here (including that ImportError)
    can NEVER interrupt or break a live trading cycle. This is capture
    only, never a gate: it must fail silently (past a single warning
    line) no matter what goes wrong.
    """
    if not ENABLE_SNAPSHOT_CAPTURE:
        return

    try:
        import backtest.price_store as price_store
        import backtest.settings as settings

        # Per-station counter: see DEPTH_CAPTURE_EVERY_N_PASSES above for
        # why a single global one silently starves depth coverage now the
        # registry spans 13 stations.
        pass_count = _capture_pass_count_by_station.get(station_icao, 0)
        depth_pass = (pass_count % DEPTH_CAPTURE_EVERY_N_PASSES) == 0
        _capture_pass_count_by_station[station_icao] = pass_count + 1

        target_date_iso = target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date)
        discovered_at = _now_iso()
        now_ts = int(time.time())

        for bucket_c, ids in token_map.items():
            quote = quotes.get(bucket_c)
            if quote is None:
                continue

            # bid -> the `price` column (what it has always held), ask ->
            # `ask_price`. quote.yes_price/no_price are ASKS since the
            # 2026-08-10 entry-pricing fix, so passing them as `price` here
            # would silently change the meaning of the stored series.
            for side, token_id, price, ask_price, depth_usd in [
                ("yes", ids.get("yes_token_id"), quote.yes_bid, quote.yes_price, quote.yes_depth_usd),
                ("no", ids.get("no_token_id"), quote.no_bid, quote.no_price, quote.no_depth_usd),
            ]:
                if not token_id:
                    continue

                if depth_pass and depth_usd is None and price is not None:
                    # One /book call per side token, depth-pass cycles only.
                    # Per-token fail-soft: a dead book must cost this token
                    # its depth field, not the whole capture pass.
                    try:
                        depth_usd = market_client.get_available_depth_usd(token_id)
                    except Exception:  # noqa: BLE001
                        depth_usd = None

                price_store.upsert_token(
                    token_id=token_id,
                    station_icao=station_icao,
                    target_date=target_date_iso,
                    bucket_c=bucket_c,
                    side=side,
                    discovered_at=discovered_at,
                )

                if price is None:
                    continue

                price_store.save_snapshot(
                    token_id=token_id,
                    ts=now_ts,
                    price=price,
                    ask_price=ask_price,
                    depth_usd=depth_usd,
                    source="live_snapshot",
                    fidelity_min=settings.DEFAULT_SNAPSHOT_FIDELITY_MIN,
                )
    except Exception as exc:
        print(f"[ev_engine] snapshot capture failed this cycle (non-fatal, trading unaffected): {exc}")


def capture_exit_snapshot(
    station_icao: str,
    target_date,
    bucket_c: int,
    side: str,
    token_id: str,
    bid_price: float,
    fidelity_min: int,
) -> None:
    """
    Capture ONE already-fetched bid from the exit path (position_manager).

    Same best-effort contract as _capture_snapshots() above -- lazy imports,
    blanket except, honours ENABLE_SNAPSHOT_CAPTURE -- because this runs
    inside the loop that carries every open position's stop-loss. Capture
    must never be able to interrupt that.

    WHY A SECOND, NARROWER CAPTURE FUNCTION. _capture_snapshots() takes a
    whole token_map plus MarketQuotes and writes both sides of every bucket.
    The exit path has none of that: it fetches one bid, for one token, for
    the one bucket it holds. Reconstructing a token_map here would mean
    re-discovering markets and re-quoting both sides on every monitor cycle
    -- entry-window work, done all day, to record prices for buckets nobody
    holds. So this writes exactly what the exit check already paid for.

    The consequence is deliberate and must not be papered over: these rows
    cover only HELD buckets, and carry ask_price=None and depth_usd=None.
    That is the honest shape of "one bid, nothing else" -- see
    price_store.save_snapshot() on why NULL means "not captured" rather than
    zero, and price_store.EXIT_SNAPSHOT_SOURCE for the coverage gap this
    closes.

    `bid_price` MUST be the bid (market_client.get_current_price_for_side),
    never an entry ask: price_store's `price` column has held the bid since
    before 2026-08-10 and mixing the two would make the series incomparable
    to itself with nothing in the data to say which row is which.

    `fidelity_min` is the ACTIVE WINDOW's scan interval, threaded down from
    scheduler.run_cycle(), not DEFAULT_SNAPSHOT_FIDELITY_MIN. It is what
    get_price_at() derives its staleness limit from, so writing 5 here for a
    row that will not be followed by another for 30 minutes would tell every
    future replay that a normal monitor cadence is a data gap.
    """
    if not ENABLE_SNAPSHOT_CAPTURE:
        return

    try:
        import backtest.price_store as price_store

        target_date_iso = (
            target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date)
        )
        # price_store stores sides lowercase ("yes"/"no"); Position.side is
        # upper. load_token_map() keys off this column, so a stray "YES"
        # here would write a token the replay cannot find.
        side_key = str(side).lower()

        price_store.upsert_token(
            token_id=token_id,
            station_icao=station_icao,
            target_date=target_date_iso,
            bucket_c=bucket_c,
            side=side_key,
            discovered_at=_now_iso(),
        )
        price_store.save_snapshot(
            token_id=token_id,
            ts=int(time.time()),
            price=bid_price,
            ask_price=None,
            depth_usd=None,
            source=price_store.EXIT_SNAPSHOT_SOURCE,
            fidelity_min=int(fidelity_min),
        )
    except Exception as exc:  # noqa: BLE001 - capture must never break an exit cycle
        print(
            f"[ev_engine] exit-path snapshot capture failed for {station_icao} "
            f"{bucket_c}°{side} (non-fatal, exit checking unaffected): {exc}"
        )


def save_ev_snapshot(station_icao: str, results: List[EVResult]) -> None:
    """
    Persist the latest EV table for one station to
    data/ev_latest_<ICAO>.json, overwriting the previous snapshot.

    Read by the status dashboard (deploy/generate_dashboard.py), which
    runs in a separate process on its own timer -- a JSON file is the
    simplest handoff that survives daemon restarts and needs no schema
    migration. An EMPTY results list still writes a snapshot: "computed
    at 05:01 and found nothing" and "never computed" are different facts,
    and the dashboard should be able to tell them apart.

    Fails soft: the EV table drives trading, the snapshot only drives
    reporting, so a disk error here must never break the cycle.
    """
    path = config.DATA_DIR / f"ev_latest_{station_icao}.json"
    payload = {
        "station_icao": station_icao,
        "generated_at": _now_iso(),
        "target_date": str(results[0].target_date) if results else None,
        "results": [
            {
                "bucket_c": r.bucket_c,
                "side": r.side,
                "model_prob": r.model_prob,
                "market_price": r.market_price,
                "raw_edge": r.raw_edge,
                "slippage_pct": r.estimated_slippage_pct,
                "net_ev_per_dollar": r.net_ev_per_dollar,
                "spread_source": r.spread_source,
                "notes": r.notes,
            }
            for r in results
        ],
    }
    try:
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[ev_engine] could not save EV snapshot for {station_icao}: {exc}")


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
