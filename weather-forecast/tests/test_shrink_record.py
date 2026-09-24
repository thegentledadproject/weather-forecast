"""
Gap 4, step 2: record what the shrink-toward-ask rule needs before it decides.

  * ev_snapshots rows carry the config_sha they were priced under (the lock
    score's unit filter reads it).
  * every entry_decisions row carries lambda_hat / lambda_se / lambda_days /
    p_robust, copied off the EVResult like the Wave 1 deciding numbers.
  * storage.load_shrink_fit_rows(before) returns the fit set: the first
    entry-window cycle per station-day, YES rows, labelled from
    settled_buckets, target_date strictly before `before`.
"""
import sqlite3
from datetime import date, datetime, timedelta

import pytest

import config
import entry_manager
import ev_engine
import storage
from models import EntryDecision, EVResult

NEW_ED_COLUMNS = ("lambda_hat", "lambda_se", "lambda_days", "p_robust")


def _cols(path, table):
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _ev(bucket=31, side="YES", p=0.4, m=0.3, **kw):
    return EVResult(
        station_icao=kw.pop("station_icao", "WSSS"), target_date=kw.pop("target_date", date(2026, 9, 3)),
        bucket_c=bucket, side=side, model_prob=p, market_price=m,
        raw_edge=None if m is None else p - m, estimated_slippage_pct=0.0,
        fee_rate_pct=0.0, net_ev_per_dollar=None, **kw,
    )


def test_migrate_adds_the_new_columns_to_old_tables_once(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE ev_snapshots (station_icao TEXT NOT NULL, target_date TEXT NOT NULL, "
        "bucket_c INTEGER NOT NULL, side TEXT NOT NULL, generated_at TEXT NOT NULL, model_prob REAL, "
        "market_price REAL, market_bid REAL, raw_edge REAL, slippage_pct REAL, fee_rate_pct REAL, "
        "net_ev_per_dollar REAL, spread_source TEXT, notes TEXT, "
        "PRIMARY KEY (station_icao, target_date, bucket_c, side, generated_at))"
    )
    old_ed = [c for c in storage.ENTRY_DECISION_COLUMNS if c not in NEW_ED_COLUMNS]
    con.execute(f"CREATE TABLE entry_decisions ({', '.join(c + ' TEXT' for c in old_ed)})")
    con.commit()
    con.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))

    storage.migrate()
    storage.migrate()

    assert _cols(path, "ev_snapshots").count("config_sha") == 1
    ed = _cols(path, "entry_decisions")
    for c in NEW_ED_COLUMNS:
        assert ed.count(c) == 1


def test_ev_snapshot_rows_carry_the_config_sha(tmp_db):
    storage.save_ev_snapshot_rows(
        "WSSS", date(2026, 9, 3), "2026-09-03T00:10:00+00:00", [_ev()], config_sha="abc123",
    )
    con = sqlite3.connect(tmp_db)
    assert con.execute("SELECT config_sha FROM ev_snapshots").fetchall() == [("abc123",)]


def test_save_ev_snapshot_stamps_the_running_sha(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "cached_git_sha", lambda: "deadbeef")
    ev_engine.save_ev_snapshot("WSSS", [_ev()])
    con = sqlite3.connect(tmp_db)
    assert con.execute("SELECT config_sha FROM ev_snapshots").fetchall() == [("deadbeef",)]


def test_entry_decision_fields_and_columns_exist():
    for c in NEW_ED_COLUMNS:
        assert c in storage.ENTRY_DECISION_COLUMNS
        assert c in EntryDecision.__dataclass_fields__
        assert c in EVResult.__dataclass_fields__


def test_deciding_numbers_copy_the_shrink_fields_off_the_ev_row():
    ev = _ev(p_robust=0.31, lambda_hat=0.2, lambda_se=0.05, lambda_days=12)
    d = entry_manager.deciding_numbers(ev)
    assert (d["p_robust"], d["lambda_hat"], d["lambda_se"], d["lambda_days"]) == (0.31, 0.2, 0.05, 12)


def test_every_decision_row_persists_the_shrink_fields(tmp_db):
    ev = _ev(p_robust=0.31, lambda_hat=0.2, lambda_se=0.05, lambda_days=12)
    d = entry_manager.collection_only_decision(ev, "tok", "collecting")
    storage.record_entry_decisions([d], book="paper", cycle_ts="2026-09-03T00:00:00+00:00", config_sha="x")
    row = storage.load_entry_decisions()[0]
    assert (row["p_robust"], row["lambda_hat"], row["lambda_se"], row["lambda_days"]) == (0.31, 0.2, 0.05, 12)


# --- the fit-set loader -------------------------------------------------------

def _snap(con, st, td, bucket, gen, p, m, side="YES"):
    con.execute(
        "INSERT INTO ev_snapshots (station_icao,target_date,bucket_c,side,generated_at,model_prob,market_price) "
        "VALUES (?,?,?,?,?,?,?)", (st, td, bucket, side, gen, p, m),
    )


def _settle(con, st, td, bucket):
    con.execute(
        "INSERT INTO settled_buckets (station_icao,target_date,bucket_c,bucket_min_c,bucket_max_c,source,recorded_at) "
        "VALUES (?,?,?,?,?,?,?)", (st, td, bucket, 30, 32, "t", "t"),
    )


def _gen(st, td, hour, minute=0):
    start, _ = config.local_day_bounds_utc(st, date.fromisoformat(td))
    return (start + timedelta(hours=hour, minutes=minute)).isoformat()


def test_loader_takes_the_first_in_window_cycle_labels_it_and_stops_before_the_day(tmp_db):
    con = sqlite3.connect(tmp_db)
    td = "2026-09-10"
    # 04:30 local is collection, not entry window: ignored.
    for b in (30, 31, 32):
        _snap(con, "WSSS", td, b, _gen("WSSS", td, 4, 30), 0.9, 0.9)
    first, later = _gen("WSSS", td, 5, 0), _gen("WSSS", td, 6, 0)
    for b, p, m in ((30, 0.2, 0.25), (31, 0.5, 0.45), (32, 0.3, 0.35)):
        _snap(con, "WSSS", td, b, first, p, m)
        _snap(con, "WSSS", td, b, later, 0.99, 0.99)
        _snap(con, "WSSS", td, b, first, 1 - p, 0.7, side="NO")
    _settle(con, "WSSS", td, 31)
    # unsettled day, and a day ON the boundary: both excluded.
    _snap(con, "WSSS", "2026-09-11", 31, _gen("WSSS", "2026-09-11", 5), 0.5, 0.5)
    _snap(con, "WSSS", "2026-09-12", 31, _gen("WSSS", "2026-09-12", 5), 0.5, 0.5)
    _settle(con, "WSSS", "2026-09-12", 31)
    con.commit()

    rows = storage.load_shrink_fit_rows(date(2026, 9, 12))

    assert sorted((r["bucket_c"], r["model_prob"], r["market_price"], r["settled_bucket_c"]) for r in rows) == [
        (30, 0.2, 0.25, 31), (31, 0.5, 0.45, 31), (32, 0.3, 0.35, 31),
    ]
    assert {r["target_date"] for r in rows} == {td}
