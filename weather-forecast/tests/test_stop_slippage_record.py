"""
tests/test_stop_slippage_record.py

P1-10 · record what a stop actually COST, not what it was set to.

THE DEFECT. A closed row records where the position sold. Nothing records where
the rule said to sell, so the realised cost of a stop cannot be separated from
its stated cost. WMKK 2026-08-07 b35 NO at 0.750: the stop triggered at 0.675
and filled at **0.060** -- a 92% loss on a "30% stop". ZGGG 35C YES sat flat at
0.260 for seven consecutive reads then printed 0.110 on the eighth, straight
through a 0.203 trigger. Single jumps, not sampling misses: config.py already
concludes no scan interval catches a price that never trades in between.

stop_loss_audit.loose_trigger_price() RECONSTRUCTS the trigger afterwards, which
is not the same thing -- it re-derives from today's constants what the rule said
under the day's own. This records it.

WHY IT STILL MATTERS AFTER P2-2. Mostly mooted on books that stop trading, but
"simulation" stays armed deliberately, so it survives there -- and the
historical record needs the distinction regardless.

THE DISTRIBUTION IS THE POINT, NOT THE MEAN. Most stops fill near their trigger
and a few fill sixty cents away; a mean hides exactly that. Median, 90th
percentile and worst are reported instead.

NO BACKFILL, DELIBERATELY. Snapshot coverage is a median 25% of each position's
hold window with 365 of 514 positions under half, so the record cannot support
reconstructing a fill. A backfilled number would be worse than no number, and
NULL on a historical row is the honest value.
"""
from datetime import date

import pytest

import config
import risk_manager
from models import ExitDecision, Position

UNIT_ENTRY = 0.750          # NO at 0.750 -> risk unit min(0.75, 0.25) = 0.25
LOOSE_HOUR = 6              # before EDGE_DECAY_TIGHTEN_HOUR_LOCAL


def _position(entry_price=UNIT_ENTRY, side="NO", entry_bid=None,
              status="open", **kw) -> Position:
    return Position(
        position_id="WMKK:2026-08-07:35:NO:x",
        station_icao="WMKK",
        target_date=date(2026, 8, 7),
        bucket_c=35,
        side=side,
        entry_price=entry_price,
        size_usd=10.0,
        entry_time="2026-08-07T02:00:00+00:00",
        status=status,
        high_water_mark=entry_price,
        entry_bid=entry_bid,
        **kw,
    )


def _evaluate(position, current_price, hour=LOOSE_HOUR):
    return risk_manager.evaluate_exit(position, current_price, hour)


# ---------------------------------------------------------------------------
# The trigger is recorded on the decision
# ---------------------------------------------------------------------------

def test_a_stop_decision_reports_the_price_the_rule_fired_at():
    """
    THE ACCEPTANCE CASE. Entry 0.750 NO: risk unit is min(0.75, 0.25) = 0.25,
    STOP_LOSS_PCT 0.30 buys a 0.075 distance, so the rule said to sell at 0.675.
    The book filled at 0.060.
    """
    decision = _evaluate(_position(), 0.060)

    assert decision.should_exit
    assert decision.reason == "stop_loss"
    assert decision.trigger_price == pytest.approx(0.675)


def test_the_slippage_is_the_trigger_minus_the_fill():
    """0.675 - 0.060 = 0.615. That is what the stop cost beyond what it said."""
    decision = _evaluate(_position(), 0.060)

    assert risk_manager.stop_slippage(decision) == pytest.approx(0.615)


def test_a_stop_that_fills_near_its_trigger_reports_almost_no_slippage():
    """
    The other end of the distribution, and the reason a mean is useless: most
    stops land here.

    0.670 rather than 0.675 because the trigger comparison is `>=` against a
    float: 0.750 - 0.675 evaluates to 0.07499999999999996, a hair under the
    0.075 threshold, so exactly-at-the-trigger does not fire. Pre-existing
    behaviour and immaterial at a 0.01 tick -- noted so the number is not read
    as arbitrary.
    """
    decision = _evaluate(_position(), 0.670)

    assert decision.should_exit
    assert risk_manager.stop_slippage(decision) == pytest.approx(0.005)


def test_the_trigger_is_measured_from_the_stop_basis_not_the_entry_ask():
    """
    stop_basis_price() moved the stop onto the entry-side BID. The recorded
    trigger has to follow it, or the slippage number is measured against a
    starting point the rule did not use.
    """
    # 0.10, not 0.01: at or below config.MIN_EXIT_PRICE the worthless-bid
    # carve-out skips the stop entirely, so a lower fill would test that branch
    # instead of this one.
    with_bid = _evaluate(_position(entry_bid=0.730), 0.10)

    assert with_bid.stop_basis == "entry_bid"
    assert with_bid.trigger_price == pytest.approx(0.730 - 0.075)


# ---------------------------------------------------------------------------
# Everything that is not a stop has no trigger
# ---------------------------------------------------------------------------

