"""
tests/test_cohort_monitor.py

cohort_monitor.py -- P0-5, the standing hold-vs-actual cohort monitor.

WHY IT EXISTS. config.py's measurement block (2026-09-02) establishes that
the book's P&L is a PRICE edge, not a forecasting one: mean entry 0.306
against a 0.344 realised win rate, while the model loses to the market on
Brier (0.1930 vs 0.1842) and is ~9 points overconfident. The consequence
stated there and acted on here is that this edge can decay without any
calibration metric noticing -- so the cohort has to be re-scored on the
hold-vs-actual basis, on a rolling window, against a pre-committed
threshold.

WHAT THESE TESTS PIN, and what they deliberately do not.

  * THE ARITHMETIC, synthetically. Every scenario, the reconciliation
    identity, the price-edge line, the clustering of the bootstrap and the
    kill criterion are exercised against hand-built positions where the
    right answer is computable by hand. No database.

  * THE PUBLISHED TOTALS ARE PINNED IN ONE PLACE ONLY -- the module's own
    PUBLISHED_* constants -- and reproduced against the real book by
    `python cohort_monitor.py --reproduce`, which needs the deployed
    database. A unit test cannot reproduce -$295.15 without that data, and
    a test that pretended to would be pinning a fixture, not a
    measurement. What IS tested here is that the reproduction check
    reports a mismatch rather than swallowing one.

THE RECONCILIATION IDENTITY IS THE LOAD-BEARING TEST. config.py names a
$43.74 residual between the two per-rule costs and the held-based total,
and calls it unexplained. This module's decomposition closes to the cent
BY CONSTRUCTION -- stop cost + take cost + the resolution-close gap -- so
the residual stops being a mystery and becomes a measured third term.
test_reconciliation_closes_to_the_cent is what guarantees the identity
holds for any cohort, which is what makes the third term trustworthy.
"""

from datetime import date

import pytest

import cohort_monitor
import config
from models import Position

WINNING_BUCKET = 32
BOUNDS = (30, 34)
SOURCE = "test"


def _settled(*days) -> dict:
    """settled_buckets rows shaped as storage.load_settled_buckets returns."""
    return {day: (WINNING_BUCKET, BOUNDS[0], BOUNDS[1], SOURCE, 1) for day in days}


def _position(
    *,
    day=date(2026, 8, 10),
    bucket_c=WINNING_BUCKET,
    side="YES",
    entry_price=0.30,
    exit_price=0.20,
    size_usd=10.0,
    status="closed_stop_loss",
    station_icao="WSSS",
    suffix="",
) -> Position:
    return Position(
        position_id=f"{station_icao}:{day}:{bucket_c}:{side}{suffix}",
        station_icao=station_icao,
        target_date=day,
        bucket_c=bucket_c,
        side=side,
        entry_price=entry_price,
        size_usd=size_usd,
        entry_time=f"{day}T02:00:00+00:00",
        status=status,
        high_water_mark=entry_price,
        exit_price=exit_price,
        exit_time=f"{day}T06:00:00+00:00",
    )


def _rows(positions, settled=None, **kwargs):
    rows, _ = cohort_monitor.cohort_rows(
        positions,
        settled if settled is not None else _settled(date(2026, 8, 10)),
        **kwargs,
    )
    return rows


# ---------------------------------------------------------------------------
# Which rows are in the cohort at all
# ---------------------------------------------------------------------------

def test_a_position_whose_day_never_settled_is_skipped_with_a_reason():
    rows, skipped = cohort_monitor.cohort_rows([_position()], settled={})
    assert rows == []
    assert sum(skipped.values()) == 1


def test_a_position_with_no_stored_model_prob_is_still_in_the_cohort():
    """
    The measured cohort is 514 rows; only 358 carry a model_prob. Requiring
    one -- as promotion_dossier.score_entries does, because Brier needs it --
    would silently drop 156 rows of real P&L.
    """
    rows = _rows([_position()])
    assert len(rows) == 1
    assert rows[0]["model_prob"] is None


def test_a_still_open_position_is_skipped():
    rows, skipped = cohort_monitor.cohort_rows(
        [_position(status="open", exit_price=None)], _settled(date(2026, 8, 10))
    )
    assert rows == []
    assert sum(skipped.values()) == 1


# ---------------------------------------------------------------------------
# Per-row P&L under each scenario
# ---------------------------------------------------------------------------

