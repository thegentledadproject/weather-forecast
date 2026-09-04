"""
tests/test_entry_fee_migration.py

P1-8(b) · symmetrise the entry fee, on the storage side.

THE ASYMMETRY. exit_price is stored NET of the exit taker fee --
position_manager and the backtest engine both subtract it before writing --
while entry_price is stored GROSS. So every recorded return is flattered by
the entry leg, and this paper record is the only forward validation the
system has.

HOW BIG, measured 2026-09-04 over the published 2026-08-03..09-01 window
(514 rows, $4,049.93 staked): **$104.99, or 2.59% of stake**. The plan
estimated "roughly 0.5-1.25% of stake per round trip"; the entry leg ALONE is
about double the top of that range, because the fee is 0.05 x (1 - p) of
notional and this book's mean entry is 0.32. Held-to-settlement drops from
+18.4% to +15.8% -- the edge survives, and the number was wrong.

WHY THE FEE IS STORED RATHER THAN RECOMPUTED. It is a pure function of
entry_price today, so storing it looks redundant. It is not, for the same
reason storage.py refuses to backfill model_prob: THE FEE SCHEDULE IS A FACT
ABOUT THE DAY THE TRADE HAPPENED. Polymarket can change the rate, and a
consumer that recomputes would then restate every historical row under a rate
that was never charged. The stored column is the record; recomputation is a
fallback for rows that never had one, and callers are told when it was used.

WHAT MUST NOT CHANGE. entry_price itself, size_usd and size_shares are
untouched -- rewriting entry_price would change the meaning of every
historical row, and of the risk unit, the stop basis and the Kelly size
derived from it. The acceptance condition is exactly that:
`size_usd == entry_price * size_shares` still holds after the backfill.
"""
import sqlite3
from datetime import date

import pytest

import config
import risk_manager
import storage
from models import Position


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    return str(tmp_path / "t.sqlite3")


def _position(entry_price=0.30, size_usd=9.0, size_shares=30.0, pid="p1") -> Position:
    return Position(
        position_id=pid,
        station_icao="WSSS",
        target_date=date(2026, 9, 3),
        bucket_c=32,
        side="YES",
        entry_price=entry_price,
        size_usd=size_usd,
        entry_time="2026-09-03T02:00:00+00:00",
        status="open",
        high_water_mark=entry_price,
        size_shares=size_shares,
    )


def _columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
    finally:
        conn.close()


def _raw(db_path, pid="p1"):
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM positions WHERE position_id = ?", (pid,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The column and the migration
# ---------------------------------------------------------------------------

def test_the_column_exists_on_a_fresh_database(db):
    storage.open_position(_position())
    assert "entry_fee_per_share" in _columns(db)


def test_the_migration_is_idempotent(db):
    """
    _connect() runs the migration on EVERY connection, so "runs twice" is the
    normal case, not an edge case.
    """
    storage.open_position(_position())
    first = _raw(db)["entry_fee_per_share"]
    storage.load_position_history("WSSS")
    storage.load_open_positions("WSSS")
    assert _raw(db)["entry_fee_per_share"] == first


def test_a_row_written_before_the_column_existed_is_backfilled(db):
    """
    The migration case: a deployed database whose positions table predates
    this column. ALTER TABLE gives every existing row NULL, and the backfill
    is what turns that into the fee actually charged.
    """
    storage.open_position(_position())
    conn = sqlite3.connect(db)
    conn.execute("UPDATE positions SET entry_fee_per_share = NULL")
    conn.commit()
    conn.close()

    storage.load_position_history("WSSS")  # any connection runs the migration

    expected = risk_manager.taker_fee_per_share(0.30)
    assert _raw(db)["entry_fee_per_share"] == pytest.approx(expected)


