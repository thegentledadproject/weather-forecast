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


# --------------------------------------------------------------------------
# replay_stored -- scoring the REAL book's entries instead of generating new
# ones. See backtest/take_sweep.py's STORED-ENTRY REPLAY MODE docstring for
# why: engine.run() re-decides entries under a capital constraint, so a wider
# take frees capital at a different hour and changes WHICH positions exist.
# The cohort is then not held fixed across rows, which is the confound this
# mode removes -- pinned by
# test_stored_replay_scores_the_identical_cohort_at_every_take_level.
# --------------------------------------------------------------------------

import itertools
import sqlite3
from datetime import date, datetime


def _ts(iso):
    return int(datetime.fromisoformat(iso).timestamp())


_DB_SEQ = itertools.count()


def _make_dbs(tmp_path, positions, snapshots, settlements=()):
    """
    Two throwaway sqlite files shaped like the real ones.

    Each call gets its OWN pair, because the cohort test replays the same
    fixture at five take levels and would otherwise re-create the tables.

    positions:   dicts of the columns replay_stored() reads
    snapshots:   (token_id, iso_ts, price) triples
    settlements: (icao, target_date, bucket_c) triples
    """
    d = tmp_path / ("db%d" % next(_DB_SEQ))
    d.mkdir()
    live = d / "live.sqlite3"
    market = d / "market.sqlite3"

    lc = sqlite3.connect(live)
    lc.execute(
        "create table positions ("
        " position_id text primary key, station_icao text, target_date text,"
        " bucket_c int, side text, entry_price real, size_usd real,"
        " entry_time text, status text, high_water_mark real, exit_price real,"
        " exit_time text, exit_reason text, token_id text, is_paper int,"
        " size_shares real, entry_bid real)"
    )
    lc.execute(
        "create table settled_buckets ("
        " station_icao text, target_date text, bucket_c int)"
    )
    for p in positions:
        cols = ",".join(p)
        marks = ",".join("?" for _ in p)
        lc.execute("insert into positions (%s) values (%s)" % (cols, marks),
                   tuple(p.values()))
    for icao, td, bkt in settlements:
        lc.execute("insert into settled_buckets (station_icao, target_date, bucket_c)"
                   " values (?,?,?)", (icao, td, bkt))
    lc.commit()
    lc.close()

    mc = sqlite3.connect(market)
    mc.execute(
        "create table price_snapshots ("
        " token_id text, ts int, price real, depth_usd real, source text,"
        " fidelity_min int, ask_price real)"
    )
    for token, iso, price in snapshots:
        mc.execute("insert into price_snapshots (token_id, ts, price) values (?,?,?)",
                   (token, _ts(iso), price))
    mc.commit()
    mc.close()
    return live, market


def _position(**kw):
    base = dict(position_id="P1", station_icao="WSSS", target_date="2026-08-20",
                bucket_c=33, side="YES", entry_price=0.10, size_usd=1.0,
                entry_time="2026-08-19T22:00:00+00:00", status="closed_take_profit",
                token_id="T1", is_paper=1, size_shares=10.0)
    base.update(kw)
    return base


@contextmanager
def _no_override():
    yield


def _replay(tmp_path, positions, snapshots, settlements=(), take=None):
    live, market = _make_dbs(tmp_path, positions, snapshots, settlements)
    ctx = take_sweep._lottery_take(take) if take is not None else _no_override()
    with ctx:
        return take_sweep.replay_stored(
            ["WSSS"], date(2026, 8, 19), date(2026, 8, 21),
            db_path=live, market_db_path=market)


def test_stored_replay_takes_profit_when_the_path_reaches_the_take_level(tmp_path):
    """
    An 0.10 lottery entry with a 50% take exits at 0.15. The path touches
    0.16, so the take fires and the position is booked at the SNAPSHOT
    price that triggered it, not at the theoretical level -- the same
    discrete-cycle fill the live system gets.
    """
    run = _replay(
        tmp_path,
        positions=[_position()],
        snapshots=[("T1", "2026-08-19T23:00:00+00:00", 0.11),
                   ("T1", "2026-08-20T00:00:00+00:00", 0.16),
                   ("T1", "2026-08-20T01:00:00+00:00", 0.04)],
        settlements=[("WSSS", "2026-08-20", 31)],
        take=0.50)

    assert len(run.closed_positions) == 1
    p = run.closed_positions[0]
    assert p.status == "closed_take_profit"
    assert p.exit_price == pytest.approx(0.16)


