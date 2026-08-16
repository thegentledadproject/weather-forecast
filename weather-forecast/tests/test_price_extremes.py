"""
tests/test_price_extremes.py

Pins the 2026-08-09 changes that made the risk framework behave sanely at
BOTH ends of the price range, where percentage-of-entry thresholds break
down in opposite directions.

The failures being pinned, all of which were live:

  * Above entry 0.667 the fixed profit-take needed price >= 1.5 x entry,
    which no market can print. Above 0.80 the tightened take and both
    trailing activations died the same way. An entry at 0.85 had NO
    upside exit of any kind and a stop-loss 1.7x its own maximum gain.
  * A trailing-stop breach at the activation point booked a fixed +6.25%
    gross gain regardless of entry price -- less than round-trip taker
    fees, so the mechanism reliably closed winners into net losses and
    recorded them as wins.
  * Lottery tickets were exempt from the stop-loss but not the trailing
    stop, leaving the exact 1-cent-tick churn the exemption exists to
    prevent fully intact one mechanism over.
  * Kelly's (1 - price) denominator meant that at price 0.95 even the
    smallest legal edge pinned MAX_POSITION_USD, so sizing stopped
    responding to edge quality at all.
  * MAX_PLAUSIBLE_RAW_EDGE could not fire above price 0.75, because raw
    edge <= 1 - price by construction -- the data-error gate was blind in
    exactly the band where bad data looks most plausible.

The single most important property here is REGRESSION SAFETY: the risk
unit is min(entry, 1-entry), which IS entry price below 0.50, so none of
this may change any decision for an entry at or below 0.50. That is
what test_reformulation_is_a_no_op_at_or_below_0_50 checks, against the
old formulas written out longhand.
"""
from datetime import date

import pytest

import config
import entry_manager
import risk_manager
from models import Position


def _position(entry_price: float, high_water_mark: float = None) -> Position:
    return Position(
        position_id=f"WSSS:2026-08-09:31:YES:{entry_price}",
        station_icao="WSSS",
        target_date=date(2026, 8, 9),
        bucket_c=31,
        side="YES",
        entry_price=entry_price,
        size_usd=50.0,
        entry_time="2026-08-09T05:00:00",
        high_water_mark=high_water_mark,
    )


# Well inside the morning window, so the loose (untightened) thresholds
# are the ones under test unless a case says otherwise.
MORNING = 6
AFTERNOON = 14


# --------------------------------------------------------------------------
# Regression safety: nothing below 0.50 may move
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry_price", [0.04, 0.10, 0.15, 0.20, 0.30, 0.42, 0.50])
def test_reformulation_is_a_no_op_at_or_below_0_50(entry_price):
    """
    Below 0.50 the risk unit IS entry price, so every threshold must sit
    at exactly the price the old percentage-of-entry formula put it at.
    Checked against the old arithmetic spelled out here rather than
    against the new code, so this fails if the two ever diverge.
    """
    unit = risk_manager.risk_unit(entry_price)
    assert unit == pytest.approx(entry_price)

    old_stop_price = entry_price * (1 - config.STOP_LOSS_PCT)
    new_stop_price = entry_price - config.STOP_LOSS_PCT * unit
    assert new_stop_price == pytest.approx(old_stop_price)

    old_take_price = entry_price * (1 + config.PROFIT_TAKE_PCT)
    new_take_price = entry_price + config.PROFIT_TAKE_PCT * unit
    assert new_take_price == pytest.approx(old_take_price)



def test_stop_loss_still_fires_at_the_same_place_below_0_50():
    """The end-to-end version of the above, through evaluate_exit()."""
    position = _position(entry_price=0.30)  # stop distance 0.30 x 0.30 = 0.09

    held = risk_manager.evaluate_exit(position, 0.215, local_hour=MORNING)
    assert held.should_exit is False

    stopped = risk_manager.evaluate_exit(position, 0.205, local_hour=MORNING)
    assert stopped.should_exit is True
    assert stopped.reason == "stop_loss"


