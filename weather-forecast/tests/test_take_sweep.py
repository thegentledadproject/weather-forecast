"""
tests/test_take_sweep.py

The scoring half of backtest/take_sweep.py.

The orchestration (sweep(), main()) needs a full replay and is not covered
here. The SCORING is, because it is where a bug produces a plausible
number rather than a crash -- and a lottery cohort is the worst possible
place for that: a handful of large multiples against many total losses,
where one 10x can carry a $-weighted average that no typical trade
resembles. Every statistic below exists to stop a single trade from
speaking for a row.
"""

from contextlib import contextmanager

import pytest

import config
from backtest import take_sweep


class _FakePosition:
    """Only the fields _cohort() reads."""

    def __init__(self, entry_price, exit_price, size_usd, status="closed_take_profit"):
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.size_usd = size_usd
        self.status = status


class _FakeRun:
    def __init__(self, closed):
        self.closed_positions = closed
        self.unresolved_positions = []


# --------------------------------------------------------------------------
# _cohort -- which positions belong to the lottery band
# --------------------------------------------------------------------------

def test_cohort_splits_on_the_lottery_threshold_not_on_the_swept_constant():
    """
    The partition must follow the position's own entry price against
    LOTTERY_PRICE_THRESHOLD -- the same test risk_manager.evaluate_exit()
    branches on. Splitting on anything else would put positions in a cohort
    whose exit rule they never ran under.
    """
    runs = [_FakeRun([
        _FakePosition(0.08, 0.20, 1.0),                      # lottery
        _FakePosition(config.LOTTERY_PRICE_THRESHOLD, 0.30, 1.0),  # exactly at: NOT lottery
        _FakePosition(0.42, 0.55, 1.0),                      # normal
    ])]

    lottery = take_sweep._cohort(runs, lottery=True)
    normal = take_sweep._cohort(runs, lottery=False)

    assert len(lottery) == 1
    assert len(normal) == 2
    assert lottery[0][0] == pytest.approx((0.20 - 0.08) / 0.08)


def test_cohort_skips_positions_with_no_exit_price():
    """
    A position with no exit price has not been scored by anything. Dividing
    by its entry price anyway would book a -100% that never happened.
    """
    runs = [_FakeRun([
        _FakePosition(0.08, None, 1.0),
        _FakePosition(0.08, 0.16, 1.0),
    ])]

    assert len(take_sweep._cohort(runs, lottery=True)) == 1


# --------------------------------------------------------------------------
# _score -- the statistics that stop one trade speaking for a row
# --------------------------------------------------------------------------

def test_drop1_removes_the_largest_contributor_not_the_largest_return():
    """
    THE POINT OF drop1. A big return on a tiny stake moves the book less
    than a modest return on a large one, and it is the BOOK the row is
    claiming something about. Removing the largest return instead would
    leave the trade that actually carried the row in place.
    """
    rows = [
        (10.0, 1.0, "closed_take_profit"),    # +1000% on $1  -> +$10 contribution
        (0.50, 100.0, "closed_take_profit"),  # +50%   on $100 -> +$50 contribution
        (-1.0, 10.0, "closed_resolution"),    # -100%  on $10  -> -$10
    ]

    s = take_sweep._score(rows)

    # Full book: (10 + 50 - 10) / 111 = +45.0%
    assert s["ret"] == pytest.approx(50.0 / 111.0)
    # Dropping the +$50 trade (largest contributor), not the +1000% one:
    # (10 - 10) / 11 = 0.0
    assert s["ret_drop1"] == pytest.approx(0.0)


def test_drop1_can_remove_a_loser_when_the_loser_is_what_carried_the_row():
    """
    Symmetry check. drop1 is about magnitude of influence, so the trade it
    removes is sometimes the worst one -- otherwise a row dominated by a
    single blow-up would look robust.
    """
    rows = [
        (-1.0, 500.0, "closed_resolution"),
        (0.20, 10.0, "closed_take_profit"),
        (0.10, 10.0, "closed_take_profit"),
    ]

    s = take_sweep._score(rows)

    assert s["ret"] < 0
    assert s["ret_drop1"] == pytest.approx(3.0 / 20.0)


def test_median_ignores_stake_and_reports_the_typical_trade():
    """
    The median is the counterweight to the $-weighted return, which is why
    it is unweighted: the 2026-08-15 cohort checkpoint had -0.3% $-weighted
    against a MEDIAN of -44%, and reporting only the first would have
    described a book nobody was actually running.
    """
    rows = [
        (5.0, 1000.0, "closed_take_profit"),
        (-1.0, 1.0, "closed_resolution"),
        (-1.0, 1.0, "closed_resolution"),
    ]

    s = take_sweep._score(rows)

    assert s["ret"] > 0
    assert s["median"] == pytest.approx(-1.0)


