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
     fixed percentage. Lets strong moves pay out more than a flat cap
     would, while still locking in most of the gain on a reversal.
  3. Fixed profit-take -- fallback for positions that haven't reached
     the trailing-activation threshold yet. Once trailing activates,
     this step is superseded (see TRAILING_STOP_ACTIVATION_PCT being
     set below PROFIT_TAKE_PCT in config.py).

All three thresholds tighten after the edge-decay hour (config.py) --
consistent with the edge-decay analysis: once the morning's edge
window closes, there's no new information coming to justify riding
out volatility, so gains and losses should both be locked in faster.

DEPENDENCIES
------------
datetime (standard library)
config.py, models.py (local)
"""

from datetime import datetime, timezone
from typing import Optional

import config
from models import Position, ExitDecision


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

    # 1. Hard stop-loss -- always checked first, overrides everything else.
    if pnl_pct <= -thresholds["stop_loss_pct"]:
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
    hwm_pnl_pct = compute_pnl_pct(position.entry_price, position.high_water_mark)
    trailing_active = hwm_pnl_pct >= thresholds["trailing_activation_pct"]
    if trailing_active:
        drawdown_from_peak = (position.high_water_mark - current_price) / position.high_water_mark
        if drawdown_from_peak >= thresholds["trailing_stop_pct"]:
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
    if pnl_pct >= thresholds["profit_take_pct"]:
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
