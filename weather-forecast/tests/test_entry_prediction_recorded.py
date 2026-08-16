"""
The model's prediction is persisted ON the position it opened.

WHY THIS EXISTS
---------------
Until 2026-08-16 the trading DB stored what a trade DID and nothing about
what the model BELIEVED when it opened it. Every entry had cleared an edge
threshold and a positive-net-EV check, but neither number was written down,
so "was the edge real?" could not be asked of a single live trade -- only of
backtest replays, which at WMKK disagreed with the live paper book by 40
percentage points (backtest +30.3% vs paper -10.6% dollar-weighted over 30
closed trades).

WHAT THESE FIELDS ARE AND ARE NOT
----------------------------------
model_prob is P(the side taken wins AT SETTLEMENT). It is NOT a prediction
about exit P&L: 29 of those 30 WMKK trades exited on a stop or take-profit
and never reached resolution, so exit P&L scores the exit rules while these
fields score the forecast. Scoring them against each other is the mistake
this docstring exists to prevent.
"""
from datetime import date

import pytest

import config
import executor
import storage
from models import EntryDecision, Position


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    storage._connect().close()


def _decision(**overrides) -> EntryDecision:
    base = dict(
        station_icao="WSSS", target_date=date(2026, 8, 16), bucket_c=32, side="YES",
        kelly_fraction_raw=0.2, kelly_fraction_applied=0.05,
        recommended_size_usd=10.0, available_depth_usd=200.0,
        slippage_at_size_pct=0.01, net_ev_at_size=0.08,
        approved=True, reason="test", station_maturity="exploratory",
        entry_price=0.30, token_id="tok-1",
        model_prob=0.42, raw_edge=0.12,
    )
    base.update(overrides)
    return EntryDecision(**base)


# --------------------------------------------------------------------------
# Storage round-trip
# --------------------------------------------------------------------------

def test_prediction_survives_a_storage_round_trip():
    storage.open_position(Position(
        position_id="WSSS:2026-08-16:32:YES:t", station_icao="WSSS",
        target_date=date(2026, 8, 16), bucket_c=32, side="YES",
        entry_price=0.30, size_usd=10.0, entry_time="2026-08-16T00:00:00+00:00",
        status="open", is_paper=True,
        model_prob=0.42, raw_edge=0.12, net_ev_at_size=0.08,
    ))

    (loaded,) = storage.load_open_positions("WSSS")

    assert loaded.model_prob == pytest.approx(0.42)
    assert loaded.raw_edge == pytest.approx(0.12)
    assert loaded.net_ev_at_size == pytest.approx(0.08)


def test_a_position_with_no_model_behind_it_stores_none_not_zero():
    """
    manual_trigger asserts a trade with the model gates bypassed. None means
    "no model ran"; 0.0 would mean "the model said zero edge". Collapsing the
    two would let a hand-placed trade be scored as a model prediction.
    """
    storage.open_position(Position(
        position_id="WSSS:2026-08-16:33:NO:t", station_icao="WSSS",
        target_date=date(2026, 8, 16), bucket_c=33, side="NO",
        entry_price=0.30, size_usd=10.0, entry_time="2026-08-16T00:00:00+00:00",
        status="open", is_paper=True,
    ))

    (loaded,) = storage.load_open_positions("WSSS")

    assert loaded.model_prob is None
    assert loaded.raw_edge is None
    assert loaded.net_ev_at_size is None


# --------------------------------------------------------------------------
# Migration of a database written before these columns existed
# --------------------------------------------------------------------------