def test_status_counts_separate_taking_profit_from_settling():
    """
    The take/settle split is the shape of the answer: a wider distance
    should convert take-profits into settlements, and the counts are how
    you see that happening rather than inferring it from P&L.
    """
    rows = [
        (0.5, 1.0, "closed_take_profit"),
        (0.5, 1.0, "closed_take_profit"),
        (-1.0, 1.0, "closed_resolution"),
        (-0.3, 1.0, "closed_stop_loss"),
    ]

    s = take_sweep._score(rows)

    assert (s["took"], s["settled"], s["n"]) == (2, 1, 4)


def test_an_empty_cohort_scores_without_raising():
    """
    A sweep row can legitimately contain no lottery positions -- a narrow
    window, or a station list with none. That must print as zeroes, not
    take down the whole sweep at the last row.
    """
    s = take_sweep._score([])

    assert s["n"] == 0 and s["ret"] == 0.0 and s["median"] == 0.0


def test_a_single_position_leaves_drop1_defined():
    """
    Dropping the only trade leaves no stake to divide by. Guarded because a
    ZeroDivisionError here would surface only on the sparsest row -- which,
    given the coverage problem in the module docstring, is the row most
    likely to be hit first.
    """
    s = take_sweep._score([(0.5, 1.0, "closed_take_profit")])

    assert s["n"] == 1
    assert s["ret"] == pytest.approx(0.5)
    assert s["ret_drop1"] == 0.0


# --------------------------------------------------------------------------
# _lottery_take -- THE ALIAS GOTCHA
# --------------------------------------------------------------------------

def test_the_context_manager_sets_and_restores_both_constants():
    """
    stop_sweep.py documents this as the gotcha that silently reintroduces a
    tightening the sweep is not modelling: the tightened constant is bound
    by VALUE at import, so setting the loose one alone leaves it stale.
    """
    before = (config.LOTTERY_PROFIT_TAKE_PCT, config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT)

    with take_sweep._lottery_take(4.0):
        assert config.LOTTERY_PROFIT_TAKE_PCT == 4.0
        assert config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT == 4.0

    assert (config.LOTTERY_PROFIT_TAKE_PCT, config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT) == before


def test_the_constants_are_restored_even_when_the_replay_raises():
    """
    engine.run() raising inside the block must not leave every LATER row of
    the sweep running at this row's distance -- which would silently score
    them all identically.
    """
    before = config.LOTTERY_PROFIT_TAKE_PCT

    with pytest.raises(RuntimeError):
        with take_sweep._lottery_take(4.0):
            raise RuntimeError("station blew up mid-replay")

    assert config.LOTTERY_PROFIT_TAKE_PCT == before


def test_no_take_is_genuinely_unreachable_for_a_lottery_entry():
    """
    The "none" row has to mean no take-profit, not a very distant one. The
    risk unit for a lottery entry IS its entry price, so the trigger sits at
    entry x (1 + NO_TAKE) -- which must exceed 1.00 for every price in the
    band, or the row quietly becomes a take at some reachable level.
    """
    highest_lottery_entry = config.LOTTERY_PRICE_THRESHOLD
    trigger = highest_lottery_entry * (1 + take_sweep.NO_TAKE)

    assert trigger > 1.0


# --------------------------------------------------------------------------
# Unresolved positions -- the exclusion that can invert a row
# --------------------------------------------------------------------------

def test_unresolved_are_counted_per_cohort_not_just_in_total():
    """
    THE 2026-08-17..19 RUN. Every closed lottery position in that window was
    a settled loser and every still-alive one -- including the WSSS 33 YES
    ticket this whole question came from -- was unresolved and excluded, so
    the table read -100% at every distance while the winners sat outside it.

    A single total, printed under the table, cannot show that: 12 unresolved
    against 24 closed normal positions is unremarkable, 12 against 10 closed
    lottery ones means the row is describing a minority of its own cohort.
    The count has to be attributable to the band it distorts.
    """
    runs = [_FakeRun([])]
    runs[0].unresolved_positions = [
        _FakePosition(0.08, None, 1.0, status="open"),   # lottery
        _FakePosition(0.06, None, 1.0, status="open"),   # lottery
        _FakePosition(0.63, None, 1.0, status="open"),   # normal
    ]

    assert take_sweep._unresolved_count(runs, lottery=True) == 2
    assert take_sweep._unresolved_count(runs, lottery=False) == 1