def test_held_pnl_pays_par_on_the_winning_bucket():
    """$10 at 0.30 buys 33.33 shares; a winner is worth $33.33, so +$23.33."""
    row = _rows([_position(entry_price=0.30, size_usd=10.0)])[0]
    assert cohort_monitor.scenario_pnl_usd(row, "held") == pytest.approx(23.3333, abs=1e-4)


def test_held_pnl_loses_the_whole_stake_on_a_losing_bucket():
    row = _rows([_position(bucket_c=WINNING_BUCKET + 1)])[0]
    assert cohort_monitor.scenario_pnl_usd(row, "held") == pytest.approx(-10.0)


def test_held_pnl_pays_a_no_side_on_every_bucket_but_the_winner():
    row = _rows([_position(side="NO", bucket_c=WINNING_BUCKET + 1, entry_price=0.30)])[0]
    assert cohort_monitor.scenario_pnl_usd(row, "held") == pytest.approx(23.3333, abs=1e-4)


def test_as_traded_pnl_is_the_stored_exit_against_the_stored_entry():
    """0.30 -> 0.20 on $10 is -33.3%, i.e. -$3.33."""
    row = _rows([_position(entry_price=0.30, exit_price=0.20, size_usd=10.0)])[0]
    assert cohort_monitor.scenario_pnl_usd(row, "as_traded") == pytest.approx(-3.3333, abs=1e-4)


def test_stop_only_replaces_a_take_profit_exit_with_settlement():
    row = _rows([_position(status="closed_take_profit", exit_price=0.45)])[0]
    assert cohort_monitor.scenario_pnl_usd(row, "stop_only") == pytest.approx(
        cohort_monitor.scenario_pnl_usd(row, "held")
    )


def test_stop_only_keeps_a_stop_loss_exit_as_traded():
    row = _rows([_position(status="closed_stop_loss", exit_price=0.20)])[0]
    assert cohort_monitor.scenario_pnl_usd(row, "stop_only") == pytest.approx(
        cohort_monitor.scenario_pnl_usd(row, "as_traded")
    )


def test_take_only_replaces_a_stop_loss_exit_with_settlement():
    row = _rows([_position(status="closed_stop_loss", exit_price=0.20)])[0]
    assert cohort_monitor.scenario_pnl_usd(row, "take_only") == pytest.approx(
        cohort_monitor.scenario_pnl_usd(row, "held")
    )


def test_neither_replaces_both_price_exits_with_settlement():
    stop = _rows([_position(status="closed_stop_loss")])[0]
    take = _rows([_position(status="closed_take_profit", exit_price=0.45)])[0]
    for row in (stop, take):
        assert cohort_monitor.scenario_pnl_usd(row, "neither") == pytest.approx(
            cohort_monitor.scenario_pnl_usd(row, "held")
        )


def test_a_trailing_stop_is_classified_as_a_stop():
    """
    The trailing stop was removed 2026-08-17 but its closed rows are still
    in the window, and filing them as "other" would move real stop cost into
    the residual this module exists to name.
    """
    row = _rows([_position(status="closed_trailing_stop")])[0]
    assert row["exit_class"] == "stop"


def test_a_resolution_close_is_neither_a_stop_nor_a_take():
    row = _rows([_position(status="closed_resolution", exit_price=1.0)])[0]
    assert row["exit_class"] == "other"
    assert cohort_monitor.scenario_pnl_usd(row, "neither") == pytest.approx(
        cohort_monitor.scenario_pnl_usd(row, "as_traded")
    )


# ---------------------------------------------------------------------------
# The reconciliation identity -- the load-bearing test
# ---------------------------------------------------------------------------

def _mixed_cohort():
    """
    One of each exit class, including a resolution close booked at a price
    that is NOT its settlement value -- which is exactly the shape that
    produces config.py's unexplained residual.
    """
    day = date(2026, 8, 10)
    return [
        _position(day=day, status="closed_stop_loss", exit_price=0.18, suffix="-a"),
        _position(day=day, status="closed_take_profit", exit_price=0.45, suffix="-b"),
        _position(day=day, status="closed_resolution", exit_price=0.92, suffix="-c"),
        _position(
            day=day,
            status="closed_resolution",
            exit_price=0.04,
            bucket_c=WINNING_BUCKET + 1,
            suffix="-d",
        ),
    ]


def test_reconciliation_closes_to_the_cent():
    summary = cohort_monitor.summarize(_rows(_mixed_cohort()))
    rec = summary["reconciliation"]
    assert rec["stop_cost_usd"] + rec["take_cost_usd"] + rec["other_gap_usd"] == pytest.approx(
        rec["held_minus_as_traded_usd"], abs=0.005
    )
    assert rec["closes"] is True


