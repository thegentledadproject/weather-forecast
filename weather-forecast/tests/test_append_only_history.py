"""
Gap 8, part A: history can no longer be silently overwritten.

observations, settled_buckets and ensemble_spread are written with
INSERT OR REPLACE, so a re-ingest used to erase the previous value with no
trace. migrate() now gives each a `<table>_history` twin fed by AFTER INSERT
and AFTER UPDATE triggers, seeded once from the current rows (history_op='backfill').
The live tables and every reader of them are unchanged.

ev_snapshots is deliberately NOT covered: generated_at is in its primary key,
so it is already append-only in practice, and a history twin would double
the largest table (~24k rows/day) on a box with ~2GB free.
"""
import sqlite3
from datetime import date, datetime, timezone

import config
import storage

HISTORY_TABLES = ("observations", "settled_buckets", "ensemble_spread")


def _rows(sql, *params):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _obs(value, source="metar_daily_max"):
    storage.save_observation(storage.ObservedReading(
        station_icao="WSSS", target_date=date(2026, 9, 20),
        max_temp_c=value, source=source,
    ))


def test_history_tables_exist_and_ev_snapshots_is_excluded(tmp_db):
    names = {r[0] for r in _rows("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in HISTORY_TABLES:
        assert f"{t}_history" in names
    assert "ev_snapshots_history" not in names


def test_observation_overwrite_keeps_every_revision(tmp_db):
    _obs(31.0)
    _obs(32.0)

    assert _rows("SELECT max_temp_c FROM observations") == [(32.0,)]
    hist = _rows(
        "SELECT history_op, station_icao, target_date, max_temp_c, source "
        "FROM observations_history ORDER BY history_id"
    )
    assert hist == [
        ("insert", "WSSS", "2026-09-20", 31.0, "metar_daily_max"),
        ("insert", "WSSS", "2026-09-20", 32.0, "metar_daily_max"),
    ]


def test_plain_update_is_recorded(tmp_db):
    _obs(31.0)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE observations SET max_temp_c = 30.5")
    conn.commit()
    conn.close()
    assert _rows("SELECT history_op, max_temp_c FROM observations_history ORDER BY history_id") == [
        ("insert", 31.0), ("update", 30.5),
    ]


def test_settlement_and_ensemble_overwrites_keep_every_revision(tmp_db):
    for bucket in (30, 31):
        storage.save_settled_bucket("WSSS", date(2026, 9, 20), bucket, 25, 35, "gamma")
    for sd in (0.5, 0.6):
        storage.save_ensemble_spread("WSSS", date(2026, 9, 20), sd, 51, "2026-09-20T05:00:00+00:00")

    assert _rows("SELECT bucket_c FROM settled_buckets_history ORDER BY history_id") == [(30,), (31,)]
    assert _rows("SELECT bucket_unit, bucket_step FROM settled_buckets_history") == [("C", 1)] * 2
    assert _rows("SELECT std_dev_c FROM ensemble_spread_history ORDER BY history_id") == [(0.5,), (0.6,)]


def test_recorded_at_is_utc_iso_and_sorts_with_python_timestamps(tmp_db):
    before = datetime.now(timezone.utc).isoformat()
    _obs(31.0)
    (recorded_at,) = _rows("SELECT history_recorded_at FROM observations_history")[0]
    parsed = datetime.fromisoformat(recorded_at)
    assert parsed.utcoffset().total_seconds() == 0
    assert len(recorded_at) == len(before)  # same shape as the repo's isoformat()
    # millisecond precision: truncate `before` to ms before comparing
    assert recorded_at >= before[:23] + "000+00:00"


def test_backfill_seeds_existing_rows_once(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE observations (station_icao TEXT NOT NULL, target_date TEXT NOT NULL, "
        "max_temp_c REAL NOT NULL, source TEXT NOT NULL, "
        "PRIMARY KEY (station_icao, target_date, source))"
    )
    conn.execute("INSERT INTO observations VALUES ('WSSS','2026-09-01',31.0,'metar_daily_max')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))

    storage.migrate()
    storage.migrate()

    assert _rows("SELECT history_op, station_icao, max_temp_c FROM observations_history") == [
        ("backfill", "WSSS", 31.0),
    ]


def test_second_migrate_writes_no_schema(tmp_db):
    (v1,) = _rows("PRAGMA schema_version")[0]
    storage.migrate()
    (v2,) = _rows("PRAGMA schema_version")[0]
    assert v1 == v2


def test_a_column_added_later_is_carried_into_history(tmp_db):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("ALTER TABLE ensemble_spread ADD COLUMN model TEXT")
    conn.commit()
    conn.close()
    storage.migrate()

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO ensemble_spread VALUES ('WSSS','2026-09-20',0.5,51,'t','ecmwf')"
    )
    conn.commit()
    conn.close()
    assert _rows("SELECT model FROM ensemble_spread_history") == [("ecmwf",)]