def test_migration_adds_the_columns_and_old_rows_read_none(tmp_path, monkeypatch):
    """
    A deployed DB predates these columns. The migration must add them, and
    the rows already there must read None -- those trades genuinely have no
    stored prediction. Backfilling by recomputing would be lookahead: today's
    calibration has already seen their outcomes.
    """
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE positions (
            position_id TEXT PRIMARY KEY, station_icao TEXT NOT NULL,
            target_date TEXT NOT NULL, bucket_c INTEGER NOT NULL, side TEXT NOT NULL,
            entry_price REAL NOT NULL, size_usd REAL NOT NULL, entry_time TEXT NOT NULL,
            status TEXT NOT NULL, high_water_mark REAL NOT NULL, exit_price REAL,
            exit_time TEXT, exit_reason TEXT, token_id TEXT,
            is_paper INTEGER NOT NULL DEFAULT 0, size_shares REAL,
            execution_mode TEXT NOT NULL DEFAULT 'paper', order_id TEXT
        )
        """
    )
    con.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old-1", "WSSS", "2026-08-01", 32, "YES", 0.3, 10.0,
         "2026-08-01T00:00:00+00:00", "open", 0.3, None, None, "", None, 1,
         None, "paper", None),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(config, "DB_PATH", str(db))
    storage._connect().close()

    (loaded,) = storage.load_open_positions("WSSS")
    assert loaded.position_id == "old-1"
    assert loaded.model_prob is None and loaded.raw_edge is None
    assert loaded.net_ev_at_size is None

    # And the migrated table now accepts a row that carries a prediction.
    storage.open_position(Position(
        position_id="new-1", station_icao="WSSS", target_date=date(2026, 8, 16),
        bucket_c=32, side="YES", entry_price=0.3, size_usd=10.0,
        entry_time="2026-08-16T00:00:00+00:00", status="open", is_paper=True,
        model_prob=0.55, raw_edge=0.25, net_ev_at_size=0.4,
    ))
    fresh = {p.position_id: p for p in storage.load_open_positions("WSSS")}
    assert fresh["new-1"].model_prob == pytest.approx(0.55)


# --------------------------------------------------------------------------
# The executor is what actually copies decision -> position
# --------------------------------------------------------------------------

def test_executor_records_the_decisions_prediction(monkeypatch):
    monkeypatch.setitem(executor.EXECUTION_MODE, "WSSS", "paper")

    executor.open_position(_decision())

    (stored,) = storage.load_open_positions("WSSS")
    assert stored.model_prob == pytest.approx(0.42)
    assert stored.raw_edge == pytest.approx(0.12)
    assert stored.net_ev_at_size == pytest.approx(0.08)


def test_executor_passes_none_through_rather_than_substituting_zero(monkeypatch):
    """A manual_trigger-style decision carries no model numbers."""
    monkeypatch.setitem(executor.EXECUTION_MODE, "WSSS", "paper")

    executor.open_position(_decision(model_prob=None, raw_edge=None, net_ev_at_size=None))

    (stored,) = storage.load_open_positions("WSSS")
    assert stored.model_prob is None
    assert stored.raw_edge is None
    assert stored.net_ev_at_size is None


# --------------------------------------------------------------------------
# The first hop: EVResult -> EntryDecision
# --------------------------------------------------------------------------

def test_evaluate_entry_carries_the_ev_results_numbers(monkeypatch):
    """
    The chain is EVResult -> EntryDecision -> Position, and it is only worth
    as much as its weakest hop. model_prob comes off the EVResult ALREADY
    side-adjusted (P(the side taken wins)), so it must be copied, not
    recomputed or flipped here.
    """
    import entry_manager
    from clients import market_client
    from models import EVResult

    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    monkeypatch.setitem(executor.EXECUTION_MODE, "WSSS", "paper")

    ev = EVResult(
        station_icao="WSSS", target_date=date(2026, 8, 16), bucket_c=32, side="YES",
        model_prob=0.45, market_price=0.35, raw_edge=0.10,
        estimated_slippage_pct=0.01, fee_rate_pct=0.0,
        net_ev_per_dollar=(0.10 / 0.35) - 0.01, spread_source="ensemble",
    )

    decision = entry_manager.evaluate_entry(ev, "TOK", min_net_ev=0.15)

    assert decision.approved
    assert decision.model_prob == pytest.approx(0.45)
    assert decision.raw_edge == pytest.approx(0.10)
