"""
risk_manager.py

PURPOSE
-------
Decides whether an OPEN position should be exited right now, based on
live price movement -- independent of whether the underlying weather
outcome has resolved yet.

Two mechanisms, checked in priority order:
  1. Hard stop-loss from entry. It does NOT always apply: two ENTRY-price
     bands are exempt from it, config.LOTTERY_PRICE_THRESHOLD below and
     config.STOP_EXEMPT_ABOVE_PRICE above, and as of 2026-08-24 it is also
     skipped whenever the CURRENT price has fallen to config.MIN_EXIT_PRICE
     or below, where selling is weakly dominated by holding. Within what
     remains, no "let it run" logic overrides cutting a loss past this bar.
  2. Fixed profit-take -- the only upside exit, and the one exit that
     applies at every entry price.

THE STOP NOW COVERS ONE BAND, NOT EVERY POSITION (upper half added
2026-08-20). Both exemptions come from the same finding read at its two
ends: the stop is a price-noise filter, and it is only worth having where
the noise it filters is smaller than the move it reacts to. Below 0.15
the threshold distance is under Polymarket's 1-cent tick, so it fires on
book wobble. At 0.45 and above the distance is large in cents but small
against a bucket's intraday range before the day's maximum is set, so it
fires on positions that go on to WIN -- measured at 33% precision against
settlement at WMKK, versus 83% below 0.30, and falling monotonically with
entry price across all 11 stations. config.STOP_EXEMPT_ABOVE_PRICE carries
the full measurement, the sizing of what it buys, and its limits.

Both tighten after the edge-decay hour (config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL,
10:00 local) -- consistent with the edge-decay analysis: once the morning's
edge window closes, there's no new information coming to justify riding out
volatility, so gains and losses should both be locked in faster.

THAT HOUR IS NO LONGER THE HOUR ENTRIES STOP. Entries closed at 10:00 when
this was written; since 2026-08-17 they close at 08:00 (config.SCHEDULE_WINDOWS),
because trades opened after 08:00 measured negative. The tightening was
deliberately left at 10:00, so there are now two hours a day where the entry
window is shut and these thresholds are still on their loose setting. Whether
tightening should follow the entry close down to 08:00 is an open question
about exits; it needs the capital a stop frees to be modeled, which
stop_loss_audit.py cannot do. Do not read "the edge window closes" above as
"entries stop" -- they are two different instants now.

THERE WAS A THIRD: A TRAILING STOP. REMOVED 2026-08-17, and worth knowing
why before anyone reintroduces one. It armed once a position's peak gain
crossed an activation threshold set BELOW the fixed take, then exited on
a give-back off that peak, so a strong move could pay out more than a
flat cap. It never did. Across the cohort window (Aug 6-16, 10 stations,
91 positions) it produced ZERO exits, in four separate profit-take
configurations including one with the fixed take disabled entirely.

Instrumenting the branch (commit 16a0949) showed why, and it was not the
reason the constants suggested. Of 907 evaluations, 327 were lottery
entries it never applied to, it armed on 7 of the remaining 580 ticks,
and it never once observed a give-back -- so the fee bar it was suspected
of failing was never even reached. A trailing stop needs TWO evaluations
to act: one with the position past activation but short of the fixed
take, and a LATER one showing the retreat. At roughly ten ticks per
position on hourly cycles over a 1-cent book, a run-up crosses the whole
activation-to-target band inside a single step, so that intermediate
state does not occur.

It is a sampling-frequency problem, not a threshold one. Retuning cannot
fix it; only a faster exit cadence while positions are live would make a
trailing stop reachable. If that cadence ever arrives, this is worth
revisiting -- and the history is in git, not lost.

Position.high_water_mark is RETAINED. It no longer drives any decision,
but it is a persisted column with real values on historical rows, and it
is the record of what a position was worth at its best.

WHAT THE THRESHOLDS ARE MEASURED AGAINST
----------------------------------------
Every threshold is a fraction of the position's RISK UNIT,
risk_unit() = min(entry_price, 1 - entry_price), NOT of entry price.

Entry price was the wrong basis because price is capped at 1.00 and the
thresholds are not. A +50% profit-take needs price >= 1.50 x entry, so
it is arithmetically unreachable above an entry of 0.667, and the
tightened take dies above 0.80. A position entered at 0.85 therefore
had no upside exit AT ALL -- only the stop-loss and resolution could
ever fire -- while carrying a
30% stop against a maximum possible gain of +17.6%.

min(entry, 1 - entry) is the distance to the nearer boundary, so no
threshold expressed against it can ever demand a price outside [0, 1]:
the take-profit is reachable at every entry price. Below 0.50 the risk
unit IS entry price, so this is a NO-OP for every entry at or below
0.50 -- identical exits, same prices, same reasons. Only the 0.50-0.75
band changes, which is the band that was mispriced. (Entries above 0.75
no longer exist; see config.MAX_ENTRY_PRICE.)

(The now-removed trailing stop was moved onto this same basis for the
same reason: as a fraction of the high-water mark it floored a trailing
exit at a fixed +6.25% gross gain regardless of entry price, below
round-trip taker fees, so it closed winners into a net loss and recorded
them as wins.)

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
            "lottery_profit_take_pct": config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT,
            "stop_loss_pct": config.TIGHTENED_STOP_LOSS_PCT,
        }
    return {
        "profit_take_pct": config.PROFIT_TAKE_PCT,
        "lottery_profit_take_pct": config.LOTTERY_PROFIT_TAKE_PCT,
        "stop_loss_pct": config.STOP_LOSS_PCT,
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

    # The mirror carve-out (config.STOP_EXEMPT_ABOVE_PRICE, 2026-08-20).
    # Expensive entries skip the stop too, for the opposite reason: not
    # that its distance is too small to mean anything, but that it is
    # large enough to be triggered by an ordinary intraday swing on a day
    # whose maximum has not been set yet. Scored against settlement on
    # the paper book, the stop's precision falls monotonically with entry
    # price -- 83% below 0.30, 33% at or above 0.45 at WMKK -- so up here
    # it fires on eventual WINNERS two times in three. The numbers, the
    # mechanism and the limits are all in config.py above the constant.
    #
    # Downside protection for these is unchanged in kind from the lottery
    # case: resolution detection in position_manager still closes them,
    # and MAX_ENTRY_PRICE still caps how much stake can be at risk.
    is_stop_exempt_high = position.entry_price >= config.STOP_EXEMPT_ABOVE_PRICE

    # The third carve-out, and the ONLY one keyed on the CURRENT price rather
    # than the entry price (2026-08-24). At or below config.MIN_EXIT_PRICE a
    # sale raises almost nothing -- at a bid of exactly 0.0000 it raises
    # NOTHING -- while forfeiting the whole remaining payout. Selling there is
    # weakly dominated by holding: both pay 0 if the bucket loses, and only
    # holding pays if it wins. (Strict dominance is the 0.0000 case; across the
    # rest of the band the argument is that the stop exists to cut a loss that
    # could still get WORSE, and at most MIN_EXIT_PRICE per share of loss
    # remains to cut.)
    #
    # This is NOT the "is this price real?" question position_manager already
    # answers by re-fetching and asking Gamma. It is what to DO once that
    # answer comes back "yes -- the market is open and it really is bidding
    # zero", which is the branch that used to fall straight through to here.
    #
    # MEASURED on the paper book 2026-08-24, and the honest headline is that
    # this recovers NO money on the record so far. 4 of 131 closed stops match
    # this condition, but 2 are WSSS 2026-08-03 rows at entry 0.04/0.05 that
    # LOTTERY_PRICE_THRESHOLD already exempts today, so it bites on 2: ZBAA
    # 2026-08-24 bkt32 @0.40 and ZBAA 2026-08-07 bkt35 @0.22, both sold at a
    # gross bid of 0.0000 for -100%. BOTH settled to a loss, so selling at zero
    # paid exactly what holding would have. Net P&L impact on history: $0.00.
    #
    # It is a correctness fix, not a P&L fix, and it is worth making anyway
    # because the action is DOMINATED -- never better, sometimes worse, so
    # removing it costs nothing. Two concrete harms it ends:
    # "closed_stop_loss" is in config.COOLDOWN_COUNTED_EXIT_STATUSES, so such a
    # row ALSO blocked re-entry on that bucket for the rest of the day as though
    # the market had rejected the entry; and it books an exit reason and price
    # that misdescribe what happened -- the exact double corruption
    # config.MIN_EXIT_PRICE's own comment warns about, arrived at through the
    # one path that comment did not anticipate. The zero-bid position that
    # WOULD have won has not occurred yet in 131 stops; that is the case this
    # exists to catch.
    #
    # DELIBERATELY NOT MIRRORED at the top of the book. Selling at or above
    # 1 - MIN_EXIT_PRICE hands over very nearly the full payout with certainty
    # and frees the position; that is a genuine trade-off, not a dominated one.
    #
    # Downside protection is unchanged in kind from the other two carve-outs:
    # resolution detection in position_manager still closes these, and it is
    # the only thing that ever could down here.
    is_worthless_bid = current_price <= config.MIN_EXIT_PRICE

    # 1. Hard stop-loss -- always checked first, overrides everything else.
    #    NOT "a safety net that always applies": there are now three bands
    #    where it does not apply at all -- two keyed on entry price, one on
    #    the current price. See the module docstring.
    if (
        not is_lottery
        and not is_stop_exempt_high
        and not is_worthless_bid
        and (position.entry_price - current_price) >= thresholds["stop_loss_pct"] * unit
    ):
        return ExitDecision(
            position_id=position.position_id,
            should_exit=True,
            reason="stop_loss",
            current_price=current_price,
            pnl_pct=pnl_pct,
        )

    # 2. Fixed profit-take -- the only upside exit, and the hard cap on
    #    greed. This system's edge is a morning-only phenomenon that decays
    #    through the day, so a position sitting at +PROFIT_TAKE_PCT is
    #    cashed rather than ridden.
    #
    #    Measured against the risk unit rather than entry price, this target
    #    is attainable at EVERY entry price. On the old basis it needed
    #    price >= 1.5 x entry, which no market can print above an entry of
    #    0.667.
    #
    #    LOTTERY ENTRIES GET THEIR OWN DISTANCE (config.LOTTERY_PROFIT_TAKE_PCT),
    #    which defaults to this same number -- so this is today a no-op and
    #    the branch exists to be swept, not because a better value is known.
    #    The carve-out above turns the STOP off for these entries on the
    #    grounds that the threshold distance is sub-tick and that cutting a
    #    ticket on noise forfeits the tail that justifies buying it. Both
    #    halves of that argument apply to the upside exit too, and neither
    #    had ever been measured against it. backtest/take_sweep.py is the
    #    measurement; until it can answer, the default keeps behaviour
    #    identical.
    take_pct = (
        thresholds["lottery_profit_take_pct"] if is_lottery
        else thresholds["profit_take_pct"]
    )
    if (current_price - position.entry_price) >= take_pct * unit:
        return ExitDecision(
            position_id=position.position_id,
            should_exit=True,
            reason="take_profit",
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