def test_the_other_gap_is_exactly_held_minus_neither():
    """
    config.py's $21.65 between held and the table's "neither" row is this
    term, and its $43.74 residual is this term double-counted. Naming it
    once, as a measured quantity, is what retires both.
    """
    summary = cohort_monitor.summarize(_rows(_mixed_cohort()))
    held = summary["scenarios"]["held"]["pnl_usd"]
    neither = summary["scenarios"]["neither"]["pnl_usd"]
    assert summary["reconciliation"]["other_gap_usd"] == pytest.approx(held - neither, abs=0.005)


def test_a_cohort_with_no_resolution_closes_has_a_zero_other_gap():
    rows = _rows([
        _position(status="closed_stop_loss", suffix="-a"),
        _position(status="closed_take_profit", exit_price=0.45, suffix="-b"),
    ])
    summary = cohort_monitor.summarize(rows)
    assert summary["reconciliation"]["other_gap_usd"] == pytest.approx(0.0, abs=0.005)
    assert summary["scenarios"]["held"]["pnl_usd"] == pytest.approx(
        summary["scenarios"]["neither"]["pnl_usd"], abs=0.005
    )


# ---------------------------------------------------------------------------
# The price-edge line -- the decay alarm
# ---------------------------------------------------------------------------

def test_price_edge_is_the_realised_win_rate_minus_the_mean_entry_price():
    """Two rows at 0.30, one winner: win rate 0.5, edge +0.20."""
    rows = _rows([
        _position(entry_price=0.30, suffix="-a"),
        _position(entry_price=0.30, bucket_c=WINNING_BUCKET + 1, suffix="-b"),
    ])
    edge = cohort_monitor.price_edge(rows)
    assert edge["mean_entry_price"] == pytest.approx(0.30)
    assert edge["win_rate"] == pytest.approx(0.50)
    assert edge["price_edge"] == pytest.approx(0.20)


def test_the_net_price_edge_charges_the_entry_side_taker_fee():
    """
    Held to settlement pays the ENTRY fee only -- redeeming a resolved
    position is not a trade. At 0.30 that is 0.05 x 0.70 x 0.30 = $0.0105
    per share, so the net edge sits exactly that far below the gross one.
    """
    rows = _rows([
        _position(entry_price=0.30, suffix="-a"),
        _position(entry_price=0.30, bucket_c=WINNING_BUCKET + 1, suffix="-b"),
    ])
    edge = cohort_monitor.price_edge(rows)
    assert edge["mean_fee_per_share"] == pytest.approx(0.0105)
    assert edge["net_price_edge"] == pytest.approx(edge["price_edge"] - 0.0105)


# ---------------------------------------------------------------------------
# Honesty rules carried from promotion_dossier / calibration_panel
# ---------------------------------------------------------------------------

def test_an_empty_cohort_returns_none_rather_than_zeros():
    assert cohort_monitor.summarize([]) is None
    assert cohort_monitor.price_edge([]) is None


def test_the_summary_reports_n_days_alongside_n():
    rows = _rows(
        [
            _position(day=date(2026, 8, 10), suffix="-a"),
            _position(day=date(2026, 8, 10), suffix="-b"),
        ],
        settled=_settled(date(2026, 8, 10)),
    )
    summary = cohort_monitor.summarize(rows)
    assert summary["n"] == 2
    assert summary["n_days"] == 1


def test_station_days_are_counted_per_station_not_per_calendar_day():
    day = date(2026, 8, 10)
    rows = _rows(
        [_position(day=day, station_icao="WSSS"), _position(day=day, station_icao="RCSS")],
        settled=_settled(day),
    )
    assert cohort_monitor.summarize(rows)["n_days"] == 2


# ---------------------------------------------------------------------------
# The bootstrap must cluster on station-days
# ---------------------------------------------------------------------------

def test_a_single_station_day_has_a_degenerate_clustered_interval():
    """
    THE TEST THAT SEPARATES CLUSTERED FROM NAIVE. With one cluster, every
    resample draws that same cluster, so the interval collapses onto the
    point estimate. A row-level bootstrap over these four rows would return
    a visibly wide interval instead -- which is precisely the dishonest
    number config.py's "252 station-days for 514 rows" warns about.
    """
    rows = _rows(_mixed_cohort())
    summary = cohort_monitor.summarize(rows)
    low, high = summary["ci"]["held_return_pct"]
    point = summary["scenarios"]["held"]["return_pct"]
    assert low == pytest.approx(point, abs=1e-9)
    assert high == pytest.approx(point, abs=1e-9)