def test_stored_replay_rides_to_settlement_when_the_take_is_never_reached(tmp_path):
    """
    Same ticket, take pushed out of reach. Nothing on the path triggers, so
    it settles -- and the winning bucket pays 1.00, which is exactly the
    payoff the take was capping.
    """
    run = _replay(
        tmp_path,
        positions=[_position()],
        snapshots=[("T1", "2026-08-19T23:00:00+00:00", 0.11),
                   ("T1", "2026-08-20T00:00:00+00:00", 0.16)],
        settlements=[("WSSS", "2026-08-20", 33)],
        take=take_sweep.NO_TAKE)

    assert len(run.closed_positions) == 1
    p = run.closed_positions[0]
    assert p.status == "closed_resolution"
    assert p.exit_price == pytest.approx(1.0)


def test_stored_replay_scores_the_identical_cohort_at_every_take_level(tmp_path):
    """
    THE POINT OF THIS MODE. engine.run() re-decides entries under a capital
    constraint, so a wider take frees capital at a different hour and
    changes WHICH positions exist -- the 2026-08-28 sweep went 44 -> 39
    lottery positions between its tightest and widest rows, and that
    difference is not the take's effect. Replaying stored entries must hold
    the cohort fixed.
    """
    positions = [_position(position_id="P%d" % i, token_id="T%d" % i) for i in range(3)]
    snapshots = [("T%d" % i, "2026-08-20T00:00:00+00:00", 0.16) for i in range(3)]
    settlements = [("WSSS", "2026-08-20", 31)]

    counts = set()
    for take in (0.10, 0.25, 0.50, 1.0, take_sweep.NO_TAKE):
        run = _replay(tmp_path, positions, snapshots, settlements, take=take)
        counts.add(len(run.closed_positions) + len(run.unresolved_positions))

    assert counts == {3}


def test_stored_replay_keeps_the_stop_for_a_normal_entry(tmp_path):
    """
    The take is the only thing this sweep varies. A NON-lottery entry still
    has its stop, and a path that dips through it must exit stop_loss --
    otherwise the mode is a take-only walker and would score the normal
    cohort as if the stop had been removed too.
    """
    run = _replay(
        tmp_path,
        positions=[_position(entry_price=0.40, size_shares=2.5)],
        snapshots=[("T1", "2026-08-19T23:00:00+00:00", 0.36),
                   ("T1", "2026-08-20T00:00:00+00:00", 0.26)],
        settlements=[("WSSS", "2026-08-20", 33)],
        take=0.50)

    assert len(run.closed_positions) == 1
    p = run.closed_positions[0]
    assert p.status == "closed_stop_loss"
    assert p.exit_price == pytest.approx(0.26)


def test_stored_replay_evaluates_each_snapshot_at_its_own_simulated_local_hour(tmp_path):
    """
    evaluate_exit() halves the take after EDGE_DECAY_TIGHTEN_HOUR_LOCAL, so
    the replay must evaluate each snapshot at ITS OWN simulated local hour.
    Shown on a NORMAL entry, because _lottery_take() deliberately pins both
    lottery constants to the same value and so models no tightening there.

    A 0.40 entry takes at +50% of the risk unit (0.60) before 10:00 local
    and +25% (0.50) after. WSSS is UTC+8, so 03:00Z is 11:00 local: a price
    of 0.52 fires only under the tightened distance. The control at 00:00Z
    (08:00 local) is the same price and must NOT fire -- together they fail
    if the replay pins one hour, or reads the wall clock.
    """
    tightened = _replay(
        tmp_path,
        positions=[_position(entry_price=0.40, size_shares=2.5)],
        snapshots=[("T1", "2026-08-20T03:00:00+00:00", 0.52)],
        settlements=[("WSSS", "2026-08-20", 31)])

    loose = _replay(
        tmp_path,
        positions=[_position(entry_price=0.40, size_shares=2.5)],
        snapshots=[("T1", "2026-08-20T00:00:00+00:00", 0.52)],
        settlements=[("WSSS", "2026-08-20", 31)])

    assert tightened.closed_positions[0].status == "closed_take_profit"
    assert loose.closed_positions[0].status == "closed_resolution"


