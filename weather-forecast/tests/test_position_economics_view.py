"""
tests/test_position_economics_view.py

Covers the `position_economics` view added 2026-08-17.

WHAT IT IS FOR. size_shares is NULL on every paper/manual_review row and
that is CORRECT -- it means shares actually held on the exchange, and a
paper fill holds none (models.Position.size_shares). Every P&L query
therefore had to hand-roll `coalesce(size_shares, size_usd/entry_price)`.
The view names that derived quantity `notional_shares` instead, and leaves
`size_shares` untouched, so the honest distinction survives.

The distinction is load-bearing: executor.close_position() reads size_shares
to tell an operator the exact share count to sell, so a fabricated value
there would ask someone to sell shares that do not exist. These tests pin
that the view adds a column rather than filling one in.
"""
import sqlite3
from datetime import date

import pytest

import config
import storage
from models import Position


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real, migrated database at a throwaway path."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    return config.DB_PATH


def _pos(pid, mode, size_usd, entry_price, size_shares=None, **kw) -> Position:
    return Position(
        position_id=pid,
        station_icao="WSSS",
        target_date=date(2026, 8, 17),
        bucket_c=32,
        side="YES",
        entry_price=entry_price,
        size_usd=size_usd,
        entry_time="2026-08-17T05:00:00+00:00",
        status=kw.pop("status", "open"),
        token_id="tok",
        is_paper=(mode != "live"),
        size_shares=size_shares,
        execution_mode=mode,
        **kw,
    )


def _rows(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


class TestNotionalSharesIsDerivedNotStored:
    def test_paper_row_gets_notional_but_size_shares_stays_null(self, db):
        storage.open_position(_pos("paper-1", "paper", size_usd=30.0, entry_price=0.40))

        row = _rows(db, "SELECT * FROM position_economics WHERE position_id = ?", ("paper-1",))[0]

        assert row["size_shares"] is None, (
            "the view must not fill in size_shares -- that field means shares "
            "actually held, and close_position() tells an operator to sell it"
        )
        assert row["notional_shares"] == pytest.approx(75.0)

    def test_live_row_keeps_its_real_share_count(self, db):
        """
        A live fill's size_shares is the exchange truth and can differ from
        the notional figure, because the order builder rounds to a share
        grid. The view must not blur the two together.
        """
        storage.open_position(
            _pos("live-1", "live", size_usd=10.0, entry_price=0.33, size_shares=30.0)
        )

        row = _rows(db, "SELECT * FROM position_economics WHERE position_id = ?", ("live-1",))[0]

        assert row["size_shares"] == 30.0
        assert row["notional_shares"] == pytest.approx(10.0 / 0.33)
        assert row["size_shares"] != pytest.approx(row["notional_shares"])


class TestRealizedPnl:
    def test_closed_position_reports_pnl(self, db):
        storage.open_position(_pos("closed-1", "paper", size_usd=30.0, entry_price=0.40))
        storage.close_position(
            position_id="closed-1", exit_price=0.50,
            exit_time="2026-08-17T09:00:00+00:00",
            status="closed_take_profit", reason="take_profit",
        )

        row = _rows(db, "SELECT * FROM position_economics WHERE position_id = ?", ("closed-1",))[0]

        # 75 notional shares, +0.10/share
        assert row["realized_pnl_usd"] == pytest.approx(7.5)
        assert row["realized_pnl_pct"] == pytest.approx(0.25)

    def test_open_position_has_null_pnl(self, db):
        storage.open_position(_pos("open-1", "paper", size_usd=30.0, entry_price=0.40))

        row = _rows(db, "SELECT * FROM position_economics WHERE position_id = ?", ("open-1",))[0]

        assert row["realized_pnl_usd"] is None
        assert row["realized_pnl_pct"] is None


class TestViewDefinitionStaysCurrent:
    def test_a_drifted_view_is_rebuilt(self, db):
        """
        The reason this is not CREATE VIEW IF NOT EXISTS: an existing
        database must pick up an edited definition, not keep the old one
        forever.
        """
        storage.open_position(_pos("p", "paper", size_usd=1.0, entry_price=0.10))

        conn = sqlite3.connect(db)
        conn.execute("DROP VIEW position_economics")
        conn.execute("CREATE VIEW position_economics AS SELECT position_id FROM positions")
        conn.commit()
        conn.close()

        # Any connection through storage should notice and repair it.
        storage.load_open_positions()

        row = _rows(db, "SELECT * FROM position_economics WHERE position_id = ?", ("p",))[0]
        assert "notional_shares" in row, "the stale one-column view was not rebuilt"

    def test_repeated_connections_do_not_churn_the_schema(self, db):
        """
        This runs on every connection, and storage opens one per call. A
        rebuild each time would take a write lock and bump the schema
        cookie for a view holding no data.
        """
        storage.open_position(_pos("p", "paper", size_usd=1.0, entry_price=0.10))

        def cookie():
            conn = sqlite3.connect(db)
            try:
                return conn.execute("PRAGMA schema_version").fetchone()[0]
            finally:
                conn.close()

        before = cookie()
        for _ in range(5):
            storage.load_open_positions()

        assert cookie() == before, "the view is being dropped and recreated on every connection"
