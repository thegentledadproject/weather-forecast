"""
risk_manager.py

PURPOSE
-------
Decides whether an OPEN position should be exited right now, based on
live price movement -- independent of whether the underlying weather
outcome has resolved yet.

Three mechanisms, checked in priority order:
  1. Hard stop-loss from entry -- a safety net that always applies,
     regardless of trailing state. No amount of "let it run" logic
     should override cutting a loss past this bar.
  2. Trailing stop -- once a position's high-water-mark gain crosses
     the activation threshold, this supersedes the fixed profit-take
     below. The position is allowed to keep running past the fixed
     target, protected by a stop that trails the peak price down by a
     fixed distance. Lets strong moves pay out more than a flat cap
     would, while still locking in most of the gain on a reversal.
  3. Fixed profit-take -- fallback for positions that haven't reached
     the trailing-activation threshold yet. Once trailing activates,
     this step is superseded (see TRAILING_STOP_ACTIVATION_PCT being
     set below PROFIT_TAKE_PCT in config.py).

All three thresholds tighten after the edge-decay hour (config.py) --
consistent with the edge-decay analysis: once the morning's edge
window closes, there's no new information coming to justify riding
out volatility, so gains and losses should both be locked in faster.

WHAT THE THRESHOLDS ARE MEASURED AGAINST
----------------------------------------
Every threshold is a fraction of the position's RISK UNIT,
risk_unit() = min(entry_price, 1 - entry_price), NOT of entry price.

Entry price was the wrong basis because price is capped at 1.00 and the
thresholds are not. A +50% profit-take needs price >= 1.50 x entry, so
it is arithmetically unreachable above an entry of 0.667; the tightened
take dies above 0.80, and the two trailing activations above 0.80 and
0.87. A position entered at 0.85 therefore had no upside exit AT ALL --
only the stop-loss and resolution could ever fire -- while carrying a
30% stop against a maximum possible gain of +17.6%.

min(entry, 1 - entry) is the distance to the nearer boundary, so no
threshold expressed against it can ever demand a price outside [0, 1]:
the take-profit is reachable at every entry price. Below 0.50 the risk
unit IS entry price, so this is a NO-OP for every entry at or below
0.50 -- identical exits, same prices, same reasons. Only the 0.50-0.75
band changes, which is the band that was mispriced. (Entries above 0.75
no longer exist; see config.MAX_ENTRY_PRICE.)

The trailing give-back moved onto the same basis. It used to be a
fraction of the high-water mark, which floored a trailing exit at a
fixed +6.25% gross gain (0.85 x 1.25 - 1) regardless of entry price --
below round-trip taker fees, so the trailing stop was reliably closing
winners into a net loss and recording them as wins. It is now a fixed
distance off the peak, and a breach that would not clear its own fees
by config.TRAILING_EXIT_COST_MARGIN does not fire at all.

DEPENDENCIES
------------
datetime (standard library)
config.py, models.py, ev_engine.py (local -- fee formula only)
"""

from datetime import datetime, timezone
from typing import Optional

import config
import ev_engine
from models import Position, ExitDecision

# Smallest risk unit any threshold is computed against. Guards the
# degenerate entry prices (<= 0, >= 1) that entry_manager rejects but
# stored history could still contain: a zero risk unit collapses every
# threshold distance to zero, which would fire a stop-loss on any price
# at all. One cent is Polymarket's tick -- the smallest distance that
# can mean anything.
MIN_RISK_UNIT = 0.01


def _local_hour(tz_offset_hours: int = 8) -> int:
    """
    Current local hour for SGT/MYT (UTC+8), both frameworks' stations.
    Hardcoded offset rather than a timezone library dependency, since
    both WSSS and WMKK share this offset -- revisit if a station in a
    different timezone is added later.
    """
    utc_now = datetime.now(timezone.utc)
    return (utc_now.hour + tz_offset_hours) % 24