def test_stored_replay_reports_a_position_with_no_settlement_as_unresolved(tmp_path):
    """
    The same censoring rule the engine path has: no settlement observation
    means the position cannot be scored, and it must land in
    unresolved_positions so _unresolved_count() still reports it. Booking it
    as a loss would manufacture exactly the -100% the lottery band is most
    vulnerable to (see _unresolved_count's docstring).
    """
    run = _replay(
        tmp_path,
        positions=[_position()],
        snapshots=[("T1", "2026-08-19T23:00:00+00:00", 0.11)],
        settlements=[])

    assert run.closed_positions == []
    assert len(run.unresolved_positions) == 1


def test_sweep_in_stored_mode_holds_the_lottery_cohort_fixed_across_takes(tmp_path):
    """
    The mode has to reach the scorecard, not just exist. Three identical
    lottery tickets, swept at three distances: in stored mode every row
    must score the same three positions, because the entries no longer
    depend on the take. The engine path cannot promise that -- which is
    the whole reason for the flag.
    """
    live, market = _make_dbs(
        tmp_path,
        positions=[_position(position_id="P%d" % i, token_id="T%d" % i) for i in range(3)],
        snapshots=[("T%d" % i, "2026-08-20T00:00:00+00:00", 0.16) for i in range(3)],
        settlements=[("WSSS", "2026-08-20", 31)])

    results = take_sweep.sweep(
        ["WSSS"], date(2026, 8, 19), date(2026, 8, 21),
        [0.25, 0.50, take_sweep.NO_TAKE],
        market_db_path=market, stored=True, db_path=live)

    assert {results[pct]["lottery"]["n"] for pct in results} == {3}


def test_stored_replay_measures_the_stop_from_the_recorded_entry_bid(tmp_path):
    """
    The sweep drives the real risk_manager.evaluate_exit(), so a Position it
    rebuilds without entry_bid measures the stop from the ASK while
    production measures it from the BID -- and the cohort this sweep exists
    to score would then exclude positions production is still holding.

    Bought at the 0.15 ask on a 0.10/0.15 book. The path never goes below
    the entry bid, so no stop is due; on the old basis the 0.05 spread alone
    clears the 0.045 stop distance and the position is cut on the first tick.
    """
    run = _replay(
        tmp_path,
        positions=[_position(entry_price=0.15, entry_bid=0.10, bucket_c=33)],
        snapshots=[("T1", "2026-08-19T23:00:00+00:00", 0.10),
                   ("T1", "2026-08-20T00:00:00+00:00", 0.10)],
        settlements=[("WSSS", "2026-08-20", 33)],
        take=take_sweep.NO_TAKE)

    assert len(run.closed_positions) == 1
    p = run.closed_positions[0]
    assert p.status == "closed_resolution", (
        f"stopped on the spread alone, exiting at {p.exit_price}"
    )


def test_stored_replay_still_runs_against_a_table_without_the_column(tmp_path):
    """
    replay_stored() reads whatever `select *` returns, and a database
    written before Position.entry_bid existed has no such column. Those rows
    replay on the old basis, which is what they were traded under -- what
    they must not do is raise.
    """
    live, market = _make_dbs(
        tmp_path,
        positions=[_position(entry_price=0.15, bucket_c=33)],
        snapshots=[("T1", "2026-08-19T23:00:00+00:00", 0.10)],
        settlements=[("WSSS", "2026-08-20", 33)],
    )
    con = sqlite3.connect(live)
    con.execute("alter table positions drop column entry_bid")
    con.commit()
    con.close()

    run = take_sweep.replay_stored(
        ["WSSS"], date(2026, 8, 19), date(2026, 8, 21),
        db_path=live, market_db_path=market)

    assert len(run.closed_positions) == 1
    assert run.closed_positions[0].status == "closed_stop_loss"
