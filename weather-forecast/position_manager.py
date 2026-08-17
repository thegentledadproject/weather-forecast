"""
position_manager.py

PURPOSE
-------
Orchestrates the "don't wait for resolution" workflow:

  1. Load every currently-open position (any station) from storage
  2. Pull each one's LIVE current price via market_client
  3. Sanity-check that price before believing it (see below)
  4. Ask risk_manager whether it should be exited right now
  5. If yes, hand off to executor.py to actually close it, then
     record the closed position back to storage

STEP 3 EXISTS BECAUSE A PRICE IS NOT A FACT
-------------------------------------------
risk_manager.evaluate_exit() is pure logic and trusts whatever price it
is handed -- which makes this module the only place that can stop a bad
number from becoming a bad trade. Two failure modes are screened here
before any exit action is taken:

  - RESOLVED MARKETS. A resolved bucket prints ~1.00 on the winning side
    and ~0.00 on the losing side. Fed straight into evaluate_exit(), the
    losing side looks exactly like a catastrophic price collapse and gets
    booked as a stop-loss -- wrong exit reason, wrong exit price, and a
    permanently corrupted performance record. Extreme prices (per
    config.MIN_EXIT_PRICE) and past-dated positions are therefore checked
    against Gamma's closed/archived flags, and closed with status
    "closed_resolution" / exit_reason "market_resolved" on a code path
    that CANNOT reach the stop-loss logic.
  - IMPLAUSIBLE JUMPS. A move larger than config.MAX_SINGLE_CYCLE_MOVE
    from the position's last OBSERVED price needs a confirming re-fetch
    before anything acts on it. If the re-fetch disagrees or fails, this
    cycle acts on nothing and leaves the position open. Missing one
    cycle is recoverable; exiting on a phantom price is not.

ONLY GAMMA DECIDES THAT A MARKET RESOLVED
-----------------------------------------
A price at the edge of the book is a REASON TO ASK, never an answer. The
only thing that books a position as resolved is Gamma explicitly saying
closed/archived, because exit_price gets rounded to 1.0/0.0 there -- and
doing that to a market that is merely cheap (say a real 0.05 that is
still sellable) fabricates a price and destroys real value. So:

    Gamma says closed  -> resolution close (never a stop-loss)
    Gamma says OPEN    -> the price is real. Fall through to
                          risk_manager.evaluate_exit() exactly like any
                          other price: stops and profit-takes DO apply.
                          "Route through the resolution check" means
                          check first, not hold forever -- a genuine
                          collapse to 0.02 on a live market still needs
                          its loss cut.
    Gamma unknown      -> hold THIS CYCLE ONLY, and count it. A position
                          stuck unresolvable escalates to a loud warning
                          rather than silently sitting there forever.

Positions whose price cannot be fetched at all are counted the same way,
and once one is unmonitorable for UNMONITORABLE_CYCLES_WARN cycles Gamma
is consulted directly -- a market can resolve while its price feed is
down, and an open position with no working price feed has no working
stop-loss either. Silence is the worst way to find that out.

This module is meant to run on every scan cycle, same cadence as
pipeline.py -- exit checks need to happen just as often as entry
checks, arguably more often near the edge-decay tightening hour,
since that's exactly when positions are most likely to need closing
rather than being opened fresh.

Deliberately separate from pipeline.py: pipeline.py answers "is there
a new trade worth entering," this module answers "does an existing
trade need to be exited." Different questions, different triggers,
different modules -- keeps each one simpler to reason about and test.

DEPENDENCIES
------------
config.py, models.py, risk_manager.py, storage.py (local)
clients/market_client.py (local)
market_discovery.py (local)
executor.py (local)
backtest/resolution.py (local) -- for the settlement-source fallback
    below. A LIVE module importing from backtest/ deserves the raised
    eyebrow, so: bucket_for_temp() and resolution_exit_price() are pure
    functions over (temperature, bucket bounds, edge mode) and import
    nothing but math, datetime and config. Writing a second copy here
    is the actually dangerous option -- bucket_for_temp() deliberately
    avoids round() because banker's rounding disagrees with
    probability.py on exactly the half-degree bucket edges, and a
    reimplementation that forgot that would settle live positions into
    different buckets than the backtest scores them in.
"""