def _active_thresholds(local_hour: Optional[int] = None) -> dict:
    """
    Return the full threshold set appropriate for the given time of day.

    local_hour defaults to None, which reads the real wall clock via
    _local_hour() -- unchanged behaviour for every live caller. Passing an
    explicit hour (0-23) uses that instead, which is what a simulated
    replay needs: a backtest re-running a past morning must apply the
    thresholds that were active AT THAT SIMULATED HOUR, not whatever hour
    the backtest itself happens to be executed at.
    """
    if local_hour is None:
        local_hour = _local_hour()
    if local_hour >= config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL:
        return {
            "profit_take_pct": config.TIGHTENED_PROFIT_TAKE_PCT,
            "stop_loss_pct": config.TIGHTENED_STOP_LOSS_PCT,
            "trailing_activation_pct": config.TIGHTENED_TRAILING_STOP_ACTIVATION_PCT,
            "trailing_stop_pct": config.TIGHTENED_TRAILING_STOP_PCT,
        }
    return {
        "profit_take_pct": config.PROFIT_TAKE_PCT,
        "stop_loss_pct": config.STOP_LOSS_PCT,
        "trailing_activation_pct": config.TRAILING_STOP_ACTIVATION_PCT,
        "trailing_stop_pct": config.TRAILING_STOP_PCT,
    }


def compute_pnl_pct(entry_price: float, price: float) -> float:
    """
    Unrealized P&L as a fraction of entry price, for an arbitrary
    reference price (current price, or high-water-mark). Positive = gain.
    Works identically for YES and NO positions, since price is already
    the price for whichever side the position was entered on.
    """
    if entry_price <= 0:
        return 0.0
    return (price - entry_price) / entry_price


def risk_unit(entry_price: float) -> float:
    """
    The distance from this entry price to the NEARER edge of the book --
    min(entry, 1 - entry) -- which is the basis every exit threshold is
    measured against. See the module docstring for why entry price alone
    was the wrong denominator.

    Below 0.50 this returns entry_price exactly, which is what makes the
    reformulation a no-op for the whole cheap-and-mid range.
    """
    return max(min(entry_price, 1.0 - entry_price), MIN_RISK_UNIT)


def taker_fee_per_share(price: float) -> float:
    """
    Polymarket's taker fee for one leg, in dollars per share.

    ev_engine.taker_fee_pct_of_notional() returns the fee as a fraction
    of notional, and notional per share is just the price, so the per-
    share cost is rate(price) x price. Delegated to ev_engine rather than
    restated here so the entry side, the exit side and the backtest can
    never disagree about what Polymarket charges.

    Applies to TAKER FILLS only. Redeeming a resolved position is not a
    trade and carries no taker fee -- see executor.close_position(),
    which skips this for resolution closes.
    """
    return ev_engine.taker_fee_pct_of_notional(price) * price


def round_trip_fee_per_share(entry_price: float, exit_price: float) -> float:
    """
    Estimated taker fees for BOTH legs of a round trip, in dollars per
    share -- the bar an upside exit has to clear to be worth taking.

    Exit-side SPREAD is not included -- nothing in this codebase models
    it yet (the exit quote is a single top-of-book number). That is what
    config.TRAILING_EXIT_COST_MARGIN's headroom is standing in for.
    """
    return taker_fee_per_share(entry_price) + taker_fee_per_share(exit_price)


def update_high_water_mark(position: Position, current_price: float) -> float:
    """
    Returns the position's updated high-water-mark given the latest
    observed price -- does not mutate the position or touch storage;
    callers (position_manager.py) are responsible for persisting the
    result if it changed.
    """
    return max(position.high_water_mark, current_price)


