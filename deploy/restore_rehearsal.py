#!/usr/bin/env python3
"""
restore_rehearsal.py -- prove a backup actually restores, without touching
the live DB. Gap audit 2026-09-24 (item 2): deploy_daemon.sh takes an
sqlite online backup on every deploy (its "== backup ==" step, ~L212-238)
and keeps the three newest at $HOME/polyweather-pre-deploy-*.sqlite3, but
nothing had ever exercised restoring one.

Usage:
    python deploy/restore_rehearsal.py <backup_file.sqlite3>

Copies the backup into a fresh temp file, runs PRAGMA integrity_check on
the copy, runs storage.migrate() against the COPY (never the source, never
the live DB), then prints row counts and the newest timestamp for the key
tables. Exit 0 if integrity_check says "ok" and migrate() and every table
read succeed; nonzero otherwise.
"""
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "weather-forecast"))

import config  # noqa: E402
import storage  # noqa: E402

# Key tables (storage.py's CREATE TABLE list) and the column that carries
# their newest write, or None where there isn't one.
TABLE_TIMESTAMP_COLUMNS = {
    "forecasts": "fetched_at",
    "observations": None,
    "positions": "entry_time",
    "entry_decisions": "cycle_ts",
    "settled_buckets": "recorded_at",
}


def rehearse(backup_path: str) -> int:
    src = Path(backup_path)
    if not src.is_file():
        print(f"!! {src} does not exist")
        return 1

    tmp_dir = Path(tempfile.mkdtemp(prefix="polyweather_restore_rehearsal_"))
    tmp_db = tmp_dir / "restore.sqlite3"
    shutil.copy2(src, tmp_db)  # never touches src again from here on
    print(f"-- copied {src} ({src.stat().st_size} bytes) -> {tmp_db}")

    t0 = time.monotonic()
    conn = sqlite3.connect(tmp_db)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        print(f"!! PRAGMA integrity_check FAILED: {result}")
        return 1
    print(f"-- integrity_check: ok ({time.monotonic() - t0:.2f}s)")

    t1 = time.monotonic()
    config.DB_PATH = str(tmp_db)  # redirect storage at the temp copy only
    storage.set_writable(True)
    try:
        storage.migrate()
    except Exception as exc:  # noqa: BLE001 - a failed migrate IS the finding
        print(f"!! migrate() FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        storage.set_writable(False)
    print(f"-- migrate(): ok ({time.monotonic() - t1:.2f}s) -- {storage.schema_summary()}")

    print("-- row counts and newest timestamp per key table:")
    ok = True
    conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
    try:
        for table, ts_col in TABLE_TIMESTAMP_COLUMNS.items():
            try:
                (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                newest = conn.execute(f"SELECT MAX({ts_col}) FROM {table}").fetchone()[0] if ts_col else "n/a"
                print(f"   {table}: {count} rows, newest={newest}")
            except sqlite3.Error as exc:
                print(f"   {table}: ERROR {exc}")
                ok = False
    finally:
        conn.close()

    print(f"-- total rehearsal time: {time.monotonic() - t0:.2f}s")
    print(f"-- temp copy left at {tmp_db} for inspection; the live DB was never opened")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python deploy/restore_rehearsal.py <backup_file.sqlite3>")
        sys.exit(2)
    sys.exit(rehearse(sys.argv[1]))