from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import config
import storage
import risk_manager
import market_discovery
from clients import market_client
from models import Position, ExitDecision
import executor
from backtest import resolution as settlement

# Consecutive failed price fetches, keyed by position_id. Deliberately
# in-memory and unpersisted: what this measures is "how long have we been
# flying blind on this position in THIS process", and a restart genuinely
# does reset that -- the first fetch after startup is a fresh data point,
# not a continuation. Nothing downstream depends on the count surviving.
_consecutive_price_failures: Dict[str, int] = {}

# Consecutive cycles a position has looked possibly-resolved (extreme price
# or past-dated) while Gamma could not say either way. Same in-memory,
# same-process rationale as _consecutive_price_failures above.
_consecutive_unknown_resolution: Dict[str, int] = {}

# Last price actually OBSERVED for a position, keyed by position_id -- the
# baseline for "how far did this move since we last looked."
#
# Deliberately NOT position.high_water_mark: the high-water mark is a
# monotone non-decreasing peak (kept as a record since the trailing stop
# was removed 2026-08-17), so using it here measured drawdown-from-peak
# instead of per-cycle movement.
# A position that had drifted down from its peak over many cycles would
# then get flagged as a huge single-cycle jump and sent for confirmation
# on every single scan, which is both noise and, combined with the
# resolution check, a way to mistake slow decay for a resolution event.
_last_observed_price: Dict[str, float] = {}

# Consecutive unmonitorable cycles before the log escalates from routine
# ("will retry next scan") to a warning -- and before Gamma is consulted
# directly to see whether the market resolved while the feed was down. An
# open position whose price can't be read has no live stop-loss behind it,
# so this needs to be noticed rather than scrolling past as an info line.
UNMONITORABLE_CYCLES_WARN = 3

# How far a confirming re-fetch may sit from the price that triggered it
# before the two are treated as disagreeing. Two quotes seconds apart on
# a market that genuinely moved should still agree closely; if they don't,
# what moved is the feed, not the market.
CONFIRMATION_TOLERANCE = 0.05


def check_and_exit_positions(
    station_icao: Optional[str] = None,
    capture_fidelity_min: Optional[int] = None,
) -> List[ExitDecision]:
    """
    Run one full exit-check cycle. Returns the list of ExitDecisions
    made this cycle (including "hold" decisions), for logging/summary.
    Positions skipped entirely this cycle (price unavailable, or a price
    that failed confirmation) produce no decision -- they are left open
    and logged, never exited on an unverified number.

    capture_fidelity_min is the ACTIVE SCHEDULE WINDOW's scan interval in
    minutes, supplied by scheduler.run_cycle(). Each confirmed price is
    written to the backtest price store tagged with it, which is how a
    replay learns what this cycle saw -- see ev_engine.capture_exit_snapshot()
    and price_store.EXIT_SNAPSHOT_SOURCE.

    None means "cadence unknown" and DISABLES capture for the cycle rather
    than guessing a fidelity. Staleness in get_price_at() is derived from
    this number, so an invented one does not merely mislabel a row, it
    changes which future replays can read it. Callers outside the scheduler
    (operator scripts, tests) fire at no fixed cadence and so have no honest
    value to pass -- for them, recording nothing is correct.
    """
    open_positions = storage.load_open_positions(station_icao=station_icao)
    decisions = []

    for position in open_positions:
        # One bad row must not take monitoring down for every other open
        # position. The scheduler wraps this whole function in a try, which
        # keeps the DAEMON alive but abandons the rest of the cycle -- so a
        # single position that raises means every position after it in the
        # list goes unchecked, on every cycle, forever. That is not
        # hypothetical: _token_id_for() raises NotImplementedError for any
        # position opened before token_id was threaded through, and
        # executor's auto-mode sell raises by design. Both are permanent
        # conditions on a specific row. _station_for() below already
        # reasoned this way about orphaned stations; the rest of the loop
        # did not.
        try:
            decision = _check_one_position(position, capture_fidelity_min)
        except Exception as exc:  # noqa: BLE001 - one row must never strand the others
            print(
                f"[position_manager] ERROR checking {position.position_id} ({position.station_icao} "
                f"{position.bucket_c}°{position.side}, ${position.size_usd:.2f} open): {exc} -- skipping "
                f"THIS position only, the rest of the cycle continues. This position is not being "
                f"monitored and has no working stop-loss until the cause is fixed."
            )
            continue
        if decision is not None:
            decisions.append(decision)

    return decisions


