"""
tests/test_exit_sweeps_armed.py

THE DEFECT THESE PIN, and why it is worse than a crash.

config.HOLD_TO_SETTLEMENT_MODES = ("paper",) (commit 2c8db40, 2026-09-02)
makes risk_manager.evaluate_exit() return "hold" before it reads any
threshold, for any position that is is_paper AND carries an execution_mode
in that tuple.

backtest/engine.py builds every replay position with is_paper=True and never
sets execution_mode, so it takes the Position default -- which is "paper".
Both sweeps therefore replay a cohort that is EXEMPT from the very rule they
vary. Every distance returns "hold", every row of the printed table is
identical, and that reads as "this threshold does not matter" rather than as
"this tool measured nothing".

backtest/compare.py::_armed_exits() already solved this and says so in its
docstring; the two sweeps never got the same line. These tests are the guard
that stops the batteries coming back out.
"""

import config
import risk_manager
from models import Position
from backtest import stop_sweep, take_sweep


def _replay_position(entry_price: float = 0.40) -> Position:
    """
    Shaped exactly the way backtest/engine.py builds one: is_paper=True with
    execution_mode left at the Position default. If engine.py ever starts
    stamping execution_mode explicitly, this helper is the thing to update.
    """
    pos = Position(
        position_id="test-1",
        station_icao="WSSS",
        target_date="2026-09-07",
        bucket_c=32,
        side="YES",
        entry_price=entry_price,
        size_usd=10.0,
        entry_time="2026-09-07T00:00:00Z",
        high_water_mark=entry_price,
        is_paper=True,
    )
    assert pos.execution_mode in config.HOLD_TO_SETTLEMENT_MODES, (
        "precondition: this cohort must be one the shipped config disarms, "
        "otherwise these tests prove nothing"
    )
    return pos


# ---------------------------------------------------------------- stop_sweep

def test_stop_distance_arms_the_replay_cohort():
    """
    Inside _stop_distance() the stop must actually be able to fire on the
    cohort engine.py produces. Entry 0.40 -> risk unit 0.40, so a 30%
    distance triggers at 0.28.
    """
    pos = _replay_position(entry_price=0.40)
    with stop_sweep._stop_distance(0.30):
        decision = risk_manager.evaluate_exit(pos, 0.28, local_hour=7)
    assert decision.reason == "stop_loss"


def test_a_wider_stop_holds_what_a_tighter_stop_cuts():
    """
    The property the sweep exists to measure: two distances must give two
    answers. 30% of 0.40 triggers at 0.28; 50% triggers at 0.20, so 0.28
    is still a hold there.
    """
    pos = _replay_position(entry_price=0.40)
    with stop_sweep._stop_distance(0.30):
        tight = risk_manager.evaluate_exit(pos, 0.28, local_hour=7)
    with stop_sweep._stop_distance(0.50):
        wide = risk_manager.evaluate_exit(pos, 0.28, local_hour=7)
    assert (tight.reason, wide.reason) == ("stop_loss", "hold")


def test_stop_distance_restores_hold_to_settlement_modes():
    """
    Borrow the ladder, return the ladder. config is process-global and the
    daemon reads the same module, so a sweep that leaks this setting would
    silently disarm the live book's hold-to-settlement behaviour.
    """
    before = config.HOLD_TO_SETTLEMENT_MODES
    with stop_sweep._stop_distance(0.50):
        pass
    assert config.HOLD_TO_SETTLEMENT_MODES == before


def test_stop_distance_restores_even_when_the_body_raises():
    before = config.HOLD_TO_SETTLEMENT_MODES
    try:
        with stop_sweep._stop_distance(0.50):
            raise RuntimeError("one bad station must not leak the setting")
    except RuntimeError:
        pass
    assert config.HOLD_TO_SETTLEMENT_MODES == before


# ---------------------------------------------------------------- take_sweep

def test_lottery_take_arms_the_replay_cohort():
    """
    The take-profit half of the same defect. A lottery entry at 0.10 with the
    take at 50% of the risk unit triggers at 0.15.

    Priced at 0.16 rather than exactly 0.15 on purpose: 0.15 - 0.10 is
    0.0499999... in binary float, just under the 0.05 the rule compares
    against, so an exact-boundary price tests the float representation
    instead of the arming this file is about.
    """
    pos = _replay_position(entry_price=0.10)
    assert pos.entry_price < config.LOTTERY_PRICE_THRESHOLD
    with take_sweep._lottery_take(0.50):
        decision = risk_manager.evaluate_exit(pos, 0.16, local_hour=7)
    assert decision.reason == "take_profit"


def test_lottery_take_restores_hold_to_settlement_modes():
    before = config.HOLD_TO_SETTLEMENT_MODES
    with take_sweep._lottery_take(0.50):
        pass
    assert config.HOLD_TO_SETTLEMENT_MODES == before
