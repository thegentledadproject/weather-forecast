"""
tests/test_stop_exempt_high.py

The stop-loss carve-out's UPPER half (config.STOP_EXEMPT_ABOVE_PRICE).

risk_manager.evaluate_exit() has always exempted sub-LOTTERY_PRICE_THRESHOLD
entries from the percentage stop. As of 2026-08-20 it also exempts entries at
or above STOP_EXEMPT_ABOVE_PRICE, on a different and directly measured
argument: scored against settlement on the paper book, the stop's precision
falls monotonically with entry price -- 83% below 0.30 against 33% at or
above 0.45 at WMKK -- so up there it fires on eventual winners two times in
three. config.py carries the full measurement.

Unlike the lottery take-profit knob, THIS ONE IS NOT A NO-OP: it changes live
exits for every entry at or above 0.45, which on the WMKK book is nine of
eighteen stops. These tests pin what changed, what deliberately did not, and
the boundary -- 0.45 exempt, 0.44 not -- because a carve-out that is off by
one tick is a different policy from the one that was measured.
"""

from contextlib import contextmanager
import math
from datetime import date

import config
import risk_manager
from models import Position

# Evaluated before config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL unless a test says
# otherwise, so the loose thresholds apply and the numbers below are the
# ones in config's comments rather than their tightened halves.
LOOSE_HOUR = 6
TIGHT_HOUR = 14


@contextmanager
def _with_stop_exempt_above(price: float):
    """Set the upper carve-out boundary for one block."""
    old = config.STOP_EXEMPT_ABOVE_PRICE
    config.STOP_EXEMPT_ABOVE_PRICE = price
    try:
        yield
    finally:
        config.STOP_EXEMPT_ABOVE_PRICE = old


def _position(entry_price: float, side: str = "NO") -> Position:
    return Position(
        position_id=f"WMKK:2026-08-12:33:{side}:{entry_price}",
        station_icao="WMKK", target_date=date(2026, 8, 12), bucket_c=33,
        side=side, entry_price=entry_price, size_usd=6.05,
        entry_time="2026-08-12T00:37:00+00:00", status="open",
        high_water_mark=entry_price,
    )


def _stopped_out_price(entry_price: float, pct: float = None) -> float:
    """
    A real quote at which the stop would fire, were it armed: the trigger
    distance below entry, snapped DOWN to Polymarket's 1-cent grid.

    Snapping matters twice. The grid is what a book can actually print, so
    a test asserting "this stops" should assert it about a price that can
    exist. And it keeps the assertion off the float boundary -- entry minus
    pct * unit lands a fraction of an ulp on either side of the threshold
    depending on the entry price, which makes an exact-trigger test pass or
    fail for reasons that have nothing to do with the carve-out.
    """
    pct = config.STOP_LOSS_PCT if pct is None else pct
    exact = entry_price - pct * risk_manager.risk_unit(entry_price)
    return math.floor(exact * 100) / 100


# --- the change itself ----------------------------------------------------

def test_expensive_entry_does_not_stop():
    """
    WMKK 2026-08-12, NO on bucket 33 entered at 0.57 -- one of the nine
    stops the measurement is about. Its risk unit is 0.43, so the stop sat
    at 0.441 and it was cut at 0.43. Bucket 33 did not settle: held, it
    was worth par.
    """
    pos = _position(0.57)
    decision = risk_manager.evaluate_exit(pos, 0.43, local_hour=LOOSE_HOUR)
    assert decision.should_exit is False
    assert decision.reason == "hold"


def test_expensive_entry_does_not_stop_however_far_it_falls():
    """
    The exemption is not a wider stop, it is no stop. A position at 0.55
    quoted at a penny still holds -- the only downside close left for it is
    resolution detection in position_manager.
    """
    decision = risk_manager.evaluate_exit(_position(0.55), 0.01, local_hour=LOOSE_HOUR)
    assert decision.should_exit is False


def test_expensive_entry_still_takes_profit():
    """
    Only the stop is exempted. The take-profit is untouched, and remains
    the one exit that applies at every entry price.
    """
    pos = _position(0.55)
    unit = risk_manager.risk_unit(0.55)          # 0.45
    target = 0.55 + config.PROFIT_TAKE_PCT * unit  # 0.775
    decision = risk_manager.evaluate_exit(pos, target, local_hour=LOOSE_HOUR)
    assert decision.should_exit is True
    assert decision.reason == "take_profit"