def _capture_exit_price(
    position: Position,
    token_id: str,
    bid_price: float,
    fidelity_min: Optional[int],
) -> None:
    """
    Hand one confirmed bid to the backtest price store, or do nothing.

    Imported lazily and swallowed whole. ev_engine reaches market_discovery
    and the CLOB client at import time, and this module is the one that
    carries every open position's stop-loss -- so nothing about recording
    history is allowed to fail, slow, or import its way into that path.
    ev_engine.capture_exit_snapshot() is itself fail-soft; this is the
    second belt, covering the import.
    """
    if fidelity_min is None:
        return
    try:
        import ev_engine

        ev_engine.capture_exit_snapshot(
            station_icao=position.station_icao,
            target_date=position.target_date,
            bucket_c=position.bucket_c,
            side=position.side,
            token_id=token_id,
            bid_price=bid_price,
            fidelity_min=fidelity_min,
        )
    except Exception as exc:  # noqa: BLE001 - history capture is never a gate
        print(
            f"[position_manager] snapshot capture unavailable this cycle "
            f"(non-fatal, exit checking unaffected): {exc}"
        )


def _check_one_position(
    position: Position,
    capture_fidelity_min: Optional[int] = None,
) -> Optional[ExitDecision]:
    """
    The full exit check for ONE position: price fetch, sanity screening,
    resolution check, high-water-mark refresh, exit evaluation, and the
    exit itself if one is decided.

    Returns the ExitDecision made (including "hold" decisions), or None
    when the position was skipped this cycle without a decision -- price
    unavailable, or a price that failed confirmation. Split out of
    check_and_exit_positions() so one position's failure can be contained
    to that position rather than aborting the cycle.
    """
    token_id = _token_id_for(position)
    current_price = market_client.get_current_price_for_side(
        token_id=token_id,
        side=position.side,
    )

    if current_price is None:
        failures = _note_price_failure(position)
        # A market can resolve while its price feed is down, and a
        # position we can't price is one we'd otherwise never see
        # resolve. Once blind for long enough, ask Gamma directly.
        if failures >= UNMONITORABLE_CYCLES_WARN:
            reported_closed = _market_reported_closed(position)
            if reported_closed is True:
                return _close_resolved_without_price(position, token_id)
            # Gamma can't say (lookup failed, or the bucket is no longer
            # listed -- which is itself what a settled event looks like)
            # AND the position's own market day is over. Two independent
            # feeds are down; the observation record is not, and it is the
            # authority both of them were only ever proxies for. Still
            # returns None and stays loud if no settlement-grade reading
            # exists. Gamma reporting OPEN is NOT overridden here: a live
            # market with a broken price feed is a feed problem, and
            # closing it on the weather would settle a position that can
            # still trade.
            if reported_closed is None and position.target_date < _local_today_for(position):
                return _close_from_settlement_source(position, gamma_closed=None)
        return None
    _consecutive_price_failures.pop(position.position_id, None)

    last_observed = _last_known_price(position)
    move = abs(current_price - last_observed)
    is_extreme = _is_extreme(current_price)
    is_big_move = move > config.MAX_SINGLE_CYCLE_MOVE
    # A bucket whose date has passed is resolved by definition, whatever
    # the book still prints -- a stale but plausible last-traded quote
    # would otherwise never trip the extreme-price check at all.
    # "Passed" is measured on the POSITION'S OWN market day: Tokyo's
    # date rolls over an hour before Singapore's and four hours before
    # Karachi's, and a UTC+8 "today" would call a Karachi position
    # past-dated while its market is still trading.
    is_past_dated = position.target_date < _local_today_for(position)

    if is_extreme or is_big_move or is_past_dated:
        # None of these is acted on off a single quote. Confirm the
        # price first; only then work out what it MEANS.
        print(
            f"[position_manager] {position.position_id}: price {current_price:.3f} needs confirmation "
            f"(extreme={is_extreme}, move={move:+.3f} from last observed {last_observed:.3f}, "
            f"past_dated={is_past_dated}) -- re-fetching once before taking any exit action."
        )
        confirmed_price = market_client.get_current_price_for_side(
            token_id=token_id,
            side=position.side,
        )

        if confirmed_price is None:
            print(
                f"[position_manager] {position.position_id}: confirming re-fetch FAILED for price "
                f"{current_price:.3f} -- taking NO exit action this cycle, position left open."
            )
            return None

        if abs(confirmed_price - current_price) > CONFIRMATION_TOLERANCE:
            print(
                f"[position_manager] {position.position_id}: price {current_price:.3f} NOT confirmed "
                f"(re-fetch says {confirmed_price:.3f}, tolerance {CONFIRMATION_TOLERANCE:.2f}) -- the "
                f"feed disagrees with itself, so taking NO exit action this cycle, position left open."
            )
            return None

        current_price = confirmed_price
        _last_observed_price[position.position_id] = current_price

        # Resolution is only a live hypothesis for an extreme price or
        # a past-dated bucket. A big move alone, once confirmed, is
        # just a big move -- no Gamma round-trip, straight to normal
        # evaluation below.
        if _is_extreme(current_price) or is_past_dated:
            market_closed = _market_reported_closed(position)

            if market_closed is True:
                return _close_as_resolved(position, current_price, market_closed)

            if market_closed is None:
                # Can't confirm either way. Hold THIS CYCLE ONLY -- and
                # count it, so an indefinitely unresolvable position
                # gets escalated instead of quietly sitting forever.
                _note_unknown_resolution(position, current_price)
                return ExitDecision(
                    position_id=position.position_id,
                    should_exit=False,
                    reason="resolution_unknown",
                    current_price=current_price,
                    pnl_pct=risk_manager.compute_pnl_pct(position.entry_price, current_price),
                )

            # Gamma says OPEN: the market is live and this price is
            # real, however extreme it looks. It gets evaluated like
            # any other price -- a genuine collapse still needs its
            # loss cut, and a genuine spike still needs taking.
            _consecutive_unknown_resolution.pop(position.position_id, None)
            print(
                f"[position_manager] {position.position_id}: price {current_price:.3f} confirmed and "
                f"Gamma reports the market still OPEN -- this is a real live price, evaluating exit "
                f"normally (stop-loss and profit-take both apply)."
            )
        else:
            print(
                f"[position_manager] {position.position_id}: {move:+.3f} move confirmed by re-fetch "
                f"({current_price:.3f}) -- real movement, evaluating exit normally."
            )

    _last_observed_price[position.position_id] = current_price

    # Record the price this cycle is about to ACT on, so a replay can see
    # the part of the day the entry path never covered. See
    # price_store.EXIT_SNAPSHOT_SOURCE for the measured gap this closes.
    #
    # PLACED HERE, AFTER CONFIRMATION, ON PURPOSE. Everything above this
    # line exists to decide whether the quote is believable; a price that
    # failed its confirming re-fetch is one this module explicitly refused
    # to act on, and writing it would seed the historical series with the
    # exact phantom quotes the confirmation logic exists to reject. The
    # cost is that prices from the early-return resolution paths go
    # uncaptured -- acceptable, since a replay settles past-dated
    # positions from the observation record (engine._resolution_sweep),
    # not from a price.
    _capture_exit_price(position, token_id, current_price, capture_fidelity_min)

    # Refresh high-water-mark BEFORE evaluating. No exit reads it since
    # the trailing stop was removed (2026-08-17); it is kept current so
    # the persisted peak stays truthful and replay matches live.
    new_hwm = risk_manager.update_high_water_mark(position, current_price)
    if new_hwm != position.high_water_mark:
        storage.update_high_water_mark(position.position_id, new_hwm)
        position.high_water_mark = new_hwm

    # local_hour is passed EXPLICITLY, from this position's own station
    # offset. risk_manager._local_hour()'s default is UTC+8 and stays
    # that way (a station-agnostic fallback the parity tests pin), so
    # leaving this argument off would apply Singapore's edge-decay
    # tightening hour to Tokyo and Karachi -- an hour early for +9, three
    # hours late for +5. Fixing the default alone would have been a
    # no-op precisely because this call site never passed one.
    decision = risk_manager.evaluate_exit(
        position, current_price, local_hour=_local_hour_for(position),
    )

    if decision.should_exit:
        executor.close_position(position, decision)
        _forget_position(position.position_id)

    return decision


