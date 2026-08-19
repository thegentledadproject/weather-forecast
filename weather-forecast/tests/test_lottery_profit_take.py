"""
tests/test_lottery_profit_take.py

The lottery carve-out's SECOND half.

risk_manager.evaluate_exit() has always exempted sub-LOTTERY_PRICE_THRESHOLD
entries from the percentage stop -- the threshold distance there is under
Polymarket's 1-cent tick, so any book wobble triggers it. The profit-take
had no such exemption and no knob of its own: a 0.08 entry took profit at
0.12, a 4-cent move on a 1-cent tick, which is the same sub-tick argument
that disqualified the stop.

config.LOTTERY_PROFIT_TAKE_PCT makes that distance settable independently.
It DEFAULTS TO PROFIT_TAKE_PCT, so shipping it changes nothing; these tests
pin both halves of that -- the no-op at the default, and the isolation
(moving it must not touch a normal-priced position) that backtest/
take_sweep.py depends on to attribute a difference to the lottery cohort.
"""

from contextlib import contextmanager
from datetime import date

import config
import risk_manager
from models import Position


@contextmanager
def _with_lottery_take(pct: float, tightened: float = None):
    """
    Set the lottery take distance (and its tightened partner) for one block.

    Both are always set together, for the reason backtest/stop_sweep.py
    documents as THE ALIAS GOTCHA: the tightened constant is written as a
    reference to its loose sibling, which binds the VALUE at import, so
    rebinding one alone silently leaves the other at the old number.
    """
    old_loose = config.LOTTERY_PROFIT_TAKE_PCT
    old_tight = config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT
    config.LOTTERY_PROFIT_TAKE_PCT = pct
    config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT = pct if tightened is None else tightened
    try:
        yield
    finally:
        config.LOTTERY_PROFIT_TAKE_PCT = old_loose
        config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT = old_tight


def _position(entry_price: float) -> Position:
    return Position(
        position_id=f"WSSS:2026-08-19:33:YES:{entry_price}",
        station_icao="WSSS", target_date=date(2026, 8, 19), bucket_c=33,
        side="YES", entry_price=entry_price, size_usd=1.0,
        entry_time="2026-08-18T21:14:20+00:00", status="open",
        high_water_mark=entry_price,
    )


# The live pair, evaluated before the edge-decay hour so the loose
# thresholds apply. 0.08 is the 2026-08-19 WSSS 33-YES entry.
LOTTERY_ENTRY = 0.08
NORMAL_ENTRY = 0.42
EARLY_HOUR = 6


def test_the_default_leaves_lottery_take_profit_exactly_where_it_was():
    """
    The knob ships as a no-op: at the default, EVERY price on both sides of
    the boundary and at both threshold regimes must produce exactly the
    decision PROFIT_TAKE_PCT alone would have produced.

    Asserted as an equality against the unbranched rule rather than by
    naming the trigger price. entry + 0.50 x 0.08 is 0.12 in decimal but
    0.12 - 0.08 is 0.0399999999999999 in binary, so a test that pins the
    exact boundary price pins float dust instead of behaviour -- and the
    dust is pre-existing, unrelated to this branch, and worth at most one
    tick of delay on a 1-cent book.
    """
    assert config.LOTTERY_PROFIT_TAKE_PCT == config.PROFIT_TAKE_PCT
    assert config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT == config.TIGHTENED_PROFIT_TAKE_PCT

    pos = _position(LOTTERY_ENTRY)
    unit = risk_manager.risk_unit(LOTTERY_ENTRY)

    for hour, pct in ((EARLY_HOUR, config.PROFIT_TAKE_PCT),
                      (config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL, config.TIGHTENED_PROFIT_TAKE_PCT)):
        for price in (0.05, 0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.20, 0.50):
            got = risk_manager.evaluate_exit(pos, price, local_hour=hour)
            unbranched = (price - LOTTERY_ENTRY) >= pct * unit
            assert got.should_exit == unbranched, (
                f"price {price} at hour {hour}: branch says {got.should_exit}, "
                f"the unbranched rule says {unbranched}"
            )


def test_a_wider_lottery_take_holds_the_ticket_the_live_setting_would_have_sold():
    """
    THE 2026-08-19 CASE. The WSSS 33-YES ticket was ordered sold six times at
    0.12-0.13 and reached 0.92 only because the book was too thin to fill and
    the exchange halted. At a wider lottery distance the system would hold it
    by DESIGN rather than by luck.
    """
    pos = _position(LOTTERY_ENTRY)

    with_wide = _with_lottery_take(4.0)
    with with_wide:
        assert not risk_manager.evaluate_exit(pos, 0.13, local_hour=EARLY_HOUR).should_exit
        # 0.08 + 4.0 x 0.08 = 0.40
        assert risk_manager.evaluate_exit(pos, 0.40, local_hour=EARLY_HOUR).reason == "take_profit"


def test_moving_the_lottery_take_does_not_touch_a_normal_priced_position():
    """
    The isolation take_sweep.py rests on. If a normal entry moved too, a
    difference between sweep rows could not be attributed to the lottery
    cohort.
    """
    pos = _position(NORMAL_ENTRY)
    # 0.42 + 0.50 x 0.42 = 0.63 at the live setting.
    baseline = risk_manager.evaluate_exit(pos, 0.63, local_hour=EARLY_HOUR)
    assert baseline.reason == "take_profit"

    with _with_lottery_take(4.0):
        assert risk_manager.evaluate_exit(pos, 0.63, local_hour=EARLY_HOUR).reason == "take_profit"
        assert not risk_manager.evaluate_exit(pos, 0.62, local_hour=EARLY_HOUR).should_exit


def test_the_lottery_take_tightens_after_the_edge_decay_hour_like_its_sibling():
    """
    PROFIT_TAKE_PCT has a TIGHTENED_ partner applied after
    EDGE_DECAY_TIGHTEN_HOUR_LOCAL. The lottery distance needs the same, or a
    sweep row would silently revert to the untightened value for half the
    trading day.
    """
    pos = _position(LOTTERY_ENTRY)
    late = config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL

    with _with_lottery_take(4.0, tightened=1.0):
        # 0.08 + 1.0 x 0.08 = 0.16 after the hour, vs 0.40 before it.
        assert not risk_manager.evaluate_exit(pos, 0.16, local_hour=EARLY_HOUR).should_exit
        assert risk_manager.evaluate_exit(pos, 0.16, local_hour=late).reason == "take_profit"


def test_the_stop_exemption_is_unchanged():
    """
    Regression guard. The lottery carve-out's FIRST half must survive this:
    a lottery position still has no percentage stop at all, which is what
    let the 2026-08-19 ticket sit at 0.03 (-62.5%) without being cut.
    """
    pos = _position(LOTTERY_ENTRY)
    assert not risk_manager.evaluate_exit(pos, 0.03, local_hour=EARLY_HOUR).should_exit
