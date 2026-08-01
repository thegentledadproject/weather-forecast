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
# monotone non-decreasing peak that exists to drive the trailing stop, so
# using it here measured drawdown-from-peak instead of per-cycle movement.
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


def check_and_exit_positions(station_icao: Optional[str] = None) -> List[ExitDecision]:
    """
    Run one full exit-check cycle. Returns the list of ExitDecisions
    made this cycle (including "hold" decisions), for logging/summary.
    Positions skipped entirely this cycle (price unavailable, or a price
    that failed confirmation) produce no decision -- they are left open
    and logged, never exited on an unverified number.
    """
    open_positions = storage.load_open_positions(station_icao=station_icao)
    decisions = []

    for position in open_positions:
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
            if failures >= UNMONITORABLE_CYCLES_WARN and _market_reported_closed(position) is True:
                resolved = _close_resolved_without_price(position, token_id)
                if resolved is not None:
                    decisions.append(resolved)
            continue
        _consecutive_price_failures.pop(position.position_id, None)

        last_observed = _last_known_price(position)
        move = abs(current_price - last_observed)
        is_extreme = _is_extreme(current_price)
        is_big_move = move > config.MAX_SINGLE_CYCLE_MOVE
        # A bucket whose date has passed is resolved by definition, whatever
        # the book still prints -- a stale but plausible last-traded quote
        # would otherwise never trip the extreme-price check at all.
        is_past_dated = position.target_date < date.today()

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
                continue

            if abs(confirmed_price - current_price) > CONFIRMATION_TOLERANCE:
                print(
                    f"[position_manager] {position.position_id}: price {current_price:.3f} NOT confirmed "
                    f"(re-fetch says {confirmed_price:.3f}, tolerance {CONFIRMATION_TOLERANCE:.2f}) -- the "
                    f"feed disagrees with itself, so taking NO exit action this cycle, position left open."
                )
                continue

            current_price = confirmed_price
            _last_observed_price[position.position_id] = current_price

            # Resolution is only a live hypothesis for an extreme price or
            # a past-dated bucket. A big move alone, once confirmed, is
            # just a big move -- no Gamma round-trip, straight to normal
            # evaluation below.
            if _is_extreme(current_price) or is_past_dated:
                market_closed = _market_reported_closed(position)

                if market_closed is True:
                    decisions.append(_close_as_resolved(position, current_price, market_closed))
                    continue

                if market_closed is None:
                    # Can't confirm either way. Hold THIS CYCLE ONLY -- and
                    # count it, so an indefinitely unresolvable position
                    # gets escalated instead of quietly sitting forever.
                    _note_unknown_resolution(position, current_price)
                    decisions.append(ExitDecision(
                        position_id=position.position_id,
                        should_exit=False,
                        reason="resolution_unknown",
                        current_price=current_price,
                        pnl_pct=risk_manager.compute_pnl_pct(position.entry_price, current_price),
                    ))
                    continue

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

        # Refresh high-water-mark BEFORE evaluating -- the trailing stop
        # needs to see this cycle's peak, not last cycle's.
        new_hwm = risk_manager.update_high_water_mark(position, current_price)
        if new_hwm != position.high_water_mark:
            storage.update_high_water_mark(position.position_id, new_hwm)
            position.high_water_mark = new_hwm

        decision = risk_manager.evaluate_exit(position, current_price)
        decisions.append(decision)

        if decision.should_exit:
            executor.close_position(position, decision)
            _forget_position(position.position_id)

    return decisions


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
    peak for the trailing stop, so measuring against it would report
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
        bucket_min=config.BUCKET_MIN_C,
        bucket_max=config.BUCKET_MAX_C,
    )
    if state is None:
        print(
            f"[position_manager] market state UNKNOWN for {position.position_id} "
            f"(Gamma lookup failed or bucket not listed) -- not assuming resolution."
        )
        return None
    return bool(state["closed"])


def _close_as_resolved(position: Position, confirmed_price: float, market_closed: Optional[bool]) -> ExitDecision:
    """
    Close a position whose market has resolved. Deliberately its own code
    path with its own status/exit_reason: a resolution is NOT a stop-loss,
    however much a losing side's 0.00 print may resemble one.

    exit_price is rounded to exactly 1.0 or 0.0 -- a resolved market pays
    par or nothing, and recording the 0.98/0.02-ish quote that happened to
    be on the book at the moment of detection would bake feed noise into
    the permanent P&L record.
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

    print(
        f"[position_manager] {position.position_id}: market RESOLVED "
        f"(gamma_closed={market_closed}, confirmed price {confirmed_price:.3f}) -- closing at "
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
        print(
            f"[position_manager] WARNING: {position.position_id} sits on a RESOLVED market but no price can "
            f"be read, so the winning side cannot be determined. Refusing to guess between 1.0 and 0.0 -- "
            f"leaving the position OPEN for manual close/redemption (${position.size_usd:.2f} at stake)."
        )
        return None

    return _close_as_resolved(position, final_price, True)


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