def _station_for(position: Position):
    """
    This position's StationConfig, or None if its station is no longer
    registered. Returning None rather than raising is deliberate: the
    exit loop runs over every open position, and one orphaned row must
    not take down monitoring for all the others -- that would strand
    real money behind a KeyError.
    """
    try:
        return config.get_station(position.station_icao)
    except KeyError as exc:
        print(f"[position_manager] {position.position_id}: {exc} -- falling back to the default UTC+8 clock.")
        return None


def _local_today_for(position: Position) -> date:
    """Today's date in the position's own market timezone (see _station_for for the fallback)."""
    return config.local_today(_station_for(position))


def _local_hour_for(position: Position) -> int:
    """
    Current hour (0-23) in the position's own market timezone -- what
    risk_manager's edge-decay tightening must be evaluated against, since
    "10:00 local" is a different instant in Tokyo, Singapore and Karachi.
    """
    station = _station_for(position)
    offset = station.utc_offset_hours if station is not None else config.LOCAL_UTC_OFFSET_HOURS
    return (datetime.now(timezone.utc).hour + offset) % 24


def _is_extreme(price: float) -> bool:
    """
    True if a price sits at either edge of the book (see
    config.MIN_EXIT_PRICE) -- the signature of a resolved market or a
    broken quote, not of a level worth stop-lossing out of.
    """
    return price <= config.MIN_EXIT_PRICE or price >= 1.0 - config.MIN_EXIT_PRICE


