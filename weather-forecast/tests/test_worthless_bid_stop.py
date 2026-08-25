"""
tests/test_worthless_bid_stop.py

The stop-loss carve-out keyed on the CURRENT price (config.MIN_EXIT_PRICE),
added 2026-08-24.

risk_manager.evaluate_exit() already skipped the percentage stop in two
ENTRY-price bands (LOTTERY_PRICE_THRESHOLD below, STOP_EXEMPT_ABOVE_PRICE
above). This third one is different in kind: it keys on where the price is
NOW, because selling into a bid of ~0 is weakly dominated by holding -- both
pay 0 if the bucket loses, and only holding pays if it wins.

HOW A ZERO BID REACHED THE STOP AT ALL. position_manager routes any extreme
price through a confirming re-fetch and a Gamma "is this market closed?"
lookup. When Gamma answers CLOSED the position is booked as a resolution;
when it answers OPEN the code deliberately falls through to normal exit
evaluation ("a genuine collapse still needs its loss cut"). That last branch
is how ZBAA 2026-08-24 bucket 32 was sold at a gross bid of 0.0000 for -100%,
and it is the branch this carve-out changes.

These tests pin the boundary (at MIN_EXIT_PRICE exempt, one tick above not),
that the upper extreme is deliberately NOT mirrored, and that a normal stop
is untouched -- a carve-out off by one tick is a different policy.
"""

from contextlib import contextmanager
from datetime import date

import config
import risk_manager
from models import Position

LOOSE_HOUR = 6


@contextmanager
def _with_min_exit_price(price: float):
    old = config.MIN_EXIT_PRICE
    config.MIN_EXIT_PRICE = price
    try:
        yield
    finally:
        config.MIN_EXIT_PRICE = old


def _position(entry_price: float, side: str = "YES") -> Position:
    """Entry sits inside the band the stop actually covers, so any exemption
    seen in these tests is the one under test and not one of the other two."""
    assert config.LOTTERY_PRICE_THRESHOLD <= entry_price < config.STOP_EXEMPT_ABOVE_PRICE
    return Position(
        position_id=f"ZBAA:2026-08-24:32:{side}:{entry_price}",
        station_icao="ZBAA", target_date=date(2026, 8, 24), bucket_c=32,
        side=side, entry_price=entry_price, size_usd=5.37,
        entry_time="2026-08-24T01:00:00+00:00", status="open",
        high_water_mark=entry_price,
    )


def test_a_zero_bid_is_not_stopped_out():
    """THE CASE THIS EXISTS FOR. ZBAA 2026-08-24 bucket 32: entered 0.40,
    sold at a gross bid of 0.0000 for -100%, which is exactly what settlement
    paid anyway -- the stop changed nothing, while booking a misleading exit
    reason and tripping the re-entry cooldown."""
    decision = risk_manager.evaluate_exit(_position(0.40), 0.0, local_hour=LOOSE_HOUR)
    assert not decision.should_exit
    assert decision.reason == "hold"


def test_at_min_exit_price_is_exempt_and_one_tick_above_is_not():
    """The boundary, both sides. At 0.03 the sale raises ~nothing and is
    skipped; at 0.04 the ordinary stop still governs."""
    with _with_min_exit_price(0.03):
        at_bound = risk_manager.evaluate_exit(_position(0.40), 0.03, local_hour=LOOSE_HOUR)
        above = risk_manager.evaluate_exit(_position(0.40), 0.04, local_hour=LOOSE_HOUR)

    assert not at_bound.should_exit, "at MIN_EXIT_PRICE the stop must not fire"
    assert at_bound.reason == "hold"
    assert above.should_exit, "one tick above MIN_EXIT_PRICE the stop still applies"
    assert above.reason == "stop_loss"


def test_a_normal_stop_is_untouched():
    """The carve-out must not widen into the band the stop is meant to cover.
    0.40 entry, 0.30 x 0.40 = 0.12 distance, so 0.28 triggers."""
    decision = risk_manager.evaluate_exit(_position(0.40), 0.28, local_hour=LOOSE_HOUR)
    assert decision.should_exit
    assert decision.reason == "stop_loss"


def test_the_top_of_the_book_is_deliberately_not_mirrored():
    """Selling at >= 1 - MIN_EXIT_PRICE hands over nearly the full payout with
    certainty -- a genuine trade-off, not a dominated one -- so the profit-take
    must still fire up there rather than being exempted by symmetry."""
    decision = risk_manager.evaluate_exit(_position(0.40), 0.99, local_hour=LOOSE_HOUR)
    assert decision.should_exit
    assert decision.reason == "take_profit"


def test_the_exemption_does_not_depend_on_the_tightening_hour():
    """The other thresholds move at EDGE_DECAY_TIGHTEN_HOUR_LOCAL. This one is
    a statement about the price, not the hour, so it holds at both."""
    for hour in (LOOSE_HOUR, 14):
        decision = risk_manager.evaluate_exit(_position(0.40), 0.0, local_hour=hour)
        assert not decision.should_exit, f"a zero bid must never stop out (hour {hour})"
