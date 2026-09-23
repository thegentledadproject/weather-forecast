"""
Wave 1 migration: a database whose `positions` table predates the five
deciding-number columns, and which has no `entry_decisions` table, must gain
both from storage.migrate() -- and gain them once.
"""
import sqlite3

import config
import storage

WAVE1_POSITION_COLUMNS = (
    "calibrated_prob", "calibration_source", "admission_edge",
    "sizing_edge", "kelly_size_preclamp_usd",
)

PRE_WAVE1_POSITIONS_DDL = """
CREATE TABLE positions (
    position_id TEXT PRIMARY KEY, station_icao TEXT NOT NULL, target_date TEXT NOT NULL,
    bucket_c INTEGER NOT NULL, side TEXT NOT NULL, entry_price REAL NOT NULL,
    size_usd REAL NOT NULL, entry_time TEXT NOT NULL, status TEXT NOT NULL,
    high_water_mark REAL NOT NULL, exit_price REAL, exit_time TEXT, exit_reason TEXT,
    token_id TEXT, is_paper INTEGER NOT NULL DEFAULT 0, size_shares REAL,
    execution_mode TEXT NOT NULL DEFAULT 'paper', order_id TEXT, model_prob REAL,
    raw_edge REAL, net_ev_at_size REAL, entry_bid REAL, exit_blocked_reason TEXT,
    entry_fee_per_share REAL, trigger_price REAL
)
"""


def _pre_wave1_db(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(path)
    con.execute(PRE_WAVE1_POSITIONS_DDL)
    con.execute(
        "INSERT INTO positions (position_id, station_icao, target_date, bucket_c, side, "
        "entry_price, size_usd, entry_time, status, high_water_mark, is_paper) "
        "VALUES ('old-1','WSSS','2026-09-01',32,'YES',0.3,10.0,'2026-09-01T00:00:00+00:00','open',0.3,1)"
    )
    con.commit()
    con.close()
    return str(path)


def _columns(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _tables(db_path):
    con = sqlite3.connect(db_path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_migrate_adds_the_five_columns_and_the_table(tmp_path, monkeypatch):
    db = _pre_wave1_db(tmp_path)
    assert "entry_decisions" not in _tables(db)
    monkeypatch.setattr(config, "DB_PATH", db)

    storage.migrate()

    cols = _columns(db, "positions")
    for name in WAVE1_POSITION_COLUMNS:
        assert name in cols
    assert "entry_decisions" in _tables(db)
    assert _columns(db, "entry_decisions") == list(storage.ENTRY_DECISION_COLUMNS)


def test_the_five_columns_come_last_in_declared_order(tmp_path, monkeypatch):
    """_row_to_position reads SELECT * positionally, so order is load-bearing."""
    db = _pre_wave1_db(tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db)
    storage.migrate()
    assert tuple(_columns(db, "positions")[-5:]) == WAVE1_POSITION_COLUMNS


def test_a_fresh_database_declares_the_same_order(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fresh.sqlite3"))
    storage.migrate()
    assert tuple(_columns(config.DB_PATH, "positions")[-5:]) == WAVE1_POSITION_COLUMNS


def test_the_migration_is_idempotent_and_keeps_old_rows_null(tmp_path, monkeypatch):
    db = _pre_wave1_db(tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db)
    storage.migrate()
    storage.migrate()
    storage.load_open_positions("WSSS")

    assert _columns(db, "positions").count("calibrated_prob") == 1
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT calibrated_prob, calibration_source, admission_edge, sizing_edge, "
        "kelly_size_preclamp_usd FROM positions WHERE position_id='old-1'"
    ).fetchone()
    con.close()
    assert row == (None, None, None, None, None)


def test_entry_decisions_has_the_pairing_index(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fresh.sqlite3"))
    storage.migrate()
    con = sqlite3.connect(config.DB_PATH)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    con.close()
    assert {"ix_ed_cycle", "ix_ed_pair"} <= names