def _last_known_price(position: Position) -> float:
    """
    The last price actually OBSERVED for this position, which is the only
    honest baseline for "how far has it moved since we last looked."

    Falls back to entry_price the first time a position is seen in this
    process. Explicitly NOT position.high_water_mark: that is a monotone
    peak, not a per-cycle price, so measuring against it would report
    drawdown-from-peak as if it were one cycle's movement, and flag every
    position that has ever drifted down as a suspicious jump forever.
    """
    return _last_observed_price.get(position.position_id, position.entry_price)


def _forget_position(position_id: str) -> None:
    """Drop a closed position's in-memory tracking so the dicts don't grow without bound."""
    _consecutive_price_failures.pop(position_id, None)
    _consecutive_unknown_resolution.pop(position_id, None)
    _last_observed_price.pop(position_id, None)


def _market_reported_closed(position: Position) -> Optional[bool]:
    """
    Ask Gamma whether this position's bucket market is closed/resolved.
    Returns True/False, or None when the answer is genuinely UNKNOWN
    (station unregistered, Gamma unreachable, event or bucket not listed).

    Unknown is never treated as closed: a position is only ever booked as
    resolved on a positive signal. Only called for prices that already
    look suspicious, so a normal cycle costs no extra Gamma requests.
    """
    try:
        station = config.get_station(position.station_icao)
    except KeyError as exc:
        print(f"[position_manager] cannot check market state for {position.position_id}: {exc}")
        return None

    state = market_discovery.get_market_state(
        station,
        position.target_date,
        bucket_c=position.bucket_c,
        # The station's OWN cross-check bounds, not the frozen module
        # globals: these only feed parse_bucket_label's last-resort
        # edge-label fallback (every real label carries its own degree
        # number), but pointing a Beijing lookup at Singapore's old 25-35
        # window is the kind of leftover that eventually decides
        # something. Nothing here should reference the globals any more.
        bucket_min=station.bucket_min_c,
        bucket_max=station.bucket_max_c,
    )
    if state is None:
        print(
            f"[position_manager] market state UNKNOWN for {position.position_id} "
            f"(Gamma lookup failed or bucket not listed) -- not assuming resolution."
        )
        return None
    return bool(state["closed"])