def test_more_station_days_widen_the_interval():
    day_a, day_b = date(2026, 8, 10), date(2026, 8, 11)
    rows = _rows(
        [
            _position(day=day_a, bucket_c=WINNING_BUCKET, exit_price=0.40, suffix="-a"),
            _position(day=day_b, bucket_c=WINNING_BUCKET + 1, exit_price=0.05, suffix="-b"),
        ],
        settled=_settled(day_a, day_b),
    )
    low, high = cohort_monitor.summarize(rows)["ci"]["held_return_pct"]
    assert high - low > 0.0


def test_the_bootstrap_is_deterministic_for_a_given_seed():
    rows = _rows(
        [
            _position(day=date(2026, 8, 10), suffix="-a"),
            _position(day=date(2026, 8, 11), bucket_c=WINNING_BUCKET + 1, suffix="-b"),
        ],
        settled=_settled(date(2026, 8, 10), date(2026, 8, 11)),
    )
    first = cohort_monitor.summarize(rows)["ci"]
    second = cohort_monitor.summarize(rows)["ci"]
    assert first == second


# ---------------------------------------------------------------------------
# Rolling windows
# ---------------------------------------------------------------------------

def test_a_trailing_window_excludes_rows_dated_before_it():
    old, recent = date(2026, 7, 1), date(2026, 8, 30)
    rows = _rows(
        [_position(day=old, suffix="-a"), _position(day=recent, suffix="-b")],
        settled=_settled(old, recent),
    )
    windows = cohort_monitor.windows(rows, as_of=date(2026, 9, 1))
    assert windows["all_time"]["n"] == 2
    assert windows["trailing_30d"]["n"] == 1


def test_a_window_with_no_rows_is_none_rather_than_a_zero_row():
    rows = _rows([_position(day=date(2026, 7, 1))], settled=_settled(date(2026, 7, 1)))
    windows = cohort_monitor.windows(rows, as_of=date(2026, 9, 1))
    assert windows["trailing_30d"] is None


# ---------------------------------------------------------------------------
# The kill criterion -- pre-committed, and measurement only
# ---------------------------------------------------------------------------

def test_the_kill_criterion_withholds_a_verdict_below_its_minimum_sample():
    rows = _rows([_position()])
    status = cohort_monitor.kill_criterion(
        cohort_monitor.windows(rows, as_of=date(2026, 8, 11))
    )
    assert status["fired"] is None
    assert status["n_days"] < config.COHORT_KILL_MIN_STATION_DAYS


def test_the_kill_criterion_fires_when_the_net_price_edge_sits_below_its_level(monkeypatch):
    """A book paying 0.90 for coin flips has no price edge left."""
    monkeypatch.setattr(config, "COHORT_KILL_MIN_STATION_DAYS", 2)
    day_a, day_b = date(2026, 8, 10), date(2026, 8, 11)
    rows = _rows(
        [
            _position(day=day_a, entry_price=0.90, bucket_c=WINNING_BUCKET + 1, suffix="-a"),
            _position(day=day_b, entry_price=0.90, bucket_c=WINNING_BUCKET + 1, suffix="-b"),
        ],
        settled=_settled(day_a, day_b),
    )
    status = cohort_monitor.kill_criterion(
        cohort_monitor.windows(rows, as_of=date(2026, 8, 11))
    )
    assert status["fired"] is True


def test_the_kill_criterion_holds_when_the_net_price_edge_clears_its_level(monkeypatch):
    monkeypatch.setattr(config, "COHORT_KILL_MIN_STATION_DAYS", 2)
    day_a, day_b = date(2026, 8, 10), date(2026, 8, 11)
    rows = _rows(
        [
            _position(day=day_a, entry_price=0.20, suffix="-a"),
            _position(day=day_b, entry_price=0.20, suffix="-b"),
        ],
        settled=_settled(day_a, day_b),
    )
    status = cohort_monitor.kill_criterion(
        cohort_monitor.windows(rows, as_of=date(2026, 8, 11))
    )
    assert status["fired"] is False