def test_a_take_profit_records_no_trigger_price():
    """
    A take-profit has a target, not a stop trigger, and conflating the two
    would put a number in the slippage distribution that is not slippage.
    """
    # Entry 0.30 YES: unit 0.30, PROFIT_TAKE_PCT 0.50 -> target 0.45.
    decision = _evaluate(_position(entry_price=0.30, side="YES"), 0.50)

    assert decision.reason == "take_profit"
    assert decision.trigger_price is None
    assert risk_manager.stop_slippage(decision) is None


def test_a_hold_records_no_trigger_price():
    decision = _evaluate(_position(entry_price=0.30, side="YES"), 0.31)

    assert not decision.should_exit
    assert decision.trigger_price is None


def test_slippage_is_none_rather_than_zero_when_there_is_no_trigger():
    """
    "No stop fired" and "a stop fired and filled exactly" are different facts.
    Zero for the first would put a phantom at the good end of the distribution.
    """
    assert risk_manager.stop_slippage(
        ExitDecision(position_id="p", should_exit=False, reason="hold",
                     current_price=0.3, pnl_pct=0.0)
    ) is None


# ---------------------------------------------------------------------------
# It reaches storage, and is not backfilled
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    return str(tmp_path / "t.sqlite3")


def test_the_trigger_price_is_persisted_on_the_closed_row(db):
    import storage

    position = _position()
    storage.open_position(position)
    storage.close_position(position.position_id, exit_price=0.060,
                           exit_time="2026-08-07T06:00:00+00:00",
                           status="closed_stop_loss", reason="stop_loss",
                           trigger_price=0.675)

    closed = storage.load_position_history("WMKK")[0]
    assert closed.exit_price == pytest.approx(0.060)
    assert closed.trigger_price == pytest.approx(0.675)


def test_a_historical_row_keeps_a_null_trigger_rather_than_a_reconstruction(db):
    """
    NO BACKFILL. Coverage is a median 25% of each hold window; the record cannot
    support reconstructing where a stop filled, and a fabricated number here
    would corrupt the very distribution this item exists to measure. Contrast
    entry_fee_per_share (P1-8b), which IS backfilled -- because it is a function
    of a stored column rather than of a book that is gone.
    """
    import storage

    position = _position()
    storage.open_position(position)
    storage.close_position(position.position_id, exit_price=0.060,
                           exit_time="x", status="closed_stop_loss",
                           reason="stop_loss")

    assert storage.load_position_history("WMKK")[0].trigger_price is None


# ---------------------------------------------------------------------------
# The distribution, not the mean
# ---------------------------------------------------------------------------

def _closed(trigger, exit_price):
    return _position(status="closed_stop_loss", exit_price=exit_price,
                     trigger_price=trigger)


def test_the_report_gives_median_p90_and_worst():
    rows = [_closed(0.675, 0.675 - s) for s in
            (0.0, 0.0, 0.005, 0.01, 0.01, 0.015, 0.02, 0.03, 0.20, 0.615)]

    dist = risk_manager.stop_slippage_distribution(rows)

    assert dist["n"] == 10
    assert dist["median"] == pytest.approx(0.0125, abs=1e-6)
    assert dist["p90"] == pytest.approx(0.20, abs=0.01)
    assert dist["worst"] == pytest.approx(0.615)


def test_the_report_does_not_lead_with_a_mean():
    """
    The mean of that sample is 0.09 -- seven times its median. Reporting it as
    the headline is precisely how "stops fill near their trigger" survives a
    record that says otherwise.
    """
    dist = risk_manager.stop_slippage_distribution(
        [_closed(0.675, 0.675 - s) for s in (0.0, 0.0, 0.615)]
    )

    assert "mean" not in dist


def test_rows_without_a_recorded_trigger_are_counted_not_dropped():
    """
    Every historical stop has a NULL trigger. Silently excluding them would
    make the distribution look like the whole record when it is the recent
    slice of it.
    """
    rows = [_closed(0.675, 0.60), _position(status="closed_stop_loss", exit_price=0.10)]

    dist = risk_manager.stop_slippage_distribution(rows)

    assert dist["n"] == 1
    assert dist["n_without_trigger"] == 1


def test_an_empty_sample_returns_none_rather_than_zeros():
    assert risk_manager.stop_slippage_distribution([]) is None


# ---------------------------------------------------------------------------
# stop_sweep must state its fill assumption
# ---------------------------------------------------------------------------

def test_the_stop_sweep_states_its_fill_assumption_in_its_own_output():
    """
    ACCEPTANCE. config.py documents the trap exactly: assume a cap always fills
    at its trigger and the best cell is +$146; assume it fills at the lowest
    quote the record can prove existed and the same cell is -$75. Same data,
    opposite sign. A sweep whose output does not say which it assumed is a
    number that can be read either way.
    """
    from backtest import stop_sweep

    assert hasattr(stop_sweep, "FILL_ASSUMPTION_NOTE")
    note = stop_sweep.FILL_ASSUMPTION_NOTE.lower()
    assert "fill" in note
    assert "trigger" in note