def _close_as_resolved(
    position: Position,
    confirmed_price: float,
    market_closed: Optional[bool],
    basis: Optional[str] = None,
) -> ExitDecision:
    """
    Close a position whose market has resolved. Deliberately its own code
    path with its own status/exit_reason: a resolution is NOT a stop-loss,
    however much a losing side's 0.00 print may resemble one.

    exit_price is rounded to exactly 1.0 or 0.0 -- a resolved market pays
    par or nothing, and recording the 0.98/0.02-ish quote that happened to
    be on the book at the moment of detection would bake feed noise into
    the permanent P&L record.

    `basis` names where the number came from, for callers that did not get
    it from the book. It is threaded into the log rather than assumed
    because "the book said 0.99" and "the airport's own thermometer said
    31C" are very different claims and the record should not blur them.
    """
    exit_price = 1.0 if confirmed_price >= 0.5 else 0.0
    pnl_pct = risk_manager.compute_pnl_pct(position.entry_price, exit_price)

    decision = ExitDecision(
        position_id=position.position_id,
        should_exit=True,
        reason="resolution",
        current_price=exit_price,
        pnl_pct=pnl_pct,
    )

    where = basis or f"confirmed price {confirmed_price:.3f}"
    print(
        f"[position_manager] {position.position_id}: market RESOLVED "
        f"(gamma_closed={market_closed}, {where}) -- closing at "
        f"{exit_price:.1f} as market_resolved, pnl={pnl_pct:+.1%}. This is NOT a stop-loss."
    )
    executor.close_position(
        position,
        decision,
        status="closed_resolution",
        exit_reason="market_resolved",
    )
    _forget_position(position.position_id)
    return decision


def _close_resolved_without_price(position: Position, token_id: str) -> Optional[ExitDecision]:
    """
    Gamma says this market is closed, but the price feed has been failing.
    Try once more for a price, because the resolution close needs to know
    WHICH side won -- and that is the one thing that must never be guessed.

    Returns the ExitDecision if a price was obtained and the position was
    closed, or None if it had to be left open for manual handling. Closing
    at a coin-flip 1.0-or-0.0 would put a fabricated number straight into
    the P&L record, which is worse than an open position and a loud log.
    """
    print(
        f"[position_manager] {position.position_id}: Gamma reports this market CLOSED while the price feed "
        f"is down -- attempting one final price read to determine which side resolved."
    )
    final_price = market_client.get_current_price_for_side(token_id=token_id, side=position.side)

    if final_price is None:
        # The book is gone, but the book was never the authority on WHO
        # WON -- the weather is, and the market settles on the same
        # airport record this station already stores.
        from_source = _close_from_settlement_source(position, gamma_closed=True)
        if from_source is not None:
            return from_source

        print(
            f"[position_manager] WARNING: {position.position_id} sits on a RESOLVED market but no price can "
            f"be read and no settlement-grade observation exists for {position.target_date}, so the winning "
            f"side cannot be determined. Refusing to guess between 1.0 and 0.0 -- leaving the position OPEN "
            f"for manual close/redemption (${position.size_usd:.2f} at stake)."
        )
        return None

    return _close_as_resolved(position, final_price, True)


def _settlement_grade_reading(position: Position):
    """
    This station's own settlement-grade observation for the position's
    target date, or None.

    STRICTLY the station's `resolution_grade_source` -- not merely the
    best reading available. The whole point is that this is the record
    Polymarket settles on; a proxy-grade reading is a good forecast input
    and a bad settlement authority, and silently accepting one here would
    close positions on the wrong number.
    """
    station = _station_for(position)
    if station is None:
        return None

    source = getattr(station, "resolution_grade_source", None)
    if not source:
        return None

    try:
        rows = storage.load_observations_since(position.station_icao, position.target_date)
    except Exception as exc:  # noqa: BLE001
        # Storage trouble must not close a position on a partial read.
        print(
            f"[position_manager] {position.position_id}: could not read observations "
            f"({type(exc).__name__}) -- not settling from the observation record this cycle."
        )
        return None

    for obs in rows:
        if obs.target_date == position.target_date and obs.source == source:
            return obs
    return None