def test_the_backfill_leaves_the_size_identity_intact(db):
    """
    THE ACCEPTANCE CONDITION. Rewriting entry_price would change the meaning
    of every historical row -- and of the risk unit, the stop basis and the
    Kelly size derived from it. Nothing but the new column may move.
    """
    storage.open_position(_position(entry_price=0.30, size_usd=9.0, size_shares=30.0))
    before = _raw(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE positions SET entry_fee_per_share = NULL")
    conn.commit()
    conn.close()

    storage.load_position_history("WSSS")
    after = _raw(db)

    assert after["entry_price"] == before["entry_price"]
    assert after["size_usd"] == before["size_usd"]
    assert after["size_shares"] == before["size_shares"]
    assert after["size_usd"] == pytest.approx(after["entry_price"] * after["size_shares"])


def test_the_backfill_does_not_overwrite_a_stored_value(db):
    """
    A recorded fee is what was CHARGED. If the schedule ever changes, a
    backfill that overwrote stored values would restate history under a rate
    nobody paid -- the exact failure storage.py's model_prob note warns about.
    """
    storage.open_position(_position())
    conn = sqlite3.connect(db)
    conn.execute("UPDATE positions SET entry_fee_per_share = 0.99")
    conn.commit()
    conn.close()

    storage.load_position_history("WSSS")

    assert _raw(db)["entry_fee_per_share"] == 0.99


def test_a_new_position_carries_the_fee_at_insert(db):
    """
    Backfill covers history; the insert covers everything after. Relying on
    the backfill for new rows would work, but it would mean the ledger is
    briefly wrong on every write and correct only on the next connection.
    """
    storage.open_position(_position(entry_price=0.40))

    assert _raw(db)["entry_fee_per_share"] == pytest.approx(
        risk_manager.taker_fee_per_share(0.40)
    )


def test_the_fee_is_read_back_onto_the_position(db):
    storage.open_position(_position())
    loaded = storage.load_open_positions("WSSS")[0]

    assert loaded.entry_fee_per_share == pytest.approx(
        risk_manager.taker_fee_per_share(0.30)
    )


def test_the_stored_fee_is_returned_verbatim_not_recomputed(db):
    """
    The point of the column. A value that disagrees with today's schedule must
    survive a read, because it is the record of what was charged.
    """
    storage.open_position(_position())
    conn = sqlite3.connect(db)
    conn.execute("UPDATE positions SET entry_fee_per_share = 0.123")
    conn.commit()
    conn.close()

    assert storage.load_open_positions("WSSS")[0].entry_fee_per_share == 0.123


# ---------------------------------------------------------------------------
# Reporting on the net basis
# ---------------------------------------------------------------------------

def test_the_economics_view_exposes_the_entry_fee_in_dollars(db):
    storage.open_position(_position(entry_price=0.30, size_usd=9.0, size_shares=30.0))
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM position_economics").fetchone())
    finally:
        conn.close()

    # 30 notional shares at 0.05 x 0.70 x 0.30 = $0.0105 each.
    assert row["entry_fee_usd"] == pytest.approx(30.0 * risk_manager.taker_fee_per_share(0.30))


def test_the_economics_view_reports_gross_and_net_side_by_side(db):
    """
    BOTH, not one. The gross figure is what the ledger says and what every
    published measurement to date was computed on; the net figure is what the
    trade actually returned. Replacing gross would silently invalidate the
    cohort monitor's reproduction of the 2026-09-02 totals.
    """
    storage.open_position(_position(entry_price=0.30, size_usd=9.0, size_shares=30.0))
    storage.close_position("p1", exit_price=0.45, exit_time="x",
                           status="closed_take_profit", reason="take_profit")

    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM position_economics").fetchone())
    finally:
        conn.close()

    assert row["realized_pnl_usd"] == pytest.approx((0.45 - 0.30) * 30.0)
    assert row["realized_pnl_usd_net"] == pytest.approx(
        row["realized_pnl_usd"] - row["entry_fee_usd"]
    )
    assert row["realized_pnl_usd_net"] < row["realized_pnl_usd"]


def test_a_row_with_no_recorded_fee_reports_a_null_net_rather_than_the_gross(db):
    """
    "Fee unknown" and "fee zero" are different facts, and only one of them
    means the net return equals the gross one. NULL propagates rather than
    quietly reporting the flattered number as if it were net.
    """
    storage.open_position(_position())
    storage.close_position("p1", exit_price=0.45, exit_time="x",
                           status="closed_take_profit", reason="take_profit")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE positions SET entry_fee_per_share = NULL")
    conn.commit()
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM position_economics").fetchone())
    conn.close()

    assert row["realized_pnl_usd"] is not None
    assert row["realized_pnl_usd_net"] is None


# ---------------------------------------------------------------------------
# The cohort monitor and the paper report
#
# BOTH BASES, SIDE BY SIDE, and the reproduction check stays on the gross one.
# The published 2026-09-02 totals were computed gross, so a monitor that
# quietly redefined its scenario P&L as net would stop reproducing them --
# and would report a MISMATCH that is really an agreement about different
# quantities. That failure would look exactly like a real defect.
# ---------------------------------------------------------------------------

def _cohort_rows(entry_price=0.30, exit_price=0.45, size_usd=9.0, fee=None, n=1):
    import cohort_monitor

    settled = {date(2026, 9, 3): (32, 30, 34, "src", 1)}
    positions = []
    for i in range(n):
        p = _position(entry_price=entry_price, size_usd=size_usd, pid=f"p{i}")
        p.status = "closed_take_profit"
        p.exit_price = exit_price
        p.entry_fee_per_share = fee
        positions.append(p)
    rows, _ = cohort_monitor.cohort_rows(positions, settled)
    return rows


def test_the_cohort_monitor_reports_the_entry_fee_it_never_charged():
    import cohort_monitor

    fee = risk_manager.taker_fee_per_share(0.30)
    summary = cohort_monitor.summarize(_cohort_rows(fee=fee))

    # 9.0 / 0.30 = 30 notional shares.
    assert summary["entry_fee_usd"] == pytest.approx(30.0 * fee)


def test_every_scenario_carries_a_net_figure_alongside_its_gross_one():
    import cohort_monitor

    fee = risk_manager.taker_fee_per_share(0.30)
    summary = cohort_monitor.summarize(_cohort_rows(fee=fee))

    for name in cohort_monitor.SCENARIOS:
        cell = summary["scenarios"][name]
        assert cell["pnl_usd_net"] == pytest.approx(
            cell["pnl_usd"] - summary["entry_fee_usd"]
        )
        assert cell["pnl_usd_net"] < cell["pnl_usd"]


def test_the_reproduction_check_stays_on_the_gross_basis():
    """
    THE INTERACTION THAT WOULD HAVE BROKEN QUIETLY. The published totals are
    gross. Scoring them against a net basis would report a mismatch that is
    really two correct numbers answering different questions -- and it would
    look identical to a real regression.
    """
    import cohort_monitor

    fee = risk_manager.taker_fee_per_share(0.30)
    with_fee = cohort_monitor.reproduction_check(
        cohort_monitor.summarize(_cohort_rows(fee=fee))
    )
    without_fee = cohort_monitor.reproduction_check(
        cohort_monitor.summarize(_cohort_rows(fee=None))
    )

    for name in cohort_monitor.PUBLISHED_TOTALS_USD:
        assert with_fee["by_scenario"][name]["measured"] == pytest.approx(
            without_fee["by_scenario"][name]["measured"]
        )


def test_a_row_with_no_recorded_fee_is_counted_rather_than_guessed_at():
    """
    The monitor falls back to today's schedule when a row has no recorded fee,
    because otherwise one un-backfilled row would void the whole net column.
    But it SAYS how many it estimated -- an estimate presented as a record is
    the thing this column exists to stop.
    """
    import cohort_monitor

    summary = cohort_monitor.summarize(_cohort_rows(fee=None))

    assert summary["n_estimated_entry_fees"] == 1
    assert summary["entry_fee_usd"] == pytest.approx(
        30.0 * risk_manager.taker_fee_per_share(0.30)
    )


def test_a_recorded_fee_is_not_counted_as_estimated():
    import cohort_monitor

    summary = cohort_monitor.summarize(
        _cohort_rows(fee=risk_manager.taker_fee_per_share(0.30))
    )

    assert summary["n_estimated_entry_fees"] == 0


def test_the_paper_report_shows_the_net_return_next_to_the_gross_one(db):
    import paper_trading_report

    fee = risk_manager.taker_fee_per_share(0.30)
    p = _position(entry_price=0.30, size_usd=9.0, size_shares=None)
    p.is_paper = True
    p.entry_fee_per_share = fee
    p.status = "closed_take_profit"
    p.exit_price = 0.45
    summary = paper_trading_report.summarize_positions([p])

    # Rounded to cents, like every other dollar figure this summary reports.
    assert summary["total_entry_fee_usd"] == pytest.approx(30.0 * fee, abs=0.005)
    assert summary["dollar_weighted_return_pct_net"] < summary["dollar_weighted_return_pct"]
