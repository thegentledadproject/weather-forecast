"""
risk_manager.py

PURPOSE
-------
Decides whether an OPEN position should be exited right now, based on
live price movement -- independent of whether the underlying weather
outcome has resolved yet.

Two mechanisms, checked in priority order:
  1. Hard stop-loss from entry. It does NOT always apply: entries below
     config.LOTTERY_PRICE_THRESHOLD are exempt, and as of 2026-08-24 it is
     also skipped whenever the CURRENT price has fallen to
     config.MIN_EXIT_PRICE or below, where selling is weakly dominated by
     holding. A third exemption for expensive entries
     (config.STOP_EXEMPT_ABOVE_PRICE) ran 2026-08-20 to 08-27 and is now
     switched off -- see below. Within what remains, no "let it run" logic
     overrides cutting a loss past this bar.
  2. Fixed profit-take -- the only upside exit, and the one exit that
     applies at every entry price.

THE UPPER CARVE-OUT IS OFF AGAIN (added 2026-08-20, REVERTED 2026-08-27).
It came from the same finding as the lottery one read at the other end: the
stop is a price-noise filter, worth having only where the noise it filters
is smaller than the move it reacts to. Below 0.15 the threshold distance is
small enough to be swallowed by the book, so it fires on wobble. At 0.45 and
above the distance is large in cents but small against a bucket's intraday
range before the day's maximum is set, so it fires on positions that go on
to WIN -- 45% precision post-deploy against 68-77% below 0.45, replicating
the pre-deploy measurement.

That argument survived contact with production. The P&L did not: over 21
exempt positions and $255.95 of stake, restoring the stop was worth +$0.44.
Its savings on correct fires almost exactly cancelled its cost on false
ones. What decided the revert was variance -- the band's worst loss went
from -$4.80 with the stop to -$16.76 without, and Kelly sizes UP exactly
where the carve-out switched the stop off, so the coin-flip band held the
biggest positions and none of the protection. config.STOP_EXEMPT_ABOVE_PRICE
carries both measurements and what would justify turning it back on.

THAT -$4.80 DOES NOT REPRODUCE (2026-09-01). It is the WMKK 2026-08-26 b32
NO @0.63, and its recorded path has no quote anywhere near the 0.519 stop
level: the bid sat at 0.60 for eleven hours, never below 0.57, and then
printed 0.05 at 08:01:11 UTC, 330 seconds after the previous snapshot. A
re-armed stop fills at 0.05, not at a level -- -$15.49 against -$16.76 held,
so it saves $1.27, not $11.96. -$4.80 implies a fill at 0.4496, a price that
never quoted. The variance argument for the revert is not wrong in direction,
but its one worked example is off by roughly 12x, and the number is worth
re-deriving from paths rather than from stop levels before it decides
anything else. See config.STOP_EXEMPT_ABOVE_PRICE, which carries the same
correction.

THE SUB-TICK CLAIM ABOVE WAS ALSO WRONG, and is corrected in the wording
now. The distance is 0.30 x min(entry, 1 - entry), so at entry 0.149 it is
4.5 cents -- ten times a tick. It is under one tick only below entry 0.033.
What actually swallows the stop down there is the SPREAD, which on this book
runs a median 2 cents: at entry 0.15 the distance is 4.5 cents and the spread
alone has been measured at 5. LOTTERY_PRICE_THRESHOLD is sited about right
and was justified by the wrong quantity. Note it is a strict `<`, so an entry
at exactly 0.15 is armed -- deliberately left alone rather than moved to
`<=`, because the spread fix below removes the reason that boundary was
hurting and a threshold move needs its own evidence.

The code path below is intact and reads the constant at call time, so
re-enabling is a one-value change rather than a revert of a revert.

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


def stop_basis_price(position: Position) -> float:
    """
    WHERE THE STOP MEASURES FROM -- the entry-side BID, falling back to
    entry_price when no bid was recorded.

    The stop is a movement filter, and an open position is marked at the
    bid (position_manager -> get_current_price_for_side), while entry_price
    is the ask an entry pays. Comparing the two charges the whole bid-ask
    spread against the stop's budget before the market has moved at all --
    a median 24% of the distance over 511 closed positions, and 100% or
    more of it on 7% of them. See evaluate_exit() for the full measurement.

    A FUNCTION, NOT AN EXPRESSION INSIDE evaluate_exit(), because
    stop_loss_audit.py restates this same rule in its own arithmetic to
    score history. Two copies of "where does the stop start" is how the
    audit ends up reporting a threshold production does not use.

    None means the entry-side book was never recorded (rows predating
    Position.entry_bid, and manual_trigger). Those keep the basis they were
    opened under.
    """
    return position.entry_bid if position.entry_bid is not None else position.entry_price


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
    #
    # The unit stays on entry_price -- the ASK, the price actually paid, which
    # is what this position can lose. Only the stop's STARTING POINT moves
    # onto the bid below; the size of the move it waits for does not.
    unit = risk_unit(position.entry_price)

    # WHERE THE STOP MEASURES FROM, AND WHY IT IS NOT entry_price.
    #
    # The stop is a movement filter (see the module docstring). current_price
    # is the BID -- position_manager marks an open position at what it could
    # be sold for -- while entry_price is the ASK, what the entry paid. Both
    # are individually right, and subtracting one from the other is not: it
    # charges the whole bid-ask spread against the stop's budget before the
    # market has moved at all.
    #
    # MEASURED over 511 closed positions with a book snapshot within 15 min of
    # entry (2026-08-01..09-01): entry_ask - bid_at_entry is positive on 86%,
    # median 0.020, max 0.080, against stop distances of 1.2c-13.8c. That is a
    # median 24% of the budget gone at entry, and 100% or more of it on 7% of
    # positions -- two of which stopped 0 seconds after entry with no market
    # movement whatsoever (WMKK 2026-08-31 b34 YES @0.15, spread 0.050 against
    # a distance of 0.045; RKSI 2026-08-25 b29 YES @0.24, 0.080 against 0.072).
    # Of the 207 fires scoreable against settlement, 117 (57%) do not fire on
    # this basis, 46 of those were eventual WINNERS, and together they carry
    # -$401.74 across 13 of 17 stations.
    #
    # NOT MIRRORED ONTO THE TAKE-PROFIT, deliberately. That rule cashes a
    # REALIZABLE gain, and what a sale realizes really is current_bid minus
    # what was paid -- the spread there is a cost genuinely incurred, not a
    # measurement error. Moving it too would make it fire EARLIER by the
    # spread, on the rule already measured as the expensive one.
    #
    # None means the entry-side book was never recorded (rows predating
    # Position.entry_bid, and manual_trigger). Those keep the basis they were
    # opened under rather than silently gaining a spread of extra room.
    stop_from = stop_basis_price(position)

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
    # Expensive entries skipped the stop too, for the opposite reason: not
    # that its distance is too small to mean anything, but that it is
    # large enough to be triggered by an ordinary intraday swing on a day
    # whose maximum has not been set yet. Up there the stop fires on
    # eventual WINNERS more often than not -- 45% precision post-deploy.
    #
    # DORMANT since 2026-08-27: the constant is 1.01, above MAX_ENTRY_PRICE,
    # so this evaluates False for every entry that can exist. It stays here
    # rather than being deleted because the precision finding replicated and
    # only the EXPOSURE half is missing; config.py above the constant has
    # both measurements and the condition for turning it back on.
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
        and (stop_from - current_price) >= thresholds["stop_loss_pct"] * unit
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