def evaluate_exit(
    position: Position,
    current_price: float,
    local_hour: Optional[int] = None,
) -> ExitDecision:
    """
    Core decision function. Assumes position.high_water_mark is
    already up to date for this cycle (position_manager.py updates it
    before calling this). Pure logic -- no I/O, no side effects, easy
    to unit-test independent of live price feeds.

    local_hour is threaded straight through to _active_thresholds():
    None (the default) reads the real wall clock exactly as before, and
    an explicit hour (0-23) pins the edge-decay tightening to a
    caller-supplied time. That makes the function fully pure when the
    hour is supplied -- a replay or a unit test can then evaluate the
    same position at 06:00 and at 14:00 and get the two genuinely
    different answers the live system would have given.
    """
    thresholds = _active_thresholds(local_hour=local_hour)
    pnl_pct = compute_pnl_pct(position.entry_price, current_price)

    # Every threshold below is a DISTANCE in dollars/share: the configured
    # fraction times this position's risk unit. See the module docstring.
    unit = risk_unit(position.entry_price)

    # Lottery-priced entries (see LOTTERY_PRICE_THRESHOLD in config.py)
    # skip BOTH price-noise exits. Below that entry price the threshold
    # distances are sub-tick, so any book wobble "triggers" them, and they
    # convert "win p% of the time" into "win only if the price never dips
    # two cents first" -- forfeiting the exact tail scenarios that justify
    # the ticket. Max loss is the stake, accepted at entry and sized tiny
    # by Kelly. Downside exits for these come from resolution detection
    # (position_manager), and the upside exit is the fixed take below,
    # which is a real move to a real level rather than a give-back off a
    # peak that may itself be one tick of noise.
    is_lottery = position.entry_price < config.LOTTERY_PRICE_THRESHOLD

    # 1. Hard stop-loss -- always checked first, overrides everything else.
    if not is_lottery and (position.entry_price - current_price) >= thresholds["stop_loss_pct"] * unit:
        return ExitDecision(
            position_id=position.position_id,
            should_exit=True,
            reason="stop_loss",
            current_price=current_price,
            pnl_pct=pnl_pct,
        )

    # 2. Trailing stop breach -- checked before the fixed take so a peak
    #    that has already given back TRAILING_STOP_PCT exits as what it
    #    is: a trailing-stop event, not a profit-take.
    peak_gain = position.high_water_mark - position.entry_price
    trailing_active = (not is_lottery) and peak_gain >= thresholds["trailing_activation_pct"] * unit
    if trailing_active:
        give_back = position.high_water_mark - current_price
        if give_back >= thresholds["trailing_stop_pct"] * unit:
            # The trailing stop exists to BANK a profit. A breach whose
            # gross gain doesn't cover both legs of taker fees banks
            # nothing -- it pays the exchange to flatten a winner, then
            # records it as one. Hold instead and let the position keep
            # trailing; the hard stop-loss above is still underneath it,
            # so this can only ever delay an upside exit, never a
            # downside one.
            gross_gain = current_price - position.entry_price
            cost_bar = config.TRAILING_EXIT_COST_MARGIN * round_trip_fee_per_share(
                position.entry_price, current_price,
            )
            if gross_gain >= cost_bar:
                return ExitDecision(
                    position_id=position.position_id,
                    should_exit=True,
                    reason="trailing_stop",
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                )

    # 3. Fixed profit-take -- the hard cap on greed. Checked BEFORE the
    #    trailing-active hold: this system's edge is a morning-only
    #    phenomenon that decays through the day, so a position sitting at
    #    +PROFIT_TAKE_PCT is cashed rather than ridden in the hope the
    #    trailing stop locks in more. (Until 2026-08-02 this check sat
    #    below an early "trailing is active, hold" return; since
    #    update_high_water_mark() guarantees hwm >= price, any pnl at the
    #    take level had always activated trailing first, and this branch
    #    was UNREACHABLE -- zero take_profit exits were possible, live or
    #    simulated. Found by the backtest's reachability sweep.)
    #
    #    Reachability again, for a different reason: measured against the
    #    risk unit rather than entry price, this target is now attainable
    #    at EVERY entry price. On the old basis it needed price >= 1.5 x
    #    entry, which no market can print above an entry of 0.667.
    if (current_price - position.entry_price) >= thresholds["profit_take_pct"] * unit:
        return ExitDecision(
            position_id=position.position_id,
            should_exit=True,
            reason="take_profit",
            current_price=current_price,
            pnl_pct=pnl_pct,
        )

    if trailing_active:
        # Trailing is armed and neither it nor the fixed take has been
        # hit -- hold, and note trailing mode in the reason
        # (informational only, doesn't change should_exit).
        return ExitDecision(
            position_id=position.position_id,
            should_exit=False,
            reason="trailing_active",
            current_price=current_price,
            pnl_pct=pnl_pct,
        )

    return ExitDecision(
        position_id=position.position_id,
        should_exit=False,
        reason="hold",
        current_price=current_price,
        pnl_pct=pnl_pct,
    )
