"""
tests/test_hold_to_settlement_modes.py

config.HOLD_TO_SETTLEMENT_MODES: the execution modes whose positions ignore
BOTH price-based exits and are closed only by resolution.

WHY IT EXISTS. Measured 2026-09-02 over 514 closed positions carrying a
recorded settlement (2026-08-03..09-01, $4,049.93 staked): held to
settlement the same entries return +$743.68 (+18.4%, day-clustered
bootstrap 95% CI [+5.2%, +31.5%]); as actually traded they returned
-$295.15 (-7.3%, CI [-13.1%, -1.8%]). The two rules cost $1,038.82 between
them, and each is individually negative -- stop only +4.6%, take only
+7.0%, neither +18.9%. 36% of the 222 stop fires would have won at
settlement; the take sells at a mean 0.468 against a 0.543 settlement win
rate on the same rows.

THE SPLIT THIS MODULE PINS IS THE WHOLE POINT. The live book is armed with
real money (WSSS, RCSS) and CANNOT hold to settlement today: there is no
redemption code, so a held winner strands the book the way the dust halts
already did. So the mechanism is keyed on execution_mode and the default
arms it for "paper" ALONE. Two separate halves, as in
test_stop_exempt_high.py:

  - THE MECHANISM, exercised against an explicitly-set mode tuple.
  - THE SHIPPED DEFAULT, pinned separately -- both that paper IS in it and
    that live is NOT. A config edit that silently disarmed the stop on the
    real-money book is the failure worth a test.

Never assert mechanism behaviour against the live constant.
"""

from contextlib import contextmanager
from datetime import date

import config
import risk_manager
from models import Position

# Evaluated before config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL, so the loose
# thresholds apply -- the same convention as the other exit tests.
LOOSE_HOUR = 6


@contextmanager
def _with_modes(modes):
    old = config.HOLD_TO_SETTLEMENT_MODES
    config.HOLD_TO_SETTLEMENT_MODES = modes
    try:
        yield
    finally:
        config.HOLD_TO_SETTLEMENT_MODES = old


def _position(execution_mode: str, entry_price: float = 0.40) -> Position:
    """
    An ordinary mid-band entry: above LOTTERY_PRICE_THRESHOLD and below
    STOP_EXEMPT_ABOVE_PRICE, so no pre-existing carve-out can answer in
    place of the one under test.
    """
    return Position(
        position_id=f"WMKK:2026-08-12:33:YES:{execution_mode}",
        station_icao="WMKK", target_date=date(2026, 8, 12), bucket_c=33,
        side="YES", entry_price=entry_price, size_usd=6.05,
        entry_time="2026-08-12T00:37:00+00:00", status="open",
        high_water_mark=entry_price, execution_mode=execution_mode,
        # THE SECOND HALF OF THE CONDITION. is_paper is what executor.py sets
        # for every non-live mode, and evaluate_exit requires it as well as
        # the mode -- see the migration-default reasoning there. Every real
        # paper row carries it (509 of 509 on 2026-09-02).
        is_paper=(execution_mode != "live"),
    )


def _stop_price(entry_price: float) -> float:
    """A quote comfortably past the stop trigger, on the 1-cent grid."""
    unit = risk_manager.risk_unit(entry_price)
    return round(entry_price - (config.STOP_LOSS_PCT * unit) - 0.02, 2)


def _take_price(entry_price: float) -> float:
    """A quote comfortably past the take trigger, on the 1-cent grid."""
    unit = risk_manager.risk_unit(entry_price)
    return round(entry_price + (config.PROFIT_TAKE_PCT * unit) + 0.02, 2)


# --- the mechanism -------------------------------------------------------

def test_disarmed_mode_holds_through_the_stop():
    with _with_modes(("paper",)):
        decision = risk_manager.evaluate_exit(
            _position("paper"), _stop_price(0.40), local_hour=LOOSE_HOUR,
        )
    assert decision.should_exit is False
    assert decision.reason == "hold"