# --------------------------------------------------------------------------
# The headline fix: upside exits exist at every entry price
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry_price", [0.05, 0.20, 0.40, 0.50, 0.60, 0.70, 0.75])
def test_profit_take_is_reachable_at_every_entry_price(entry_price):
    """
    The take-profit target must be a price the market can actually print.
    On the old entry-price basis this failed for everything above 0.667
    (and above 0.80 for the tightened target), which is what left a 0.85
    entry with no upside exit at all.
    """
    for hour in (MORNING, AFTERNOON):
        thresholds = risk_manager._active_thresholds(local_hour=hour)
        take_price = entry_price + thresholds["profit_take_pct"] * risk_manager.risk_unit(entry_price)
        assert take_price <= 1.0, (
            f"entry {entry_price} at hour {hour}: take-profit needs price {take_price:.4f}, "
            f"which is outside the book"
        )

        # And it actually fires there, rather than merely being expressible.
        # Evaluated one tick above the target rather than exactly on it:
        # the target is computed by the same float arithmetic the check
        # uses, so an exact-boundary comparison is a coin flip on the last
        # bit. Real prices arrive on a 1-cent tick, well clear of that.
        position = _position(entry_price=entry_price, high_water_mark=entry_price)
        decision = risk_manager.evaluate_exit(position, take_price + 0.01, local_hour=hour)
        assert decision.should_exit is True
        # hwm == entry, so the trailing stop cannot be armed: this is the
        # fixed take firing, which is the branch under test.
        assert decision.reason == "take_profit"


@pytest.mark.parametrize("entry_price", [0.55, 0.65, 0.70, 0.75])
def test_stop_never_risks_more_than_the_remaining_upside(entry_price):
    """
    The stop distance must not exceed what the position can possibly win.
    On the old basis this inverted above entry 0.769 (0.87 tightened):
    at 0.85 you risked 30% to make at most 17.6%.
    """
    remaining_upside = 1.0 - entry_price
    for hour in (MORNING, AFTERNOON):
        thresholds = risk_manager._active_thresholds(local_hour=hour)
        stop_distance = thresholds["stop_loss_pct"] * risk_manager.risk_unit(entry_price)
        assert stop_distance < remaining_upside, (
            f"entry {entry_price} at hour {hour}: risking {stop_distance:.4f} to win at most "
            f"{remaining_upside:.4f}"
        )


# --------------------------------------------------------------------------
# Trailing stop: must bank an actual profit
# --------------------------------------------------------------------------

def test_no_exit_path_produces_a_trailing_stop():
    """
    The trailing stop was REMOVED on 2026-08-17: it produced zero exits
    across the cohort window in four profit-take configurations, and the
    instrumented replay showed why -- it armed on 7 of 580 eligible ticks
    and never once saw a give-back, because acting requires two
    evaluations and a run-up crosses the whole band inside one.

    Swept across the price range and the peak/current grid that used to
    drive it, because the failure this guards against is a reintroduction
    that only fires in a corner.
    """
    for entry_price in (0.04, 0.10, 0.20, 0.30, 0.50, 0.70):
        unit = risk_manager.risk_unit(entry_price)
        for peak_mult in (0.1, 0.25, 0.4, 0.6, 1.0, 2.0):
            peak = entry_price + peak_mult * unit
            for give_back_mult in (0.0, 0.08, 0.15, 0.3, 0.6):
                price = max(peak - give_back_mult * unit, 0.01)
                position = _position(entry_price=entry_price, high_water_mark=peak)
                for hour in (MORNING, 14):
                    decision = risk_manager.evaluate_exit(position, price, local_hour=hour)
                    assert decision.reason != "trailing_stop"
                    assert decision.reason != "trailing_active"


def test_lottery_ticket_holds_through_a_peak_and_pullback():
    """
    The 1-cent-tick churn case: a $0.05 ticket ticks to $0.07 and back.
    On the old rules that armed trailing and stopped it out flat, which
    is precisely the noise the lottery exemption exists to ignore.
    """
    entry_price = 0.05
    assert entry_price < config.LOTTERY_PRICE_THRESHOLD

    position = _position(entry_price=entry_price, high_water_mark=0.07)
    decision = risk_manager.evaluate_exit(position, 0.05, local_hour=MORNING)

    assert decision.should_exit is False
    assert decision.reason == "hold", "a lottery ticket must not even arm the trailing stop"


def test_lottery_ticket_keeps_its_profit_take():
    """The exemption is downside/noise only -- real upside still exits."""
    entry_price = 0.05
    position = _position(entry_price=entry_price, high_water_mark=entry_price)
    take_price = entry_price + config.PROFIT_TAKE_PCT * risk_manager.risk_unit(entry_price)

    decision = risk_manager.evaluate_exit(position, take_price, local_hour=MORNING)
    assert decision.should_exit is True
    assert decision.reason == "take_profit"