def _event_bounds(position: Position, station) -> Optional[tuple]:
    """
    The bucket bounds of the position's OWN event, from its token map, or
    None if they can't be established.

    config.STATIONS' bounds are passed only as parse_bucket_label's
    edge-label hint -- the same way ev_engine calls this on the trading
    path -- and derive_bucket_bounds() then rejects any map that isn't a
    contiguous run of EXPECTED_BUCKET_COUNT buckets. A partial map cannot
    quietly produce narrower bounds.
    """
    try:
        token_map = market_discovery.discover_token_map(
            station, position.target_date, station.bucket_min_c, station.bucket_max_c,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[position_manager] {position.position_id}: token map lookup failed "
            f"({type(exc).__name__}) -- cannot establish this event's bucket bounds."
        )
        return None

    if not token_map:
        return None
    return market_discovery.derive_bucket_bounds(token_map)


def _close_from_settlement_source(position: Position, gamma_closed: Optional[bool]) -> Optional[ExitDecision]:
    """
    Close a resolved position using the station's own settlement-grade
    reading, when the order book can no longer be read at all.

    WHY THIS EXISTS. Once a market settles, Polymarket unseeds its book:
    every bucket returns "no orderbook", so get_current_price_for_side()
    returns None forever. The old code correctly refused to guess between
    1.0 and 0.0 and left the position open -- but "open" then meant
    permanently open, emitting an UNMONITORABLE warning every cycle until
    someone cleared it by hand. Five positions were cleared that way
    between 2026-08-09 and 2026-08-14; two more were queued behind them.

    The refusal was right and the resignation was wrong. The winning side
    was never unknowable -- it just wasn't on the book. It is the airport
    record this station already ingests, the same one the backtest settles
    on, and the same one Polymarket resolves against.

    "NEVER GUESS" IS PRESERVED, and narrowed rather than weakened: this
    returns None -- leaving the position open and loud -- whenever the
    settlement-grade observation for that date is missing. A daily maximum
    only exists once the day is over and the source published it, so the
    observation's existence is itself the evidence that the day is done.
    """
    obs = _settlement_grade_reading(position)
    if obs is None:
        return None

    station = _station_for(position)

    # BOUNDS COME FROM THE EVENT, NEVER FROM config.STATIONS.
    #
    # bucket_for_temp() CLAMPS into the bounds it is given, so the bounds
    # decide the answer for any reading at or past an edge -- and
    # config.STATIONS' bounds are documented as a seasonal cross-check that
    # drifts, not as truth. Measured 2026-08-14: 10 of 13 stations had
    # drifted, RJTT by 4C and RKPK/ZBAA by 5C, and 15 settlement-grade
    # readings from the previous fortnight land in a DIFFERENT bucket under
    # config's bounds than under the live event's. ZBAA on 2026-08-12 read
    # 27.0C: the live event settles that as bucket 27, config's stale 30-40
    # clamps it to 30 -- so a winning 27C position would have been written
    # off as a loser, by this function, silently.
    #
    # The token map survives settlement (only the ORDER BOOK is unseeded --
    # discovery still returns all 11 buckets), so the authoritative bounds
    # are available at exactly the moment this runs.
    bounds = _event_bounds(position, station)
    if bounds is None:
        print(
            f"[position_manager] WARNING: {position.position_id} has a settlement reading "
            f"({obs.source} {obs.max_temp_c:.1f}C) but its event's bucket bounds could not be "
            f"discovered, and config's bounds are a drifting cross-check rather than truth. "
            f"Refusing to settle on bounds that may clamp the winner into the wrong bucket -- "
            f"leaving the position OPEN (${position.size_usd:.2f} at stake)."
        )
        return None

    bucket_min, bucket_max = bounds
    if (bucket_min, bucket_max) != (station.bucket_min_c, station.bucket_max_c):
        print(
            f"[position_manager] {position.position_id}: settling on the LIVE event bounds "
            f"{bucket_min}-{bucket_max}C, not config's {station.bucket_min_c}-"
            f"{station.bucket_max_c}C (bounds drift)."
        )

    winning_bucket = settlement.bucket_for_temp(
        obs.max_temp_c,
        bucket_min,
        bucket_max,
        station.bucket_edge_mode,
    )
    exit_price = settlement.resolution_exit_price(
        position.side, position.bucket_c, winning_bucket,
    )

    basis = (
        f"no book left to read; settled from {obs.source} {obs.max_temp_c:.1f}C "
        f"-> winning bucket {winning_bucket}C, so {position.bucket_c}C "
        f"{position.side} pays {exit_price:.1f}"
    )
    return _close_as_resolved(position, exit_price, gamma_closed, basis=basis)


