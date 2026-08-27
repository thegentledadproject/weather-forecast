"""
tests/test_stop_exempt_high.py

The stop-loss carve-out's UPPER half (config.STOP_EXEMPT_ABOVE_PRICE), which
is currently DORMANT.

It ran 2026-08-20 to 08-27: entries at or above 0.45 skipped the percentage
stop, on stop precision measured against settlement -- 33% at or above 0.45
versus 83% below 0.30 at WMKK, and the same monotone fall across all stations.
Post-deploy the precision finding REPLICATED (45% against 68-77%) but the P&L
did not follow: restoring the stop over 21 exempt positions and $255.95 of
stake was worth +$0.44. It was reverted on variance -- the band's worst loss
was -$16.76 without the stop against -$4.80 with it, and Kelly sizes up exactly
where the carve-out switched protection off. config.py carries both numbers.

So these tests are in two halves, and the split is the point:

  - THE MECHANISM, exercised at an explicitly-set threshold. It has to keep
    working, because re-enabling is meant to be a one-value change and the
    exposure work that would justify that is still open.
  - THE SHIPPED DEFAULT, pinned separately as OFF. A revert that silently
    stopped being a revert is exactly the failure worth a test.

Never assert mechanism behaviour against the live constant: that is what tied
this module to the shipped value the first time and broke it on the revert.
"""

import math
from contextlib import contextmanager
from datetime import date

import config
import risk_manager
from models import Position

# The value the carve-out shipped with, used explicitly by every mechanism
# test below rather than read from config.
ARMED = 0.45

# Evaluated before config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL unless a test says
# otherwise, so the loose thresholds apply.
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

    Snapping matters twice. The grid is what a book can actually print, so a
    test asserting "this stops" should assert it about a price that can exist.
    And it keeps the assertion off the float boundary -- entry minus
    pct * unit lands a fraction of an ulp either side of the threshold
    depending on the entry price, which makes an exact-trigger test pass or
    fail for reasons that have nothing to do with the carve-out.

    Callers must also keep the result above config.MIN_EXIT_PRICE, or the
    2026-08-24 worthless-bid carve-out answers instead of this one.
    """
    pct = config.STOP_LOSS_PCT if pct is None else pct
    exact = entry_price - pct * risk_manager.risk_unit(entry_price)
    return math.floor(exact * 100) / 100


# --- the shipped default: OFF ---------------------------------------------

def test_shipped_default_leaves_the_carve_out_off():
    """
    1.01 is above MAX_ENTRY_PRICE (0.75), so no entry that can exist reaches
    it. This is the revert, pinned: if someone sets the constant back to a
    reachable value without meaning to, this fails.
    """
    assert config.STOP_EXEMPT_ABOVE_PRICE > config.MAX_ENTRY_PRICE


def test_expensive_entries_stop_normally_on_the_shipped_config():
    """The behaviour the revert restored, read through the live constant."""
    for entry in (0.45, 0.57, 0.71, 0.75):
        decision = risk_manager.evaluate_exit(
            _position(entry), _stopped_out_price(entry), local_hour=LOOSE_HOUR
        )
        assert decision.should_exit is True, entry
        assert decision.reason == "stop_loss", entry


# --- the mechanism, at an explicitly-set threshold -------------------------

def test_expensive_entry_does_not_stop_when_armed():
    """
    WMKK 2026-08-12, NO on bucket 33 entered at 0.57 -- one of the nine stops
    the original measurement was about. Risk unit 0.43, so the stop sat at
    0.441 and it was cut at 0.43. Bucket 33 did not settle: held, it was
    worth par.
    """
    with _with_stop_exempt_above(ARMED):
        decision = risk_manager.evaluate_exit(_position(0.57), 0.43, local_hour=LOOSE_HOUR)
    assert decision.should_exit is False
    assert decision.reason == "hold"


def test_exemption_is_no_stop_not_a_wider_one():
    """
    0.10 is chosen to sit well below the stop trigger for a 0.55 entry and
    still ABOVE config.MIN_EXIT_PRICE, so this exercises the entry-price
    carve-out rather than the worthless-bid one.
    """
    assert 0.10 > config.MIN_EXIT_PRICE
    with _with_stop_exempt_above(ARMED):
        decision = risk_manager.evaluate_exit(_position(0.55), 0.10, local_hour=LOOSE_HOUR)
    assert decision.should_exit is False


def test_exemption_survives_the_edge_decay_hour():
    """
    The tightening moves threshold DISTANCES; it does not re-arm an exit that
    is switched off. A 0.57 entry is exempt at 14:00 as at 06:00.
    """
    trigger = _stopped_out_price(0.57, config.TIGHTENED_STOP_LOSS_PCT)
    with _with_stop_exempt_above(ARMED):
        decision = risk_manager.evaluate_exit(_position(0.57), trigger, local_hour=TIGHT_HOUR)
    assert decision.should_exit is False


def test_take_profit_is_untouched_by_the_exemption():
    """Only the stop is exempted; the take-profit applies at every price."""
    target = 0.55 + config.PROFIT_TAKE_PCT * risk_manager.risk_unit(0.55)
    with _with_stop_exempt_above(ARMED):
        decision = risk_manager.evaluate_exit(_position(0.55), target, local_hour=LOOSE_HOUR)
    assert decision.should_exit is True
    assert decision.reason == "take_profit"


def test_boundary_is_inclusive_and_one_cent_below_is_not():
    """
    `>=`, not `>`. WMKK 2026-08-07 entered a NO at exactly 0.45 and was
    stopped for -13.94 on a position that settled a winner, so this tick is
    not hypothetical.
    """
    with _with_stop_exempt_above(ARMED):
        assert risk_manager.evaluate_exit(
            _position(ARMED), _stopped_out_price(ARMED), local_hour=LOOSE_HOUR
        ).should_exit is False
        under = round(ARMED - 0.01, 2)
        assert risk_manager.evaluate_exit(
            _position(under), _stopped_out_price(under), local_hour=LOOSE_HOUR
        ).reason == "stop_loss"


def test_threshold_is_read_at_call_time_not_import_time():
    """
    backtest/stop_sweep.py rebinds config constants between runs, so this one
    has to be read per evaluation -- and the revert itself depends on it, since
    the code path stays in place and only the value changes.
    """
    trigger = _stopped_out_price(0.57)
    with _with_stop_exempt_above(0.60):
        armed = risk_manager.evaluate_exit(_position(0.57), trigger, local_hour=LOOSE_HOUR)
    assert armed.reason == "stop_loss"


# --- what the carve-out never touched -------------------------------------

def test_lottery_exemption_intact():
    with _with_stop_exempt_above(ARMED):
        decision = risk_manager.evaluate_exit(_position(0.08, side="YES"), 0.05,
                                              local_hour=LOOSE_HOUR)
    assert decision.should_exit is False


def test_cheap_entry_still_stops():
    """The 0.15-0.30 band, which measured 77% precise post-deploy."""
    with _with_stop_exempt_above(ARMED):
        decision = risk_manager.evaluate_exit(_position(0.24, side="YES"),
                                              _stopped_out_price(0.24), local_hour=LOOSE_HOUR)
    assert decision.reason == "stop_loss"