def test_the_kill_criterion_names_no_action():
    """
    Phase 0 is measurement only. What firing MEANS -- halt the station, halt
    the book, drop to paper -- is an open operator decision recorded in
    config.py, and nothing in this module or any import of it may act on it.
    """
    assert not hasattr(cohort_monitor, "halt")
    assert "action" not in cohort_monitor.kill_criterion(
        cohort_monitor.windows(_rows([_position()]), as_of=date(2026, 8, 11))
    )


# ---------------------------------------------------------------------------
# The reproduction check must be able to fail
# ---------------------------------------------------------------------------

def test_the_reproduction_check_flags_a_mismatch_against_the_published_totals():
    """
    The acceptance criterion is "reproduces the four published totals to the
    cent". A check that could only ever pass would satisfy the letter of
    that and none of its purpose, so the failing direction is what is
    tested: a cohort that plainly is not the published one must be reported
    as a mismatch, per scenario.
    """
    report = cohort_monitor.reproduction_check(cohort_monitor.summarize(_rows(_mixed_cohort())))
    assert report["matches"] is False
    assert set(report["by_scenario"]) == set(cohort_monitor.PUBLISHED_TOTALS_USD)
    assert any(not entry["matches"] for entry in report["by_scenario"].values())


def test_the_published_totals_are_the_figures_config_records():
    """
    One copy of these numbers, here, checked against the block that owns
    them. A second transcription is how a monitor ends up reproducing a
    typo of the measurement it was built to re-run.
    """
    assert cohort_monitor.PUBLISHED_TOTALS_USD == {
        "as_traded": -295.15,
        "stop_only": 186.81,
        "take_only": 283.37,
        "neither": 765.33,
        "held": 743.68,
    }
    assert cohort_monitor.PUBLISHED_STAKED_USD == pytest.approx(4049.93)


# ---------------------------------------------------------------------------
# Per-status costs
#
# MEASURED 2026-09-04 against the deployed book over the published window,
# and the reason this breakout exists rather than the three-class one being
# enough: the two stop statuses move in OPPOSITE directions.
#
#     closed_stop_loss       222 rows   cost  +600.61
#     closed_trailing_stop    15 rows   cost   -22.09
#     closed_take_profit     197 rows   cost  +481.96
#     closed_resolution       80 rows   cost   -21.65
#
# The fixed stop cost $600.61 against holding; the trailing stop BEAT
# holding by $22.09 on its 15 rows. Rolling them into one "stop" number
# hides a sign change, and it is exactly the $22.09 that made config.py's
# residual look unexplained -- its "222 fires / $600.61" excluded the
# trailing rows while the table's "take only" column included them.
# ---------------------------------------------------------------------------

def test_the_summary_breaks_cost_out_by_exact_status():
    rows = _rows([
        _position(status="closed_stop_loss", suffix="-a"),
        _position(status="closed_trailing_stop", suffix="-b"),
        _position(status="closed_take_profit", exit_price=0.45, suffix="-c"),
    ])
    by_status = cohort_monitor.summarize(rows)["by_status"]
    assert set(by_status) == {"closed_stop_loss", "closed_trailing_stop", "closed_take_profit"}
    assert by_status["closed_stop_loss"]["n"] == 1


def test_the_per_status_costs_sum_to_the_per_class_costs():
    """
    The two breakouts are the same dollars grouped two ways. If they can
    disagree, one of them is wrong and there is no way to tell which.
    """
    summary = cohort_monitor.summarize(_rows(_mixed_cohort()))
    from_status = sum(cell["cost_usd"] for cell in summary["by_status"].values())
    from_class = sum(cell["cost_usd"] for cell in summary["by_exit_class"].values())
    assert from_status == pytest.approx(from_class, abs=0.005)


def test_a_status_that_beat_holding_reports_a_negative_cost():
    """
    Sign convention, pinned because it is the one that carries the finding:
    cost is held minus as-traded, so POSITIVE means the rule cost money and
    NEGATIVE means the rule earned it. The trailing stop's -$22.09 is a
    rule that helped, and a breakout that could not express that would have
    hidden it.
    """
    rows = _rows([
        # Sold at 0.90 a position that went on to settle WORTHLESS. Nothing
        # can beat par on an eventual winner, so an eventual loser exited
        # high is the only shape where a price exit earns against holding --
        # which is what the trailing stop's 15 rows did.
        _position(
            status="closed_trailing_stop",
            exit_price=0.90,
            bucket_c=WINNING_BUCKET + 1,
        )
    ])
    cost = cohort_monitor.summarize(rows)["by_status"]["closed_trailing_stop"]["cost_usd"]
    assert cost < 0