def test_disarmed_mode_holds_through_the_take():
    with _with_modes(("paper",)):
        decision = risk_manager.evaluate_exit(
            _position("paper"), _take_price(0.40), local_hour=LOOSE_HOUR,
        )
    assert decision.should_exit is False
    assert decision.reason == "hold"


def test_mode_outside_the_set_still_stops():
    with _with_modes(("paper",)):
        decision = risk_manager.evaluate_exit(
            _position("live"), _stop_price(0.40), local_hour=LOOSE_HOUR,
        )
    assert decision.should_exit is True
    assert decision.reason == "stop_loss"


def test_mode_outside_the_set_still_takes():
    with _with_modes(("paper",)):
        decision = risk_manager.evaluate_exit(
            _position("live"), _take_price(0.40), local_hour=LOOSE_HOUR,
        )
    assert decision.should_exit is True
    assert decision.reason == "take_profit"


def test_empty_set_restores_the_old_behaviour_everywhere():
    """The revert is a one-value change, so an empty tuple must arm both
    rules for every mode -- including paper."""
    with _with_modes(()):
        stopped = risk_manager.evaluate_exit(
            _position("paper"), _stop_price(0.40), local_hour=LOOSE_HOUR,
        )
        took = risk_manager.evaluate_exit(
            _position("paper"), _take_price(0.40), local_hour=LOOSE_HOUR,
        )
    assert stopped.reason == "stop_loss"
    assert took.reason == "take_profit"


# --- the shipped default -------------------------------------------------

def test_shipped_default_disarms_paper_only():
    assert "paper" in config.HOLD_TO_SETTLEMENT_MODES
    assert "live" not in config.HOLD_TO_SETTLEMENT_MODES, (
        "The live book has no redemption code -- a position held to "
        "settlement there strands the book. See live-halt dust findings."
    )
    assert "simulation" not in config.HOLD_TO_SETTLEMENT_MODES, (
        "simulation exists to rehearse live exactly; disarming its exits "
        "would make the rehearsal model a book that does not exist."
    )


def test_shipped_default_holds_a_real_paper_position():
    """Belt and braces: the constant is right AND the wiring reaches it."""
    decision = risk_manager.evaluate_exit(
        _position("paper"), _stop_price(0.40), local_hour=LOOSE_HOUR,
    )
    assert decision.should_exit is False


# --- the is_paper guard --------------------------------------------------

def test_a_real_money_row_mislabelled_as_paper_keeps_its_exits():
    """
    execution_mode was added by migration with DEFAULT 'paper', so a row
    written before it existed reads "paper" whatever it really was. None
    exist today; if one ever appears, it must not lose its stop.
    """
    position = _position("paper")
    position.is_paper = False          # real money, mode says otherwise
    with _with_modes(("paper",)):
        decision = risk_manager.evaluate_exit(
            position, _stop_price(0.40), local_hour=LOOSE_HOUR,
        )
    assert decision.should_exit is True
    assert decision.reason == "stop_loss"


def test_a_replay_position_is_disarmed_like_the_paper_book_it_models():
    """
    backtest/portfolio.py forces is_paper=True and leaves execution_mode on
    the Position default, so a replay models the paper book -- including
    this carve-out. If it did not, every sweep would keep scoring exits the
    paper book no longer has.
    """
    replayed = Position(
        position_id="WMKK:2026-08-12:33:YES:replay",
        station_icao="WMKK", target_date=date(2026, 8, 12), bucket_c=33,
        side="YES", entry_price=0.40, size_usd=6.05,
        entry_time="2026-08-12T00:37:00+00:00", status="open",
        high_water_mark=0.40, is_paper=True,
    )
    with _with_modes(("paper",)):
        decision = risk_manager.evaluate_exit(
            replayed, _stop_price(0.40), local_hour=LOOSE_HOUR,
        )
    assert decision.should_exit is False