def test_lottery_ticket_is_still_exempt_from_the_stop_loss():
    """Unchanged behaviour, pinned so the trailing change didn't disturb it."""
    entry_price = 0.05
    position = _position(entry_price=entry_price, high_water_mark=entry_price)

    decision = risk_manager.evaluate_exit(position, 0.01, local_hour=MORNING)
    assert decision.should_exit is False


# --------------------------------------------------------------------------
# Degenerate entry prices must not collapse every threshold to zero
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry_price", [0.0, 1.0, 1.5, -0.2])
def test_risk_unit_is_floored_for_degenerate_entry_prices(entry_price):
    """
    entry_manager rejects these, but stored history could contain them,
    and a zero risk unit would put every threshold distance at zero --
    firing a stop-loss at any price at all.
    """
    assert risk_manager.risk_unit(entry_price) >= risk_manager.MIN_RISK_UNIT


# --------------------------------------------------------------------------
# Entry-side gates
# --------------------------------------------------------------------------

def test_max_entry_price_is_below_where_the_gates_go_blind():
    """
    0.75 is not arbitrary: above it MAX_PLAUSIBLE_RAW_EDGE can never fire
    (raw edge <= 1 - price), and the normal-regime stop would start
    exceeding total available upside.
    """
    assert config.MAX_ENTRY_PRICE <= 0.75

    ceiling_price = config.MAX_ENTRY_PRICE
    assert config.MAX_PLAUSIBLE_RAW_EDGE >= (1.0 - ceiling_price), (
        "MAX_ENTRY_PRICE has drifted above the point where the flat edge ceiling stops being reachable"
    )

    unit = risk_manager.risk_unit(ceiling_price)
    assert config.STOP_LOSS_PCT * unit < (1.0 - ceiling_price)


def test_plausibility_ceiling_is_continuous_at_0_50_and_binds_above_it():
    """
    The headroom term and the flat ceiling must meet at 0.50, so nothing
    below 0.50 changes, and the headroom term must take over above it.
    """
    assert entry_manager.max_plausible_edge_for(0.50) == pytest.approx(config.MAX_PLAUSIBLE_RAW_EDGE)

    for price in (0.05, 0.20, 0.35, 0.49):
        assert entry_manager.max_plausible_edge_for(price) == pytest.approx(config.MAX_PLAUSIBLE_RAW_EDGE)

    for price in (0.60, 0.75, 0.85, 0.95):
        ceiling = entry_manager.max_plausible_edge_for(price)
        assert ceiling < config.MAX_PLAUSIBLE_RAW_EDGE
        assert ceiling <= (1.0 - price), "ceiling must sit inside the edge that can physically exist"


def test_plausibility_ceiling_catches_the_high_price_data_error():
    """
    A spurious model_prob of 0.99 against a real 0.85 quote is an edge of
    0.14 -- under the old flat 0.25 ceiling, through every other gate,
    and sized at the cap. It must now be rejected.
    """
    assert 0.14 > entry_manager.max_plausible_edge_for(0.85)
    assert 0.14 < config.MAX_PLAUSIBLE_RAW_EDGE, "the old flat ceiling really did let this through"


def test_kelly_denominator_floor_keeps_sizing_responsive_to_edge():
    """
    Without the floor, price 0.95 turns the smallest legal edge into 0.60
    full-Kelly and every approved entry lands on MAX_POSITION_USD. With
    it, doubling the edge must still double the size.
    """
    class _EV:
        def __init__(self, price, edge):
            self.market_price = price
            self.raw_edge = edge

    small = entry_manager.compute_kelly_fraction(_EV(0.95, config.MIN_ABS_RAW_EDGE))
    double = entry_manager.compute_kelly_fraction(_EV(0.95, 2 * config.MIN_ABS_RAW_EDGE))

    assert double == pytest.approx(2 * small), "sizing must still respond to edge quality at high prices"
    assert small * config.KELLY_FRACTION * config.BANKROLL_USD < config.MAX_POSITION_USD, (
        "the smallest legal edge must not pin the per-trade cap"
    )


def test_kelly_floor_does_not_touch_normal_prices():
    """The floor binds only above price 0.80 -- below that, plain Kelly."""
    class _EV:
        def __init__(self, price, edge):
            self.market_price = price
            self.raw_edge = edge

    for price in (0.10, 0.30, 0.50, 0.70, 0.80):
        got = entry_manager.compute_kelly_fraction(_EV(price, 0.05))
        assert got == pytest.approx(0.05 / (1 - price))