def test_exemption_holds_after_the_edge_decay_hour():
    """
    The tightening moves threshold DISTANCES; it does not re-arm an exit
    that is switched off. A 0.57 entry is exempt at 14:00 as at 06:00.
    """
    pos = _position(0.57)
    tight_trigger = _stopped_out_price(0.57, config.TIGHTENED_STOP_LOSS_PCT)
    decision = risk_manager.evaluate_exit(pos, tight_trigger, local_hour=TIGHT_HOUR)
    assert decision.should_exit is False


# --- what deliberately did NOT change -------------------------------------

def test_midband_entry_still_stops():
    """
    0.44 is below the boundary, so the stop is unchanged there. This is
    the band where the measurement says the stop is worth keeping (62%
    precise at 0.30-0.45), and the whole point of siting a boundary is
    that one side of it keeps its old behaviour.
    """
    pos = _position(0.44)
    decision = risk_manager.evaluate_exit(pos, _stopped_out_price(0.44), local_hour=LOOSE_HOUR)
    assert decision.should_exit is True
    assert decision.reason == "stop_loss"


def test_cheap_entry_still_stops():
    """The 0.15-0.30 band, where the stop measured 78% precise."""
    pos = _position(0.24, side="YES")
    decision = risk_manager.evaluate_exit(pos, _stopped_out_price(0.24), local_hour=LOOSE_HOUR)
    assert decision.should_exit is True
    assert decision.reason == "stop_loss"


def test_lottery_exemption_intact():
    """The lower carve-out is untouched by the upper one."""
    pos = _position(0.08, side="YES")
    decision = risk_manager.evaluate_exit(pos, 0.01, local_hour=LOOSE_HOUR)
    assert decision.should_exit is False


# --- the boundary ---------------------------------------------------------

def test_boundary_is_inclusive_at_the_threshold():
    """
    `>=`, not `>`. An entry exactly at STOP_EXEMPT_ABOVE_PRICE is exempt.
    WMKK 2026-08-07 entered a NO at exactly 0.45 and was stopped for
    -13.94 on a position that settled a winner, so this tick is not
    hypothetical.
    """
    at = config.STOP_EXEMPT_ABOVE_PRICE
    assert risk_manager.evaluate_exit(
        _position(at), _stopped_out_price(at), local_hour=LOOSE_HOUR
    ).should_exit is False
    # ...and one cent below it is not exempt, which is what makes the
    # assertion above about the boundary rather than about the price.
    just_under = round(at - 0.01, 2)
    assert risk_manager.evaluate_exit(
        _position(just_under), _stopped_out_price(just_under), local_hour=LOOSE_HOUR
    ).reason == "stop_loss"


def test_boundary_is_read_at_call_time_not_import_time():
    """
    backtest/stop_sweep.py rebinds config constants between runs, so this
    one has to be read per evaluation. It is NOT written as an alias of
    another constant -- see THE ALIAS GOTCHA in stop_sweep.py -- but a
    module-level capture in risk_manager would break sweeping it just the
    same.
    """
    pos = _position(0.57)
    trigger = _stopped_out_price(0.57)
    with _with_stop_exempt_above(0.60):
        armed = risk_manager.evaluate_exit(pos, trigger, local_hour=LOOSE_HOUR)
    assert armed.should_exit is True
    assert armed.reason == "stop_loss"


def test_carve_out_can_be_disabled_entirely():
    """
    1.01 restores the pre-2026-08-20 behaviour, since MAX_ENTRY_PRICE caps
    entries at 0.75. That is the documented off switch and it needs to work
    -- it is how this change gets reverted without a deploy.
    """
    with _with_stop_exempt_above(1.01):
        for entry in (0.45, 0.57, 0.71, 0.75):
            decision = risk_manager.evaluate_exit(
                _position(entry), _stopped_out_price(entry), local_hour=LOOSE_HOUR
            )
            assert decision.should_exit is True, entry
            assert decision.reason == "stop_loss", entry
