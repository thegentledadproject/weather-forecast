"""
Wave 3 item 3a. The schema is applied by ONE explicit call, storage.migrate()
-- run by the daemon at boot (scheduler._boot_storage) and by
deploy/deploy_daemon.sh with the daemon stopped -- and by nothing else.
storage._connect() opens; it never issues DDL. A process that has not called
storage.set_writable(True) opens the file mode=ro, and a write from it is a
StorageReadOnlyError that names the process, not a silent schema write from
a root-owned dashboard timer (the 3b half of the same defect).

The suite is the exception that proves the rule: conftest sets _WRITABLE for
the test process (tests are the operator of their throwaway files) and the
read-only tests below flip it back explicitly.
"""
import pathlib
import sqlite3
import sys
from datetime import date

import pytest

import config
import storage
from models import ObservedReading, PointForecast, Position

PKG = pathlib.Path(__file__).resolve().parents[1]

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


def _tables(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _columns(path, table):
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def test_connect_creates_nothing(tmp_path, monkeypatch):
    """THE CHANGE. A bare open leaves an empty file: no tables, no view."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fresh.sqlite3"))
    storage._connect().close()
    assert _tables(config.DB_PATH) == set()


def test_migrate_builds_the_whole_schema_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fresh.sqlite3"))
    storage.migrate()
    assert {"forecasts", "observations", "positions", "live_order_attempts",
            "entry_decisions", "settled_buckets", "ensemble_spread", "ev_snapshots"} <= _tables(config.DB_PATH)
    # 8 evidence tables + 3 gap-8 `_history` twins
    assert "11 tables" in storage.schema_summary() and "position_economics" in storage.schema_summary()


def test_migrate_on_a_pre_wave1_schema_adds_columns_and_tables_idempotently(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.sqlite3")
    con = sqlite3.connect(path)
    con.execute(PRE_WAVE1_POSITIONS_DDL)
    con.execute(
        "INSERT INTO positions (position_id, station_icao, target_date, bucket_c, side, "
        "entry_price, size_usd, entry_time, status, high_water_mark, is_paper) "
        "VALUES ('old-1','WSSS','2026-09-01',32,'YES',0.3,10.0,'2026-09-01T00:00:00+00:00','open',0.3,1)"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(config, "DB_PATH", path)

    storage.migrate()
    storage.migrate()

    cols = _columns(path, "positions")
    assert cols[-5:] == ["calibrated_prob", "calibration_source", "admission_edge",
                         "sizing_edge", "kelly_size_preclamp_usd"]
    assert cols.count("calibrated_prob") == 1
    assert "entry_decisions" in _tables(path)
    # The backfill ran (entry fee) and the deciding numbers stayed NULL.
    con = sqlite3.connect(path)
    fee, cal = con.execute("SELECT entry_fee_per_share, calibrated_prob FROM positions").fetchone()
    con.close()
    assert fee == pytest.approx(0.05 * 0.7 * 0.3) and cal is None
    (loaded,) = storage.load_open_positions("WSSS")
    assert loaded.position_id == "old-1"


def test_a_read_only_process_reads_but_cannot_write(tmp_db, monkeypatch):
    storage.save_forecast(PointForecast(
        station_icao="WSSS", source="a", target_date=date(2026, 9, 19),
        max_temp_c=32.0, fetched_at="2026-09-18T21:00:00+00:00",
    ))
    monkeypatch.setattr(storage, "_WRITABLE", False)
    monkeypatch.setattr(sys, "argv", ["cohort_monitor.py"])

    assert not storage.is_writable()
    assert len(storage.load_forecast_history("WSSS", "a")) == 1        # reads work
    with pytest.raises(storage.StorageReadOnlyError) as exc:
        storage.save_observation(ObservedReading(
            station_icao="WSSS", target_date=date(2026, 9, 19), max_temp_c=32.0, source="metar_daily_max"))
    assert "cohort_monitor.py" in str(exc.value)
    assert "set_writable" in str(exc.value)


def test_a_read_only_process_gets_a_clear_error_when_the_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "absent.sqlite3"))
    monkeypatch.setattr(storage, "_WRITABLE", False)
    with pytest.raises(storage.StorageReadOnlyError) as exc:
        storage.load_open_positions()
    assert "absent.sqlite3" in str(exc.value) and "migrate" in str(exc.value)
    assert not (tmp_path / "absent.sqlite3").exists()   # ro never creates the file


def test_set_writable_is_the_only_way_in(tmp_db, monkeypatch):
    monkeypatch.setattr(storage, "_WRITABLE", False)
    storage.set_writable(True)
    assert storage.is_writable()
    storage.open_position(Position(
        position_id="w", station_icao="WSSS", target_date=date(2026, 9, 19), bucket_c=32, side="YES",
        entry_price=0.3, size_usd=1.0, entry_time="2026-09-19T05:00:00+00:00", status="open",
        high_water_mark=0.3, is_paper=True, execution_mode="paper",
    ))
    assert [p.position_id for p in storage.load_open_positions("WSSS")] == ["w"]
    storage.set_writable(False)
    assert not storage.is_writable()


def test_tmp_db_is_migrated_and_is_config_db_path(tmp_db):
    assert tmp_db == str(config.DB_PATH)
    assert "positions" in _tables(tmp_db)


def test_the_suite_never_touches_the_checkout_database():
    """The session fixture in conftest: the default DB_PATH under test is a
    temp file, not data/polyweather.sqlite3 in the checkout."""
    assert str(config.DB_PATH).endswith("polyweather.sqlite3")
    assert not pathlib.Path(config.DB_PATH).resolve().is_relative_to(PKG)
