"""
Capturing both sides of the book into price_snapshots.

An entry pays the ASK. The store only ever recorded one price per token,
and that price was the bid, so every replay entered at the bid and
overstated raw edge by the spread -- the same defect fixed on the live path
on 2026-08-10, still present in the backtest afterwards.

The fix adds an ask_price column rather than repurposing the existing one.
That distinction is the thing most worth pinning: switching `price` to the
ask would have been a one-line change that silently made every row written
before the change incomparable to every row after, with nothing in the data
to say which was which.
"""

import sqlite3

import pytest

import backtest.price_store as price_store
from backtest import engine


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "market.sqlite3")


def test_both_sides_round_trip(db):
    price_store.save_snapshot(
        token_id="tok", ts=1000, price=0.29, ask_price=0.31, depth_usd=500.0,
        source=price_store.LIVE_SNAPSHOT_SOURCE, fidelity_min=5, db_path=db,
    )
    row = price_store.get_price_at("tok", 1000, db_path=db)

    assert row["price"] == 0.29        # bid -- what a sale receives
    assert row["ask_price"] == 0.31    # ask -- what an entry pays


def test_ask_is_optional_and_reads_back_as_none(db):
    """
    Not every source has an ask. CLOB's history endpoint returns a single
    traded-price series with no book behind it, so price_history_client
    genuinely has nothing to record.
    """
    price_store.save_snapshot(
        token_id="tok", ts=1000, price=0.29, depth_usd=None,
        source="clob_history", fidelity_min=5, db_path=db,
    )
    assert price_store.get_price_at("tok", 1000, db_path=db)["ask_price"] is None


def test_save_snapshots_accepts_both_arities(db):
    n = price_store.save_snapshots(
        [("a", 10, 0.20, None), ("b", 10, 0.30, 100.0, 0.32)],
        source=price_store.LIVE_SNAPSHOT_SOURCE, fidelity_min=5, db_path=db,
    )
    assert n == 2
    assert price_store.get_price_at("a", 10, db_path=db)["ask_price"] is None
    assert price_store.get_price_at("b", 10, db_path=db)["ask_price"] == 0.32


def test_save_snapshots_rejects_a_wrong_shape(db):
    with pytest.raises(ValueError, match="4- or 5-tuples"):
        price_store.save_snapshots(
            [("a", 10, 0.20)], source="x", fidelity_min=5, db_path=db,
        )


def test_existing_database_is_migrated_in_place(tmp_path):
    """
    THE case that matters in production: market_data.sqlite3 already exists
    on the box with 280k rows and the old six-column schema. CREATE TABLE IF
    NOT EXISTS does nothing to a table that already exists, so without an
    explicit ALTER every read of the new field fails on the real database
    while passing on a fresh one.
    """
    db = str(tmp_path / "legacy.sqlite3")
    legacy = sqlite3.connect(db)
    legacy.execute(
        """
        CREATE TABLE price_snapshots (
            token_id TEXT NOT NULL, ts INTEGER NOT NULL, price REAL NOT NULL,
            depth_usd REAL, source TEXT NOT NULL, fidelity_min INTEGER NOT NULL,
            PRIMARY KEY (token_id, ts, source)
        )
        """
    )
    legacy.execute(
        "INSERT INTO price_snapshots VALUES ('old-tok', 500, 0.44, 250.0, 'live_snapshot', 5)"
    )
    legacy.commit()
    legacy.close()

    # First touch through price_store must add the column, not blow up.
    row = price_store.get_price_at("old-tok", 500, db_path=db)

    assert row["price"] == 0.44
    assert row["ask_price"] is None, "a pre-migration row has no ask, and must say so"

    # ...and the migrated database still accepts new two-sided writes.
    price_store.save_snapshot(
        token_id="new-tok", ts=600, price=0.29, ask_price=0.31, depth_usd=None,
        source="live_snapshot", fidelity_min=5, db_path=db,
    )
    assert price_store.get_price_at("new-tok", 600, db_path=db)["ask_price"] == 0.31


def test_migration_is_idempotent(tmp_path):
    db = str(tmp_path / "m.sqlite3")
    for _ in range(3):
        price_store.save_snapshot(
            token_id="t", ts=1, price=0.5, ask_price=0.52, depth_usd=None,
            source="live_snapshot", fidelity_min=5, db_path=db,
        )
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(price_snapshots)")]
    conn.close()
    assert cols.count("ask_price") == 1


# --------------------------------------------------------------------------
# The engine's use of it
# --------------------------------------------------------------------------

def test_engine_prices_entries_off_the_ask(db):
    price_store.save_snapshot(
        token_id="tok", ts=1000, price=0.29, ask_price=0.31, depth_usd=None,
        source="live_snapshot", fidelity_min=5, db_path=db,
    )
    reader = engine._PriceReader(db_path=db)

    assert reader.entry_price("tok", 1000) == 0.31   # entry pays the ask
    assert reader.price("tok", 1000) == 0.29         # exits/marks use the bid
    assert reader.n_entry_priced_ask == 1
    assert reader.n_entry_priced_bid_fallback == 0


def test_engine_falls_back_to_the_bid_on_pre_migration_rows(db):
    price_store.save_snapshot(
        token_id="tok", ts=1000, price=0.29, depth_usd=None,
        source="live_snapshot", fidelity_min=5, db_path=db,
    )
    reader = engine._PriceReader(db_path=db)

    assert reader.entry_price("tok", 1000) == 0.29
    assert reader.n_entry_priced_bid_fallback == 1
    assert reader.n_entry_priced_ask == 0


def test_a_mixed_run_is_counted_as_mixed(db):
    """
    A replay spanning 2026-08-10 has ask-priced and bid-priced entries in
    one set of numbers. The counts are what let provenance label it, rather
    than reporting a blended edge figure that looks like a clean one.
    """
    price_store.save_snapshot(
        token_id="new", ts=1000, price=0.29, ask_price=0.31, depth_usd=None,
        source="live_snapshot", fidelity_min=5, db_path=db,
    )
    price_store.save_snapshot(
        token_id="old", ts=1000, price=0.40, depth_usd=None,
        source="live_snapshot", fidelity_min=5, db_path=db,
    )
    reader = engine._PriceReader(db_path=db)
    reader.entry_price("new", 1000)
    reader.entry_price("old", 1000)

    assert reader.n_entry_priced_ask == 1
    assert reader.n_entry_priced_bid_fallback == 1


def test_missing_snapshot_prices_nothing_and_counts_nothing(db):
    reader = engine._PriceReader(db_path=db)

    assert reader.entry_price("absent", 1000) is None
    assert reader.n_entry_priced_ask == 0
    assert reader.n_entry_priced_bid_fallback == 0