def _note_unknown_resolution(position: Position, price: float) -> int:
    """
    Record one cycle where a position looked possibly-resolved but Gamma
    couldn't confirm, and log it -- escalating once the position has been
    stuck in that state for UNMONITORABLE_CYCLES_WARN consecutive cycles.
    Returns the new count.

    The escalation matters because this state HOLDS the position: no exit
    can fire while resolution is unknown, so a position stuck here is one
    nobody is protecting, and it must not stay quiet about it.
    """
    count = _consecutive_unknown_resolution.get(position.position_id, 0) + 1
    _consecutive_unknown_resolution[position.position_id] = count

    print(
        f"[position_manager] {position.position_id}: price {price:.3f} looks possibly-resolved but Gamma "
        f"cannot confirm -- holding this cycle only, no exit action taken (consecutive: {count})"
    )
    if count >= UNMONITORABLE_CYCLES_WARN:
        print(
            f"[position_manager] WARNING: {position.position_id} has been stuck UNRESOLVABLE for {count} "
            f"consecutive cycles -- price reads {price:.3f} but Gamma can't say whether the market closed, "
            f"so no exit can fire and ${position.size_usd:.2f} is effectively unprotected. Check the Gamma "
            f"lookup for this bucket and resolve this position by hand if it has in fact settled."
        )
    return count


def _note_price_failure(position: Position) -> int:
    """
    Record one failed price fetch for a position and log it, escalating
    to a warning once the position has been unmonitorable for
    UNMONITORABLE_CYCLES_WARN consecutive cycles. Returns the new count.
    """
    count = _consecutive_price_failures.get(position.position_id, 0) + 1
    _consecutive_price_failures[position.position_id] = count

    print(
        f"[position_manager] could not fetch live price for {position.position_id} "
        f"-- skipping this cycle, will retry next scan (consecutive failures: {count})"
    )
    if count >= UNMONITORABLE_CYCLES_WARN:
        print(
            f"[position_manager] WARNING: {position.position_id} has been UNMONITORABLE for {count} "
            f"consecutive cycles -- ${position.size_usd:.2f} is open with no working price feed, which "
            f"means no working stop-loss either. Investigate the feed or close this position by hand."
        )
    return count


def _token_id_for(position: Position) -> str:
    """
    Returns the Polymarket CLOB token_id for this position's specific
    (station, bucket, side). This used to be an unwired gap -- nothing
    stored this at entry time, so exit-side price checks couldn't run
    at all. Fixed by threading token_id through entry_manager.EntryDecision
    -> executor.open_position() -> Position.token_id, so it's simply read
    back here rather than needing rediscovery.

    Still raises for positions that predate this fix (token_id=None) --
    that's a real, if narrower, gap: any position opened before this
    change has no token_id on record and genuinely cannot be priced
    without a manual backfill.
    """
    if position.token_id is None:
        raise NotImplementedError(
            f"Position {position.position_id} has no token_id on record "
            f"(likely opened before entry_manager -> executor -> Position "
            f"wiring existed). Cannot price-check without a manual backfill."
        )
    return position.token_id


def print_summary(decisions: List[ExitDecision]) -> None:
    """Human-readable console output for one exit-check cycle."""
    if not decisions:
        print("[position_manager] no open positions to check.")
        return

    print(f"\n=== Position Exit Check — {datetime.now(timezone.utc).isoformat()} ===")
    for d in decisions:
        flag = "EXIT" if d.should_exit else "hold"
        print(
            f"  {d.position_id:<40} {flag:>5}  reason={d.reason:<12} "
            f"price={d.current_price:.3f}  pnl={d.pnl_pct:+.1%}"
        )
    print()
