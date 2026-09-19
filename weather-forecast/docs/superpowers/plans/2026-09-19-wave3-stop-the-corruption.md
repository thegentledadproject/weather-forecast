# Wave 3: Stop the Data from Corrupting Itself — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** DRAFT 2026-09-19 — not started. Target merge + deploy ~2026-09-25, inside the 15:00–19:00Z gap.

**Goal:** Remove the four ways the production record corrupts itself without anyone deciding to: every process that opens the trading database issues schema DDL (3a); the dashboard timer runs as root and can leave root-owned files beside a database the daemon must write (3b); the entry leg runs before the exit check, so yesterday's resolved position holds a live slot through the 05:00 window, and a past-dated position with no price is held blind for three cycles (3c); the exit-tightening hour and the replay's local clock use the static registry offset, so every DST station tightens at true 11:00 (3d). Sweep the stale comments the 2026-09-15 reviews listed (3e) and settle, read-only, whether `NegRiskAdapter.redeemPositions` expects a two-element amounts array (3f). Fold in two carried items: `scheduler._config_sha` resolved at boot, and `deploy_daemon.sh` starting the dashboard before the daemon restart.
**Architecture:** `storage.migrate()` holds every DDL statement and the entry-fee backfill (moved verbatim out of `_connect()` into `_apply_schema(conn)`); `_connect()` is open-only and, unless `storage.set_writable(True)` was called in this process, opens a `mode=ro` URI — a write then raises `StorageReadOnlyError` naming the process. The daemon calls `scheduler._boot_storage()` (migrate → writable → `_config_sha` primed) at the top of `run_forever`; the three operator writers (`manual_trigger.py`, `bucket_bias.py --ingest`, `main.py`) call `set_writable(True)`; everything else — dashboards, cohort_monitor, calibration_panel, promotion_dossier, the sweeps, spread_audit, redeem.py — is read-only by default with no edit. `deploy_daemon.sh` stops the daemon, backs the database up through the sqlite backup API, runs `storage.migrate()` as ubuntu, restarts the daemon and only then starts the dashboard. `setup_dashboard.sh` writes `User=ubuntu` and hands `/var/www/html` to ubuntu. 3c and 3d are three flags in a `WAVE 3` block at the end of `config.py`, defaulting on, each read at one site. The test suite gets ONE migrated throwaway database per session and ONE `tmp_db` fixture; the 36 per-file copies of "point DB_PATH at a tmp file and let `_connect()` build the schema" either alias `tmp_db` or call `storage.migrate()` explicitly.
**Tech Stack:** Python 3.12, sqlite3, pytest; bash/systemd on Ubuntu EC2; no numpy/scipy (not on the box)
**Spec:** docs/superpowers/specs/2026-09-17-evidence-first-remediation-design.md (section "Wave 3 — stop the data from corrupting itself")

## Global Constraints
- 3c and 3d sit behind `EXIT_CHECK_BEFORE_ENTRIES`, `PAST_DATED_GAMMA_ON_FIRST_FAILURE` and `DST_AWARE_LOCAL_HOUR` in the `WAVE 3` block of `config.py`, defaulting to on; `False` restores the pre-Wave-3 path exactly at the one site each is read. 3a's read-only default is NOT flagged (it is the point); `storage.set_writable(True)` is the escape hatch, and an AST test pins which four modules call it.
- No statistic changes in this wave: NO `REGIME_BOUNDARIES` stamp. `config.REGIME_BOUNDARIES` stays `("2026-09-20",)`.
- One merge, one deploy (spec Principle 1). The deploy is BACKUP-AND-STOP (P1-8(b) shape): `deploy_daemon.sh` now does it itself (stop → sqlite backup → `storage.migrate()` as ubuntu → restart → dashboard). It MUST land in the 15:00–19:00Z gap, when no region's entry window is open (Asia 20:00–00:00Z, Europe 02:00–07:00Z, Americas 09:00–16:00Z at UTC−4). Never 05:00–08:00 SGT.
- Nothing here touches live-money semantics: `HOLD_TO_SETTLEMENT_MODES`, `LIVE_TRADE_SIZE_USD`, the live executor, and `redeem.py` are untouched (3f is a read-only script; the redeem.py fix is a checkpoint decision).
- `_connect()` issues no DDL — a structural test in `tests/test_no_fd_leak.py` fails the moment an `execute` reappears in it. `backtest/price_store.py` (the market-data database) keeps its own lazy DDL: out of scope, noted in the risks.
- Run pytest from the `weather-forecast/` package dir; never on the EC2 box. The deploy dir is `deploy/` at the GIT ROOT (a sibling of the package dir), so test paths reach it as `pathlib.Path(__file__).resolve().parents[2] / "deploy"`.
- Commit after every task; commit messages end with the line: Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
---

**Paths.** The git root is `C:\Users\user\Downloads\weather-forecast`; the Python package is the nested `weather-forecast/` directory; the deploy scripts are `deploy/*.sh` and `deploy/generate_*.py` at the GIT ROOT. Every path below is relative to the package dir unless it starts with `../deploy/`. Every `pytest` and `git` command runs from the package dir (git commands name `../deploy/...` for the deploy files). Line numbers are as of HEAD `1031b4e`, before any Wave 3 task; when an earlier task has shifted them, match on the quoted code, not the number. The suite collects **1851** tests at HEAD.

**Branch.** `git checkout -b feat/wave3-stop-the-corruption` from `main` at `1031b4e` before Task 1.

**Four facts that shape the tasks.**

1. *`_connect()` today is `sqlite3.connect(config.DB_PATH)` followed by ~360 lines of DDL and one backfill UPDATE, then `conn.commit()` (storage.py:205-580). There are NO PRAGMAs to preserve.* `_db()` (storage.py:27-61) wraps it in `with conn:` and closes in a `finally`. Every module-level storage function goes through `_db()`; nothing in the package opens the trading database any other way except `wave1_falsifier.py`/`wave2_falsifier.py` (their own `mode=ro` URI) and the deploy script's demotion guard (a plain read). So "read-only by default" is ONE change in `_connect()`, and "the daemon is the writer" is one call at boot.
2. *193 tests in 34 files rely on `_connect()` building the schema lazily.* Measured on HEAD by running the suite with `_connect()` replaced by an open-only version (plugin injected from the scratchpad, repo untouched): `132 failed, 1658 passed, 61 errors`. Every failing file sets `config.DB_PATH` to a fresh tmp path (or reaches `conftest.build_scenario`, which does) and then writes through storage. Re-running the same prototype with the DEFAULT `DB_PATH` pointed at an EMPTY migrated session temp file produced the identical 193 — so isolating the suite from the operator's `data/polyweather.sqlite3` (which `test_no_fd_leak.py` was flagged for writing) costs nothing and is folded into Task 1. The ruling counted 13 fixtures; the real count is in the Task 1 edit table.
3. *Every production caller of `risk_manager.evaluate_exit` already passes `local_hour` explicitly* (position_manager.py:456-458 from `_local_hour_for`; backtest/engine.py:864 from `SimClock.local_hour()`; backtest/take_sweep.py:272 from `config.current_utc_offset_hours(at=...)`; backtest/entry_bar_sweep.py:121 with a literal). `risk_manager._local_hour(tz_offset_hours=8)` is reached only through `_active_thresholds(local_hour=None)`, and the only caller that omits the hour is `tests/test_parity_exit.py`, which pins the wall clock for a WSSS position. So the UTC+8 default is dead in production and can be REMOVED: `evaluate_exit` resolves a missing hour from the POSITION's own station (Task 5), which is what the parity test was proving anyway.
4. *Exits-first is strictly safer than today on the failure path too.* `_run_full_cycle` returns early when `pipeline.run()` raises (scheduler.py:475-477), so today a forecast outage also SKIPS the exit check for that station-cycle (tests/test_exit_snapshot_capture.py:212-215 documents it). Moving the exit check to the top of the function fixes that as a side effect; Task 4 pins it. The comment at scheduler.py:565-581 that justifies `interval_min=None` ("the exit check runs seconds AFTER the EV leg, so its ask-less row is the NEWER one") still holds in spirit — nothing is captured on that call either way — and is rewritten rather than deleted.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `storage.py` | modify | `StorageReadOnlyError`, `_WRITABLE`, `set_writable`, `is_writable`, `migrate`, `schema_summary`, `_apply_schema` (the DDL body, verbatim); `_connect` open-only + `mode=ro`; `_db` translates the readonly error; view docstring and the three "on every connection" comments corrected |
| `tests/conftest.py` | modify | session-scoped isolated default database; `tmp_db` fixture; `build_scenario` migrates |
| `tests/test_no_fd_leak.py` | modify | the structural guard extended: `_connect` has no `execute`, `_apply_schema` has every table |
| `tests/test_wave3_storage_migrate.py` | create | migrate on a pre-Wave-1 schema, idempotence, read-only default refuses writes and names the process, missing file under ro, `schema_summary` |
| 34 test files (Task 1 table) | modify | alias `tmp_db` or call `storage.migrate()` |
| `scheduler.py` | modify | `_boot_storage` (migrate, writable, `_config_sha` primed) at the top of `run_forever`; 3c ordering in `_run_full_cycle` |
| `manual_trigger.py`, `bucket_bias.py`, `main.py` | modify | `storage.set_writable(True)` at the operator write sites |
| `../deploy/deploy_daemon.sh` | modify | generator copy no longer starts the dashboard; stop → backup → migrate → restart → dashboard |
| `tests/test_wave3_writers_and_readers.py` | create | boot sequence; `set_writable` call-site allowlist (AST over the package + deploy); deploy script order |
| `../deploy/setup_dashboard.sh` | modify | idempotent; `User=ubuntu`; `chown -R ubuntu:ubuntu /var/www/html`; journal group; generators copied from the repo |
| `tests/test_wave3_dashboard_as_ubuntu.py` | create | unit content; generators never call `set_writable`/`migrate`/`sqlite3.connect` |
| `config.py` | modify | the `WAVE 3` flag block; 3e comment fixes at 602-604, 1446, 2539 |
| `position_manager.py` | modify | 3c first-failure Gamma path; 3d `_local_hour_for` |
| `tests/test_wave3_exit_check_first.py` | create | order of calls; the 2026-09-02 journal case; pipeline failure no longer skips exits; first-failure Gamma; flag-off paths |
| `risk_manager.py` | modify | `_local_hour` required offset; `_station_offset_now`; `_active_thresholds(local_hour)` required; 3e docstring/comment fixes |
| `backtest/simclock.py`, `backtest/engine.py`, `backtest/settings.py` | modify | `utc_offset_for`, `SimClock.retune`; per-day offset in the day loop; stale comment |
| `tests/test_wave3_dst_local_hour.py` | create | EGLC in September tightens at 10:00 true local live and in the replay; flag off restores static |
| `models.py`, `executor.py`, `storage.py` | modify | 3e trailing-stop references |
| `tests/test_wave3_stale_docs.py` | create | the stale phrases are gone, the replacements present |
| `redeem_abi_probe.py` | create | read-only ABI spike |
| `tests/test_wave3_redeem_abi_probe.py` | create | selector, PUSH4 scanner, source-line reader, import allowlist, `--offline` |

---

### Task 1: 3a storage — `migrate()`, read-only by default, one `tmp_db`
**Files:** Modify `storage.py` (imports 18-24; `_db` 27-61; `_ensure_position_economics_view` docstring 153-170; `_connect` 205-580), `tests/conftest.py` (lines 160-166; new fixtures after `_deterministic_maturity`, line 395), `tests/test_no_fd_leak.py` (after `test_storage_has_no_bare_with_connect`, line 151), the 34 test files in the edit table / Test `tests/test_wave3_storage_migrate.py`
**Interfaces:** Produces: `storage.StorageReadOnlyError(RuntimeError)`, `storage._WRITABLE: bool = False`, `storage.set_writable(flag: bool = True) -> None`, `storage.is_writable() -> bool`, `storage.migrate() -> None`, `storage.schema_summary() -> str`, `storage._apply_schema(conn) -> None`, conftest `tmp_db` (returns the path as `str`), conftest `_isolated_default_db` (session, autouse)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave3_storage_migrate.py
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
    assert "8 tables" in storage.schema_summary() and "position_economics" in storage.schema_summary()


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
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave3_storage_migrate.py -v` / Expected: FAIL with `AttributeError: module 'storage' has no attribute 'migrate'` (and `fixture 'tmp_db' not found` on the two `tmp_db` tests)

- [ ] **Step 3: Write minimal implementation**

storage.py imports (line 18-24) — old:
```python
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import config
from models import PointForecast, ObservedReading, Position, SettledToken
```
new:
```python
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from models import PointForecast, ObservedReading, Position, SettledToken


class StorageReadOnlyError(RuntimeError):
    """
    A process that never called set_writable(True) tried to write, or tried
    to open a database file that does not exist. WAVE 3 (3a): the daemon and
    three named operator scripts write; everything else reads mode=ro.
    """


# WAVE 3 (3a). READ-ONLY BY DEFAULT. Until 2026-09-25 every process that
# imported this module -- the daemon, the root dashboard timer, cohort_monitor,
# the sweeps, a pytest run -- opened the file read-write AND issued the whole
# schema (CREATE TABLE / ALTER TABLE / the entry-fee UPDATE) on every single
# connection. Five processes racing DDL on one file, one of them root, is how
# a database corrupts itself with nobody deciding to. Now: _connect() opens a
# `mode=ro` URI unless THIS process called set_writable(True); the schema is
# applied by migrate(), which scheduler._boot_storage() runs once at daemon
# boot and deploy/deploy_daemon.sh runs once with the daemon stopped.
#
# Who flips this: scheduler.run_forever (the writer), manual_trigger.py,
# bucket_bias.py --ingest and main.py (operator writes). Nothing else may --
# tests/test_wave3_writers_and_readers.py pins the call sites by AST. The
# test suite sets it in conftest because tests own their throwaway files.
_WRITABLE = False


def set_writable(flag: bool = True) -> None:
    """Declare THIS process a writer (or not). See _WRITABLE."""
    global _WRITABLE
    _WRITABLE = bool(flag)


def is_writable() -> bool:
    return _WRITABLE


def _process_name() -> str:
    return os.path.basename(sys.argv[0] or "") or "python"
```

storage.py `_db` (line 27-61) — old:
```python
@contextmanager
def _db():
    """
    Transaction scope AND connection lifetime in one context manager.
    EVERY function in this module must use _db(); never `with _connect()`.
```
new (the docstring's first two lines only; the rest of the docstring is unchanged):
```python
@contextmanager
def _db():
    """
    Transaction scope AND connection lifetime in one context manager.
    EVERY function in this module must use _db(); never `with _connect()`.

    WAVE 3 (3a): also the one place a read-only process's write becomes a
    StorageReadOnlyError. sqlite reports "attempt to write a readonly
    database" as an OperationalError, which reads like a lock or a disk
    problem; naming the process and the fix is what turns a mystery into a
    one-line journal entry.
```
and its body (line 56-61) — old:
```python
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()
```
new:
```python
    conn = _connect()
    try:
        with conn:
            yield conn
    except sqlite3.OperationalError as exc:
        if not _WRITABLE and "readonly" in str(exc).lower():
            raise StorageReadOnlyError(
                f"{_process_name()} opened {config.DB_PATH} read-only (storage._WRITABLE is "
                f"False) and tried to write: {exc}. Only the daemon (scheduler.run_forever) "
                f"and the operator writers named in storage.py call storage.set_writable(True); "
                f"if this process is meant to write, call it at boot."
            ) from exc
        raise
    finally:
        conn.close()
```

storage.py `_ensure_position_economics_view` docstring (line 153-170) — old:
```python
    NOT an unconditional DROP + CREATE either. This runs on EVERY connection
    (storage opens one per call site), and a schema write on each would take
    a write lock and bump the schema cookie, invalidating prepared statements
    across the daemon for a view that holds no data. Comparing the stored SQL
    first makes the common case a single read.

    THE REBUILD IS NOT ATOMIC, so the CREATE tolerates a peer having won the
    race. Each execute() here is its own transaction in autocommit, and more
    than one process reaches this code: the daemon and the dashboard
    generator both open connections, the latter on a 5-minute timer. Two of
    them interleaving as DROP, DROP, CREATE, CREATE would leave the second
    CREATE raising "view position_economics already exists" out of
    _connect(), i.e. out of the one function every storage call goes
    through. IF NOT EXISTS makes that loser a no-op, which is correct: the
    peer just wrote the definition this process was about to write. The
    window is only open until someone has stored the current definition, so
    in practice this is the first moments after a deploy or an edit to the
    SQL above.
```
new:
```python
    NOT an unconditional DROP + CREATE either. Since WAVE 3 (3a) this runs
    only from migrate() -- daemon boot and the deploy script -- but a schema
    write on every migrate would still take a write lock and bump the schema
    cookie for a view that holds no data. Comparing the stored SQL first
    makes the common case a single read.

    THE REBUILD IS NOT ATOMIC, so the CREATE tolerates a peer having won the
    race. Before Wave 3 every connection ran this and the daemon raced the
    root dashboard timer; now only a daemon booting while a deploy's
    migrate() is still running could interleave DROP, DROP, CREATE, CREATE,
    and deploy_daemon.sh stops the daemon first. IF NOT EXISTS is kept so the
    loser of any such race is a no-op rather than a raise out of migrate():
    the peer just wrote the definition this process was about to write.
```

storage.py `_connect` (line 205-206) — old:
```python
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecasts (
```
new:
```python
def _connect() -> sqlite3.Connection:
    """
    OPEN ONLY. No DDL, no backfill, no PRAGMA -- WAVE 3 (3a). Writable
    processes get a plain connection; every other process gets a `mode=ro`
    URI, which sqlite refuses to create, so a reader can never leave an
    empty root-owned file where the daemon's database should be.
    """
    if _WRITABLE:
        return sqlite3.connect(config.DB_PATH)
    path = Path(config.DB_PATH).resolve()
    if not path.exists():
        raise StorageReadOnlyError(
            f"{_process_name()} opened {path} read-only and it does not exist. The daemon "
            f"creates it at boot (scheduler._boot_storage -> storage.migrate()); run that, "
            f"or `python -c \"import storage; storage.migrate()\"` as the daemon's user."
        )
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def migrate() -> None:
    """
    Apply the whole schema -- every CREATE TABLE / INDEX, the idempotent
    ALTER TABLE column list, the entry-fee backfill, the economics view --
    and commit. Idempotent. THE ONLY DDL PATH (WAVE 3, 3a): run by
    scheduler._boot_storage() at daemon boot and by deploy/deploy_daemon.sh
    with the daemon stopped. Opens read-write regardless of _WRITABLE,
    because applying the schema is the one write a deploy makes.
    """
    conn = sqlite3.connect(config.DB_PATH)
    try:
        _apply_schema(conn)
        conn.commit()
    finally:
        conn.close()


def schema_summary() -> str:
    """'N tables: a, b, ...; M view(s)' -- one line for the deploy log."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    tables = [n for t, n in rows if t == "table"]
    views = [n for t, n in rows if t == "view"]
    return f"{len(tables)} tables: {', '.join(tables)}; {len(views)} view(s): {', '.join(views)}"


def _apply_schema(conn: sqlite3.Connection) -> None:
    """
    The DDL, moved VERBATIM out of _connect() on 2026-09-2x (Wave 3, 3a).
    Every statement is idempotent (IF NOT EXISTS, PRAGMA-guarded ALTER,
    WHERE ... IS NULL) because a deploy and a daemon boot both run it.
    tests/test_no_fd_leak.py asserts every table is declared here and that
    _connect() executes nothing.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecasts (
```
Everything from that first `CREATE TABLE IF NOT EXISTS forecasts` through `_ensure_position_economics_view(conn)` (old lines 207-562) stays byte-for-byte as the body of `_apply_schema`, with three comments corrected in place:

old (line 267-275):
```python
    # CREATE TABLE IF NOT EXISTS is a no-op against a database that already has
    # a `positions` table from before these columns existed -- it does
    # NOT add columns to an existing table. Without this migration, an
    # existing deployed database silently keeps the old schema and every read
    # of the new fields (size_shares, execution_mode, order_id) fails or, for
    # SELECT *, just returns short rows. Run on every connection so every
    # code path (backtest scripts, tests, the live executor) gets migrated,
    # and check PRAGMA table_info first so this stays idempotent -- ALTER
    # TABLE ADD COLUMN errors if the column is already there.
```
new:
```python
    # CREATE TABLE IF NOT EXISTS is a no-op against a database that already has
    # a `positions` table from before these columns existed -- it does
    # NOT add columns to an existing table. Without this migration, an
    # existing deployed database silently keeps the old schema and every read
    # of the new fields (size_shares, execution_mode, order_id) fails or, for
    # SELECT *, just returns short rows. Run from migrate() -- daemon boot and
    # the deploy script, since Wave 3 -- and check PRAGMA table_info first so
    # this stays idempotent: ALTER TABLE ADD COLUMN errors if the column is
    # already there.
```
old (line 366-371):
```python
    # GUARDED BY A READ, because this runs on every connection and the daemon
    # opens one per storage call. An unconditional UPDATE would take a write
    # lock every time -- on a table that needs it exactly once -- and a write
    # lock on the connection path is how a read-heavy daemon starts contending
    # with itself. The SELECT costs nothing after the first run and stops the
    # UPDATE from ever being issued again.
```
new:
```python
    # GUARDED BY A READ. Until Wave 3 this ran on every connection, and an
    # unconditional UPDATE would have taken a write lock on every storage call
    # for a table that needs it exactly once. It now runs only from migrate(),
    # but the guard stays: a deploy's migrate and the daemon's boot migrate
    # both run it, and neither should issue an UPDATE that changes nothing.
```
old (line 564-580):
```python
    # WAVE 1 (2026-09-17). The entry-fee backfill above is a DML statement
    # (UPDATE), and under sqlite3's default (legacy) transaction control that
    # opens an implicit transaction covering every statement after it --
    # including every CREATE TABLE / CREATE INDEX / ALTER TABLE below it in
    # this function. Every normal caller goes through _db(), whose `with
    # conn:` commits on the way out, so this was invisible. But
    # `storage._connect().close()` is ALSO an established idiom across this
    # suite (test_no_fd_leak.py, test_station_maturity.py, and this file's
    # own tests) for "just run the migration" -- and .close() on a
    # connection with a pending transaction rolls it back, silently, with no
    # exception. On a legacy database that still needs the entry-fee
    # backfill, that discarded every table/index created after it, including
    # this Wave's entry_decisions. Committing explicitly here makes
    # `_connect()` durable on its own, matching what `_db()` already gave
    # every other caller.
    conn.commit()
    return conn
```
new:
```python
    # WAVE 1 (2026-09-17) found that the entry-fee backfill above is a DML
    # statement (UPDATE) whose implicit transaction covered every CREATE /
    # ALTER after it, and that closing without a commit rolled them all back
    # silently. migrate() commits explicitly after this function returns;
    # nothing else may call this.
```
(`_apply_schema` returns None; `migrate()` owns the commit.)

tests/conftest.py `build_scenario` (line 160-166) — old:
```python
    import config

    monkeypatch.setattr(config, "DB_PATH", str(trading_db))

    import storage
    from models import ObservedReading, PointForecast
    from backtest import price_store, simclock
```
new:
```python
    import config

    monkeypatch.setattr(config, "DB_PATH", str(trading_db))

    import storage
    from models import ObservedReading, PointForecast
    from backtest import price_store, simclock

    # WAVE 3 (3a): _connect() no longer builds the schema; the fixture does.
    storage.migrate()
```
and its docstring (line 144-146) — old:
```python
    config.DB_PATH is monkeypatched to the throwaway trading db BEFORE
    storage is touched -- storage._connect() reads config.DB_PATH at call
    time, so the patch has to be live for both the seeding below and the
```
new:
```python
    config.DB_PATH is monkeypatched to the throwaway trading db BEFORE
    storage is touched, then storage.migrate() builds the schema there --
    storage reads config.DB_PATH at call time, so the patch has to be live
    for both the seeding below and the
```

tests/conftest.py, appended after `_deterministic_maturity` (line 395):
```python


@pytest.fixture(scope="session", autouse=True)
def _isolated_default_db(tmp_path_factory):
    """
    WAVE 3 (3a). Two things every test gets without asking:

      * config.DB_PATH points at ONE empty, migrated temp file for the whole
        session -- never at data/polyweather.sqlite3 in the checkout. Until
        this fixture the suite wrote into the operator's own database (the
        memory note that said "do not run the suite on the box" was about
        exactly this). Measured before the change: pointing the default at
        an empty file fails nothing.
      * storage._WRITABLE is True for the test process. Tests own their
        throwaway files; the read-only default is exercised by the tests
        that flip it back explicitly (tests/test_wave3_storage_migrate.py).

    Session-scoped MonkeyPatch, undone at the end of the run.
    """
    import config
    import storage

    path = tmp_path_factory.mktemp("session-db") / "polyweather.sqlite3"
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DB_PATH", str(path))
    mp.setattr(storage, "_WRITABLE", True)
    storage.migrate()
    yield
    mp.undo()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """
    A fresh, MIGRATED trading database at a throwaway path, with
    config.DB_PATH pointed at it for the test. Returns the path as a str.
    THE one implementation of what 36 fixtures used to do by hand (point
    DB_PATH somewhere and let _connect() build the schema); those now alias
    this or call storage.migrate() after their own setattr.
    """
    import config
    import storage

    path = tmp_path / "trading.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    storage.migrate()
    return str(path)
```

tests/test_no_fd_leak.py, appended after `test_storage_has_no_bare_with_connect` (after line 151):
```python


def test_connect_issues_no_ddl_and_apply_schema_declares_every_table():
    """
    WAVE 3 (3a) structural guard. _connect() opens and returns; the schema
    lives in _apply_schema(), which only migrate() calls. The moment an
    execute() reappears in _connect() -- "just one CREATE IF NOT EXISTS" --
    every reader is a schema writer again, root dashboard timer included.
    """
    import ast
    from pathlib import Path

    text = Path(storage.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def _executes(fn):
        return [
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("execute", "executescript", "executemany")
        ]

    assert not _executes(fns["_connect"]), (
        f"storage._connect() must issue no SQL -- execute() at lines {_executes(fns['_connect'])}"
    )
    assert _executes(fns["_apply_schema"])
    body = ast.get_source_segment(text, fns["_apply_schema"])
    for table in ("forecasts", "observations", "positions", "live_order_attempts",
                  "entry_decisions", "settled_buckets", "ensemble_spread", "ev_snapshots"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in body, table
    assert "_ensure_position_economics_view(conn)" in body
    # migrate() is the only caller of _apply_schema, and migrate() commits.
    callers = [
        fn.name for fn in fns.values()
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_apply_schema"
    ]
    assert callers == ["migrate"]
    assert "conn.commit()" in ast.get_source_segment(text, fns["migrate"])
```

**The fixture edits.** Every site is one of two shapes. **ALIAS** replaces a fixture whose whole job was "point DB_PATH at a tmp file" with a delegation to `tmp_db` (the local name is kept so no test signature changes). **MIGRATE** adds `storage.migrate()` right after a `setattr(config, "DB_PATH", ...)` that is followed by writes, and replaces the `storage._connect().close()` idiom ("just run the migration", which now does nothing) with `storage.migrate()`. All 36 sites in the 34 files that fail without lazy DDL, plus the two `_connect().close()` idioms in files that happen to pass:

| file:line | shape | old | new |
|---|---|---|---|
| tests/test_spread_estimator.py:25-29 | MIGRATE | `    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))` … `    storage._connect().close()` | keep the setattr and the `_pooled_spread_cache` line; `    storage._connect().close()` → `    storage.migrate()` |
| tests/test_spread_estimator.py:215 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_spread_tier_brier_days.py:27-29 | ALIAS | `@pytest.fixture`<br>`def db(tmp_path, monkeypatch):`<br>`    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))` | `@pytest.fixture`<br>`def db(tmp_db):`<br>`    return tmp_db` |
| tests/test_station_maturity.py:28,36 | MIGRATE | line 36 `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_station_maturity.py:125 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_entry_fee_migration.py:44-47 | ALIAS | `def db(tmp_path, monkeypatch):`<br>`    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))`<br>`    return str(tmp_path / "t.sqlite3")` | `def db(tmp_db):`<br>`    return tmp_db` |
| tests/test_entry_fee_migration.py:95-99 | MIGRATE | docstring `    _connect() runs the migration on EVERY connection, so "runs twice" is the`<br>`    normal case, not an edge case.` | `    migrate() runs at every daemon boot AND on every deploy, so "runs twice"`<br>`    is the normal case, not an edge case.` and insert `    storage.migrate()` as the first statement of the test body |
| tests/test_entry_fee_migration.py:119 | MIGRATE | `    storage.load_position_history("WSSS")  # any connection runs the migration` | `    storage.migrate()  # the backfill runs from migrate(), not from a read` |
| tests/test_entry_fee_migration.py:138 | MIGRATE | `    storage.load_position_history("WSSS")` (the line before `    after = _raw(db)`) | `    storage.migrate()` |
| tests/test_entry_fee_migration.py:159 | MIGRATE | `    storage.load_position_history("WSSS")` (the line before `    assert _raw(db)["entry_fee_per_share"] == 0.99`) | `    storage.migrate()` |
| tests/test_live_execution.py:1668-1669 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_position_economics_view.py:28-32 | ALIAS | `def db(tmp_path, monkeypatch):`<br>`    """A real, migrated database at a throwaway path."""`<br>`    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))`<br>`    return config.DB_PATH` | `def db(tmp_db):`<br>`    """A real, migrated database at a throwaway path."""`<br>`    return tmp_db` |
| tests/test_ev_snapshot_retention.py:26-29 | ALIAS | `def db(tmp_path, monkeypatch):`<br>`    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))`<br>`    return tmp_path` | `def db(tmp_db, tmp_path):`<br>`    return tmp_path` |
| tests/test_ev_snapshot_retention.py:142-146 | MIGRATE | after `    monkeypatch.setattr(config, "DATA_DIR", tmp_path)` | insert `    storage.migrate()` |
| tests/test_entry_prediction_recorded.py:35 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_entry_prediction_recorded.py:130 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave1_shadow_pass.py:119 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave1_schema.py:4 | doc | `both from any storage._connect() -- and gain them once.` | `both from storage.migrate() -- and gain them once.` |
| tests/test_wave1_schema.py:60,65 | MIGRATE | `def test_connect_adds_the_five_columns_and_the_table(` … `    storage._connect().close()` | `def test_migrate_adds_the_five_columns_and_the_table(` … `    storage.migrate()` |
| tests/test_wave1_schema.py:78 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave1_schema.py:84 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave1_schema.py:91-92 | MIGRATE | `    storage._connect().close()`<br>`    storage._connect().close()` | `    storage.migrate()`<br>`    storage.migrate()` |
| tests/test_wave1_schema.py:107 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_ensemble_spread_collection.py:28-30 | ALIAS | `def db(tmp_path, monkeypatch):`<br>`    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))` | `def db(tmp_db):`<br>`    return tmp_db` |
| tests/test_ensemble_spread_storage.py:18-21 | ALIAS | `def db(tmp_path, monkeypatch):`<br>`    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))`<br>`    return tmp_path` | `def db(tmp_db, tmp_path):`<br>`    return tmp_path` |
| tests/test_settled_token_identity.py:27-33 | MIGRATE | `    storage.load_open_positions()` (the "forces schema creation" call) | `    storage.migrate()` |
| tests/test_settled_token_wiring.py:27-38 | MIGRATE | `    storage.load_open_positions()  # forces schema creation` | `    storage.migrate()` ; docstring line 31 `storage._connect(), so patching it here covers both the seeding and` → `storage, so patching it here covers both the seeding and` |
| tests/test_wave1_falsifier.py:37 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave2_falsifier.py:53 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave2_regime_reports.py:36 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave1_entry_decisions_recorded.py:27-31 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave1_deciding_numbers_persisted.py:20 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_wave1_resolved_net_ev.py:28-31 | ALIAS | `def db(tmp_path, monkeypatch):`<br>`    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))`<br>`    return str(tmp_path / "t.sqlite3")` | `def db(tmp_db):`<br>`    return tmp_db` |
| tests/test_wave1_refusal_rows.py:220 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_stop_basis.py:187 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_stop_basis.py:201 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_stop_basis.py:211 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_stop_slippage_record.py:160-163 | ALIAS | `def db(tmp_path, monkeypatch):`<br>`    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))`<br>`    return str(tmp_path / "t.sqlite3")` | `def db(tmp_db):`<br>`    return tmp_db` |
| tests/test_no_fd_leak.py:38-42 | MIGRATE | `    storage._connect().close()` | `    storage.migrate()` |
| tests/test_unexitable_fill.py:105 | MIGRATE | after `    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))` | insert `    storage.migrate()` |
| tests/test_unexitable_fill.py:123 | MIGRATE | same | insert `    storage.migrate()` |
| tests/test_region_isolation.py:708 | MIGRATE | after `        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))` | insert `        storage.migrate()` |
| tests/test_region_isolation.py:726 | MIGRATE | same | insert `        storage.migrate()` |
| tests/test_bucket_axis.py:624-625 | MIGRATE | `        # No init step: storage._connect() creates the schema on first use.`<br>`        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))` | `        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))`<br>`        storage.migrate()` |
| tests/test_bucket_axis.py:638 | MIGRATE | after `        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))` | insert `        storage.migrate()` |
| tests/test_bucket_bias.py:223, 247, 271, 289, 310 | MIGRATE | after each `    monkeypatch.setattr(bba.storage.config, "DB_PATH", str(tmp_path / "t.sqlite3"), raising=False)` | insert `    bba.storage.migrate()` |
| tests/test_simclock_per_station.py:126 | MIGRATE | after `    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))` | insert `    storage.migrate()` (add `import storage` to the file's imports) |
| tests/test_simclock_per_station.py:147 | MIGRATE | same | insert `    storage.migrate()` |

(Files that set `DB_PATH` and build their own tables by hand — `test_forecast_bias.py`, `test_forecast_lead_window.py`, `test_spread_audit.py`, `test_wave2_error_sample_window.py` — and the two that only read through stubs — `test_wave2_forecast_outage_refuses.py`, `test_wave2_signed_admission_edge.py` — are untouched; they pass on the prototype.)

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave3_storage_migrate.py tests/test_no_fd_leak.py tests/test_wave1_schema.py tests/test_entry_fee_migration.py -v` / Expected: PASS
- [ ] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1860 (1851 + 8 new + 1 guard). Any remaining `no such table` failure is a site the table above missed: add `storage.migrate()` after its `DB_PATH` setattr and record it in the commit message.
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add storage.py tests/conftest.py tests/test_no_fd_leak.py tests/test_wave3_storage_migrate.py tests/test_spread_estimator.py tests/test_spread_tier_brier_days.py tests/test_station_maturity.py tests/test_entry_fee_migration.py tests/test_live_execution.py tests/test_position_economics_view.py tests/test_ev_snapshot_retention.py tests/test_entry_prediction_recorded.py tests/test_wave1_shadow_pass.py tests/test_wave1_schema.py tests/test_ensemble_spread_collection.py tests/test_ensemble_spread_storage.py tests/test_settled_token_identity.py tests/test_settled_token_wiring.py tests/test_wave1_falsifier.py tests/test_wave2_falsifier.py tests/test_wave2_regime_reports.py tests/test_wave1_entry_decisions_recorded.py tests/test_wave1_deciding_numbers_persisted.py tests/test_wave1_resolved_net_ev.py tests/test_wave1_refusal_rows.py tests/test_stop_basis.py tests/test_stop_slippage_record.py tests/test_unexitable_fill.py tests/test_region_isolation.py tests/test_bucket_axis.py tests/test_bucket_bias.py tests/test_simclock_per_station.py
git commit -m "Wave 3 (3a): storage.migrate() is the only DDL path; readers open mode=ro

_connect() opens and returns -- no CREATE, no ALTER, no backfill. The
schema moved verbatim into _apply_schema(), called only by migrate(),
which commits. A process that has not called set_writable(True) opens a
mode=ro URI; a write from it raises StorageReadOnlyError naming the
process, and a missing file is refused rather than created. The suite
now runs against one migrated session temp file (never the checkout's
data/polyweather.sqlite3) and one tmp_db fixture; the 36 per-file
copies of the lazy-schema fixture alias it or call migrate().

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: 3a callers — the daemon migrates at boot, three operator writers, the deploy script's order
**Files:** Modify `scheduler.py` (after `_config_sha` 106-112; `run_forever` body at 750), `manual_trigger.py` (`main()` line 161-162), `bucket_bias.py` (`__main__` at 600-603), `main.py` (`main()` line 30 and the `run(` call at 59), `../deploy/deploy_daemon.sh` (lines 59-79 and 156-194) / Test `tests/test_wave3_writers_and_readers.py`
**Interfaces:** Consumes: Task 1 / Produces: `scheduler._boot_storage() -> None`; `deploy_daemon.sh` steps `== stop ==`, `== backup ==`, `== migrate ==`, `== dashboard ==`

**Why redeem.py is NOT in the writer list.** The ruling names it as one of "the two operator writers"; on the code it is a reader: `redeem.py` never imports `storage`, and the only storage call on its path is `clients/redemption_client.py:122 storage.load_settled_live_tokens()` (a SELECT). Making it writable would grant a write it never makes. The two real operator writers beyond `manual_trigger.py` are `bucket_bias.py --ingest` (bucket_bias.py:600-603 → `storage.save_settled_bucket`) and `main.py` (`pipeline.run` → `storage.save_forecast`/`save_ensemble_spread`); both would otherwise die with `StorageReadOnlyError` after Task 1. Every other module-level entry point (cohort_monitor, calibration_panel, promotion_dossier, spread_audit, spread_tier_brier, stop_loss_audit, paper_trading_report, check_holdings, check_open_orders, backtest/cli, the three sweeps, observed_half_life, the falsifiers, the three generators) reads only, and the AST test below pins that none of them calls `set_writable`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave3_writers_and_readers.py
"""
Wave 3 item 3a, the callers. The daemon migrates and declares itself the
writer ONCE, at the top of run_forever, and primes the git sha there too (the
Wave 1 minor: _config_sha shelled out to git on the entry path). Exactly four
modules may call storage.set_writable -- the daemon and the three operator
scripts that write -- and the deploy script stops the daemon, backs up, runs
migrate() as ubuntu, restarts, and only THEN starts the dashboard.
"""
import ast
import pathlib

import config
import scheduler
import storage

PKG = pathlib.Path(__file__).resolve().parents[1]
REPO = PKG.parent

WRITERS = {"scheduler.py", "manual_trigger.py", "bucket_bias.py", "main.py"}


def _py_files():
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG)
        if rel.parts[0] in ("tests", ".venv", "docs") or "__pycache__" in rel.parts:
            continue
        yield rel.as_posix(), path
    for path in sorted((REPO / "deploy").glob("*.py")):
        yield "../deploy/" + path.name, path


def _calls(path, attr):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Attribute) and n.func.attr == attr)
            or (isinstance(n.func, ast.Name) and n.func.id == attr)
        )
    ]


def test_only_the_daemon_and_the_three_operator_writers_set_writable():
    found = {rel for rel, path in _py_files() if rel != "storage.py" and _calls(path, "set_writable")}
    assert found == WRITERS, f"set_writable call sites: {sorted(found)}"


def test_only_the_daemon_migrates_in_code():
    found = {rel for rel, path in _py_files() if rel != "storage.py" and _calls(path, "migrate")}
    assert found == {"scheduler.py"}, f"migrate() call sites: {sorted(found)}"


def test_boot_migrates_then_sets_writable_then_primes_the_sha(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "boot.sqlite3"))
    monkeypatch.setattr(storage, "_WRITABLE", False)
    monkeypatch.setattr(scheduler, "_config_sha_cache", {})
    monkeypatch.setattr(config, "_current_git_sha", lambda: "abc123")
    # No stations -> run_forever returns right after booting.
    monkeypatch.setattr(scheduler, "stations_by_utc_offset", lambda station_icaos=None: {})

    scheduler.run_forever()

    assert storage.is_writable()
    assert "positions" in storage.schema_summary()
    assert scheduler._config_sha_cache == {"sha": "abc123"}
    out = capsys.readouterr().out
    assert "boot: storage migrated" in out and "abc123" in out


def test_boot_order_is_migrate_before_writable(monkeypatch):
    order = []
    monkeypatch.setattr(storage, "migrate", lambda: order.append("migrate"))
    monkeypatch.setattr(storage, "set_writable", lambda flag=True: order.append(("writable", flag)))
    monkeypatch.setattr(storage, "schema_summary", lambda: "stub")
    monkeypatch.setattr(scheduler, "_config_sha", lambda: order.append("sha") or "s")
    scheduler._boot_storage()
    assert order == ["migrate", ("writable", True), "sha"]


def test_deploy_script_stops_backs_up_migrates_restarts_then_starts_the_dashboard():
    script = (REPO / "deploy" / "deploy_daemon.sh").read_text(encoding="utf-8")
    i_guard = script.index("refusing to demote a daemon holding live positions")
    i_stop = script.index("systemctl stop $SERVICE")
    i_backup = script.index(".backup(")
    i_migrate = script.index("storage.migrate()")
    i_restart = script.index("systemctl restart $SERVICE")
    i_dash = script.index("systemctl start polyweather-dashboard.service")
    assert i_guard < i_stop < i_backup < i_migrate < i_restart < i_dash
    # The generator-copy block no longer starts the dashboard: one start, at the end.
    assert script.count("systemctl start polyweather-dashboard.service") == 1
    # migrate runs as the script's own user (ubuntu), never under sudo.
    migrate_line = next(l for l in script.splitlines() if "storage.migrate()" in l)
    assert not migrate_line.lstrip().startswith("sudo")
    assert '"$VENV/bin/python" -c "import storage; storage.migrate()' in migrate_line
    # Backup through the sqlite backup API (a python heredoc), never cp on a live file.
    assert "import sqlite3" in script[i_backup - 300:i_backup]
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave3_writers_and_readers.py -v` / Expected: FAIL with `assert set() == {...}` on the writer allowlist, `AttributeError: module 'scheduler' has no attribute '_boot_storage'`, and `ValueError: substring not found` on the deploy script

- [ ] **Step 3: Write minimal implementation**

scheduler.py, after `_config_sha` (after line 112):
```python


def _boot_storage() -> None:
    """
    WAVE 3 (3a). The daemon is THE writer. Apply the schema once, then open
    every later connection writable; every other process on the box opens
    storage read-only (storage._WRITABLE defaults False) and cannot corrupt
    the file the daemon is writing. deploy_daemon.sh runs the same
    migrate() with the daemon stopped, so on a normal boot this is a no-op
    pass over IF NOT EXISTS statements.

    Also primes _config_sha() here, off the entry path: config._current_
    git_sha() shells out to git, and the first entry cycle after a boot
    used to pay for that subprocess (Wave 1 minor).
    """
    storage.migrate()
    storage.set_writable(True)
    sha = _config_sha()
    print(
        f"[scheduler] boot: storage migrated at {config.DB_PATH} "
        f"({storage.schema_summary()}); writable; config sha {sha or 'unknown'}."
    )
```

scheduler.py `run_forever` (line 750-753) — old:
```python
    groups = stations_by_utc_offset(station_icaos)
    if not groups:
        print("[scheduler] no registered stations to run -- nothing to do.")
        return
```
new:
```python
    _boot_storage()

    groups = stations_by_utc_offset(station_icaos)
    if not groups:
        print("[scheduler] no registered stations to run -- nothing to do.")
        return
```

manual_trigger.py `main()` (line 161-162) — old:
```python
def main():
    args = _parse_args()
```
new:
```python
def main():
    args = _parse_args()

    # WAVE 3 (3a): this script OPENS A POSITION, so it is one of the three
    # operator writers (with bucket_bias.py --ingest and main.py). Declared
    # up front, before any storage read, because the reads and the write
    # share one connection mode for the life of the process.
    storage.set_writable(True)
```

bucket_bias.py `__main__` (line 600-603) — old:
```python
    if args.ingest:
        written = ingest_settled_buckets([args.station], max_lookback_days=args.lookback,
                                         max_per_sweep=args.max_per_sweep)
        print(f"[bucket_bias] recorded {written} new settlement(s) for {args.station}.")
```
new:
```python
    if args.ingest:
        # WAVE 3 (3a): --ingest writes settled_buckets; the report path
        # below stays read-only. Only this branch declares the process a writer.
        storage.set_writable(True)
        written = ingest_settled_buckets([args.station], max_lookback_days=args.lookback,
                                         max_per_sweep=args.max_per_sweep)
        print(f"[bucket_bias] recorded {written} new settlement(s) for {args.station}.")
```

main.py (line 26-27 and `main()` line 30) — old:
```python
import config
from pipeline import run, print_summary


def main():
```
new:
```python
import config
import storage
from pipeline import run, print_summary


def main():
    # WAVE 3 (3a): pipeline.run() stores every forecast and ensemble spread
    # it fetches, exactly as the daemon's cycle does, so a one-off run is an
    # operator WRITE and says so. The rows it leaves are the same rows the
    # daemon would have written.
    storage.set_writable(True)
```

../deploy/deploy_daemon.sh generator block (line 59-79) — old:
```bash
    grep -q generate_realmoney_dashboard.py /etc/systemd/system/polyweather-dashboard.service \
      || echo "!! dashboard unit ExecStart does not invoke generate_realmoney_dashboard.py -- realmoney.html will not render; hand-edit the unit"
    sudo systemctl start polyweather-dashboard.service 2>/dev/null || true
fi
```
new:
```bash
    grep -q generate_realmoney_dashboard.py /etc/systemd/system/polyweather-dashboard.service \
      || echo "!! dashboard unit ExecStart does not invoke generate_realmoney_dashboard.py -- realmoney.html will not render; hand-edit the unit"
    # NOT started here any more (WAVE 3, 3a): the dashboard renders AFTER
    # the daemon restart, at the end of this script -- see "== dashboard ==".
fi
```

../deploy/deploy_daemon.sh tail (line 185-194) — old:
```bash
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE
# restart, not `enable --now`: --now is a no-op on an already-running service,
# which left every redeploy onto a live box running the OLD code (bit us 2026-08-07).
sudo systemctl restart $SERVICE

sleep 5
echo "== Service status =="
systemctl is-active $SERVICE
sudo journalctl -u $SERVICE -n 20 --no-pager
```
new:
```bash
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE

echo "== stop =="
# WAVE 3 (3a). storage._connect() no longer issues DDL; the schema is applied
# by storage.migrate(), which the daemon also runs at boot. Running it HERE,
# as ubuntu, with the daemon stopped, is what makes a schema-changing deploy
# an ordinary deploy: the backup is taken first, the ALTERs run with no other
# writer holding the file, and the daemon comes up on a migrated database.
# The dashboard timer is paused so no render lands mid-migration. This sits
# AFTER the demotion guard above on purpose: a refused deploy must leave the
# daemon running, not stopped.
sudo systemctl stop polyweather-dashboard.timer 2>/dev/null || true
sudo systemctl stop $SERVICE

echo "== backup =="
# sqlite's online backup API, never cp: a copy of a file with a hot journal is
# not a database. Keeps the three most recent; older ones are pruned.
DB="$PKG_DIR/data/polyweather.sqlite3"
if [ -f "$DB" ]; then
    STAMP=$(date -u +%Y%m%dT%H%M%SZ)
    "$VENV/bin/python" - "$DB" "$HOME/polyweather-pre-deploy-$STAMP.sqlite3" <<'PYBACKUP'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
print(f"backup written: {sys.argv[2]}")
PYBACKUP
    ls -1t "$HOME"/polyweather-pre-deploy-*.sqlite3 2>/dev/null | tail -n +4 | xargs -r rm -f
fi

echo "== migrate =="
# As ubuntu -- this script runs as ubuntu; do NOT sudo this line. A root-owned
# -journal or -wal file beside the database is exactly the corruption 3a/3b
# close. The one-line summary is the deploy log's proof of what was applied.
cd "$PKG_DIR"
"$VENV/bin/python" -c "import storage; storage.migrate(); print('migrate: ok --', storage.schema_summary())"

# restart, not `enable --now`: --now is a no-op on an already-running service,
# which left every redeploy onto a live box running the OLD code (bit us 2026-08-07).
sudo systemctl restart $SERVICE

echo "== dashboard =="
# AFTER the daemon, never before it. The generators run as ubuntu (WAVE 3, 3b,
# setup_dashboard.sh) and open the database read-only; starting them before
# the restart used to race a root-owned render against the migration. Skip
# silently if the dashboard was never set up on this box.
if [ -f /etc/systemd/system/polyweather-dashboard.service ]; then
    sudo systemctl start polyweather-dashboard.timer 2>/dev/null || true
    sudo systemctl start polyweather-dashboard.service 2>/dev/null || true
fi

sleep 5
echo "== Service status =="
systemctl is-active $SERVICE
sudo journalctl -u $SERVICE -n 20 --no-pager
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave3_writers_and_readers.py tests/test_realmoney_dashboard.py tests/test_wave1_entry_decisions_recorded.py -v` / Expected: PASS (the two existing `test_deploy_daemon_*` tests still find "generate_realmoney_dashboard.py", "polyweather-dashboard.service" and "hand-edit the unit")
- [ ] **Step 5: Run the full suite** — `pytest -q` / Expected: all pass, count >= 1865
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add scheduler.py manual_trigger.py bucket_bias.py main.py ../deploy/deploy_daemon.sh tests/test_wave3_writers_and_readers.py
git commit -m "Wave 3 (3a): daemon migrates at boot; three operator writers; deploy stops, backs up, migrates, restarts, then renders

scheduler._boot_storage() runs migrate(), sets the process writable and
primes _config_sha() (the Wave 1 minor) at the top of run_forever.
manual_trigger.py, bucket_bias.py --ingest and main.py declare their
writes; redeem.py is a reader and stays one. deploy_daemon.sh now stops
the daemon and the dashboard timer after the demotion guard, backs the
database up through sqlite's backup API, runs storage.migrate() as
ubuntu, restarts the daemon and only then starts the dashboard -- it
used to start the root dashboard service before the restart.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: 3b — the dashboard runs as ubuntu, into a directory ubuntu owns, reading only
**Files:** Modify `../deploy/setup_dashboard.sh` (whole file, 49 lines) / Test `tests/test_wave3_dashboard_as_ubuntu.py`
**Interfaces:** Consumes: Task 1 (generators are read-only by default: they never call `set_writable`) / Produces: an idempotent `setup_dashboard.sh` that writes `User=ubuntu`

**Why the same path.** nginx serves `/var/www/html`; every bookmark points at it. `chown -R ubuntu:ubuntu /var/www/html` once (idempotent) lets `os.replace(tmp, OUT)` in all three generators succeed as ubuntu — that needs write permission on the DIRECTORY, and the recursive chown also takes the existing root-owned `.html` files. The script used to `mv` the generators from `/home/ubuntu/*.py` (scp'd copies), which fails on a re-run; it now copies from the repo's `deploy/` like `deploy_daemon.sh` does, so re-running it IS the 3b deploy step. `journalctl -u polyweather` inside `generate_dashboard.py` needs journal read access as ubuntu: `usermod -aG systemd-journal ubuntu` (idempotent; the service picks the group up on its next start). `/proc/<pid>/environ` of the daemon (realmoney's Gate 2 probe) is readable by the same uid, which ubuntu is.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave3_dashboard_as_ubuntu.py
"""
Wave 3 item 3b. polyweather-dashboard.service ran as root (no User= line),
and its generators opened the trading database read-write with lazy DDL: a
root-owned journal beside a ubuntu-owned database, or a root-owned empty
file where the daemon expected its own, was one timer tick away. The unit
now runs as ubuntu, the web root belongs to ubuntu, and the generators are
pinned by AST to never declare themselves writers.
"""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SETUP = REPO / "deploy" / "setup_dashboard.sh"
GENERATORS = sorted((REPO / "deploy").glob("generate_*.py"))


def _service_block(script):
    start = script.index("polyweather-dashboard.service")
    block = script[start:]
    block = block[block.index("[Service]"):]
    return block[:block.index("UNIT")]


def test_the_dashboard_unit_runs_as_ubuntu():
    script = SETUP.read_text(encoding="utf-8")
    block = _service_block(script)
    assert "User=ubuntu" in block
    assert "Type=oneshot" in block
    exec_line = next(l for l in script.splitlines() if l.startswith("ExecStart="))
    assert "generate_dashboard.py --region asia" in exec_line   # unchanged command line


def test_the_web_root_is_handed_to_ubuntu_idempotently():
    script = SETUP.read_text(encoding="utf-8")
    assert "chown -R ubuntu:ubuntu /var/www/html" in script
    assert "usermod -aG systemd-journal ubuntu" in script
    # Re-runnable: the generators are copied from the repo, not mv'd from $HOME.
    assert "sudo mv /home/ubuntu/" not in script
    assert 'sudo cp "$APP_DIR/deploy/$gen" /usr/local/bin/$gen' in script


def test_three_generators_exist_and_none_is_a_writer():
    assert [g.name for g in GENERATORS] == [
        "generate_backtest_dashboard.py", "generate_dashboard.py", "generate_realmoney_dashboard.py",
    ]
    for gen in GENERATORS:
        tree = ast.parse(gen.read_text(encoding="utf-8"))
        offenders = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Attribute) and n.func.attr in ("set_writable", "migrate", "_connect", "_db"))
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "connect"
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "sqlite3")
            )
        ]
        assert not offenders, f"{gen.name} touches storage beyond its public readers at lines {offenders}"
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave3_dashboard_as_ubuntu.py -v` / Expected: FAIL on `"User=ubuntu" in block`, on `chown -R`, and on `"sudo mv /home/ubuntu/" not in script`; the generator AST test passes already (pinning, not changing)

- [ ] **Step 3: Write minimal implementation** — replace `../deploy/setup_dashboard.sh` (all 49 lines) with:

```bash
#!/usr/bin/env bash
# Serve the polyweather dashboard publicly: nginx + 5-min regeneration timer.
#
# IDEMPOTENT (WAVE 3, 3b). Re-run it whenever the unit below changes; every
# step tolerates having been done before. The first version mv'd the
# generators out of /home/ubuntu and could only be run once.
set -euo pipefail

APP_DIR=/home/ubuntu/weather-forecast
VENV_PY=$APP_DIR/.venv/bin/python
WEB_ROOT=/var/www/html

echo "== nginx =="
sudo apt-get install -y -q nginx >/dev/null
sudo rm -f $WEB_ROOT/index.nginx-debian.html

echo "== web root owned by ubuntu =="
# The generators run as ubuntu (User= below) and os.replace() their page into
# this directory. The path stays /var/www/html -- nginx serves it and every
# bookmark points at it -- and the recursive chown also takes the .html files
# an earlier root-run timer left behind.
sudo chown -R ubuntu:ubuntu /var/www/html

echo "== ubuntu may read the system journal =="
# generate_dashboard.py tails `journalctl -u polyweather`; as root that was
# free. Group membership is read by systemd when the service starts, so the
# next timer tick sees it.
sudo usermod -aG systemd-journal ubuntu

echo "== generators into place =="
# FROZEN COPIES in /usr/local/bin, refreshed from the repo here and on every
# deploy_daemon.sh run (a git pull alone leaves the page rendering old code).
for gen in generate_dashboard.py generate_backtest_dashboard.py generate_realmoney_dashboard.py; do
    sudo cp "$APP_DIR/deploy/$gen" /usr/local/bin/$gen
    sudo chmod 644 /usr/local/bin/$gen
done

echo "== systemd service + timer =="
# User=ubuntu (WAVE 3, 3b): the generators open the trading database READ-ONLY
# (storage opens mode=ro unless a process calls set_writable, which none of
# the three does -- tests/test_wave3_dashboard_as_ubuntu.py) and write only
# under $WEB_ROOT. Running them as root was how a root-owned file could land
# beside the daemon's ubuntu-owned database.
sudo tee /etc/systemd/system/polyweather-dashboard.service >/dev/null <<UNIT
[Unit]
Description=Regenerate polyweather status dashboard (paper trading + backtest lab)
After=network-online.target

[Service]
Type=oneshot
User=ubuntu
ExecStart=/bin/sh -c '$VENV_PY /usr/local/bin/generate_dashboard.py --region asia && $VENV_PY /usr/local/bin/generate_dashboard.py --region europe && $VENV_PY /usr/local/bin/generate_dashboard.py --region americas && $VENV_PY /usr/local/bin/generate_backtest_dashboard.py && $VENV_PY /usr/local/bin/generate_realmoney_dashboard.py'
UNIT

sudo tee /etc/systemd/system/polyweather-dashboard.timer >/dev/null <<UNIT
[Unit]
Description=Regenerate polyweather dashboard every 5 minutes

[Timer]
OnBootSec=90
OnUnitActiveSec=300

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now polyweather-dashboard.timer
sudo systemctl start polyweather-dashboard.service

echo "== first render =="
sudo systemctl status polyweather-dashboard.service --no-pager -n 5 | tail -3
systemctl show polyweather-dashboard.service -p User
ls -l $WEB_ROOT/*.html
echo "== local check =="
curl -s -o /dev/null -w "nginx says: HTTP %{http_code}, %{size_download} bytes\n" http://localhost/
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave3_dashboard_as_ubuntu.py tests/test_realmoney_dashboard.py -v` / Expected: PASS (the existing ExecStart assertions in test_realmoney_dashboard.py still hold: the line is unchanged)
- [ ] **Step 5: Run the full suite** — `pytest -q` / Expected: all pass, count >= 1868
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add ../deploy/setup_dashboard.sh tests/test_wave3_dashboard_as_ubuntu.py
git commit -m "Wave 3 (3b): dashboard timer runs as ubuntu into a ubuntu-owned /var/www/html

setup_dashboard.sh is idempotent now: generators copied from the repo,
chown -R ubuntu:ubuntu on the web root, ubuntu added to systemd-journal
for the journal tail, User=ubuntu on the oneshot unit. Same path, same
ExecStart, same URLs. An AST test pins that no generator ever calls
set_writable/migrate or opens sqlite itself: they read through storage's
public loaders, which open mode=ro.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: 3c — exits before entries; a past-dated position with no price asks Gamma at once
**Files:** Modify `config.py` (append the `WAVE 3` block after `REGIME_BOUNDARIES`, line 5133), `scheduler.py` (`_run_full_cycle` 457-462 and 565-581), `position_manager.py` (317-338) / Test `tests/test_wave3_exit_check_first.py`
**Interfaces:** Produces: `config.EXIT_CHECK_BEFORE_ENTRIES: bool = True`, `config.PAST_DATED_GAMMA_ON_FIRST_FAILURE: bool = True` (and the `DST_AWARE_LOCAL_HOUR` constant Task 5 reads, declared here so the block ships once)

**The 2026-09-02 journal case (scheduler-timing review).** Five live WSSS positions sat at `REGION_LIVE_MAX_CONCURRENT_POSITIONS["asia"] = 5`; one was yesterday's, already resolved. `_run_full_cycle` ran the EV leg and `executor.open_position` → `_live_budget_breach` counted five open live rows and refused `region_concurrent`; one second later `_run_exit_check` closed the resolved row. Every 05:00–05:20 SGT tick repeated it. With the exit check first, the resolved row is closed before the count is taken.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave3_exit_check_first.py
"""
Wave 3 item 3c. (i) _run_full_cycle runs the exit check BEFORE the EV/entry
leg, so a position that resolved overnight frees its slot before the entry
count is taken (the 2026-09-02 journal case: five live positions at the
region cap, one of them yesterday's, every 05:00-05:20 tick refused). As a
side effect the exit check also runs when pipeline.run() fails, which used
to skip it. (ii) A past-dated position whose price fetch fails asks Gamma on
the FIRST failure, not the third: its market day is over, so there is
nothing a second or third blind cycle can learn.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import config
import entry_manager
import ev_engine
import executor
import position_manager
import scheduler
import storage
from clients import market_client, wallet_client
from models import EVResult, ExitDecision, Position

STATION = "WSSS"
TARGET = date(2026, 9, 17)


def _ev(bucket=32, side="YES", model_prob=0.55, price=0.35):
    return EVResult(
        station_icao=STATION, target_date=TARGET, bucket_c=bucket, side=side,
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02,
        net_ev_per_dollar=(model_prob - price) / price - 0.03, spread_source="ensemble",
        market_bid=price - 0.02,
    )


def _live_position(pid, target_date):
    return Position(
        position_id=pid, station_icao=STATION, target_date=target_date, bucket_c=30 + int(pid[-1]),
        side="YES", entry_price=0.30, size_usd=1.0, entry_time=f"{target_date}T21:00:00+00:00",
        status="open", high_water_mark=0.30, is_paper=False, execution_mode="live", token_id=f"t{pid}",
    )


@pytest.fixture
def cycle(monkeypatch, tmp_db):
    """
    _run_full_cycle with the network stubbed and the REAL entry path running
    for a graduated WSSS; storage is the migrated tmp_db (NOT stubbed, so the
    live count below is a real read). Records the order of the exit check,
    the decision recording and the executor call, and what the real
    executor._live_budget_breach says at each open_position call.
    """
    seen = {"order": [], "breach": []}
    monkeypatch.setattr(executor, "EXECUTION_MODE", {icao: "paper" for icao in config.STATIONS})
    monkeypatch.setattr(scheduler.pipeline, "run",
                        lambda station_icao, forecast_bias_c=0.0: {"estimate": SimpleNamespace(inputs_used=["open_meteo_ecmwf"])})
    monkeypatch.setattr(scheduler.pipeline, "print_summary", lambda r: None)
    monkeypatch.setattr(ev_engine, "save_ev_snapshot", lambda icao, results: None)
    ev_run = ev_engine.StationEVRun(
        station_icao=STATION, target_date=TARGET,
        token_map={b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in (31, 32, 33)},
        bucket_min_c=31, bucket_max_c=33, ev_results=[_ev(32)],
    )
    monkeypatch.setattr(ev_engine, "run_for_station_with_map", lambda estimate, **kw: ev_run)
    monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.1, 20, 0.1))
    monkeypatch.setattr(entry_manager, "resolution_obs_count", lambda icao: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY)
    monkeypatch.setattr(entry_manager, "forecast_bias_source_mix", lambda icao: None)
    monkeypatch.setattr(entry_manager, "station_error_width_ratio", lambda icao: None)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    monkeypatch.setattr(wallet_client, "reconcile_cached",
                        lambda positions, **_: wallet_client.Reconciliation(ok=True, checked=True, reason="stubbed"))
    real_record = storage.record_entry_decisions
    monkeypatch.setattr(storage, "record_entry_decisions",
                        lambda decisions, **kw: seen["order"].append("record") or real_record(decisions, **kw))

    def _open(decision):
        seen["order"].append("open")
        seen["breach"].append(executor._live_budget_breach(1.0, STATION))

    monkeypatch.setattr(executor, "open_position", _open)
    return seen


def _stub_exit_check_that_closes_yesterday(monkeypatch, seen):
    """What position_manager does to a resolved past-dated row, minus the network."""
    def _exit(station_icao, interval_min=None):
        seen["order"].append("exit")
        for p in storage.load_open_positions(station_icao=station_icao, is_paper=False):
            if p.target_date < TARGET:
                storage.close_position(p.position_id, 1.0, "2026-09-16T21:00:05+00:00",
                                       "closed_resolution", "market_resolved")
    monkeypatch.setattr(scheduler, "_run_exit_check", _exit)


def _five_live_one_resolved():
    for i in range(1, 5):
        storage.open_position(_live_position(f"p{i}", TARGET))
    storage.open_position(_live_position("p5", date(2026, 9, 16)))   # yesterday's


def test_the_exit_check_runs_first(cycle, monkeypatch):
    _stub_exit_check_that_closes_yesterday(monkeypatch, cycle)
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)
    assert cycle["order"] == ["exit", "record", "open"]


def test_the_2026_09_02_case_a_resolved_slot_is_free_before_the_count(cycle, monkeypatch):
    """THE CHANGE. Five live rows at the asia cap of 5, one resolved yesterday."""
    assert config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["asia"] == 5
    _five_live_one_resolved()
    _stub_exit_check_that_closes_yesterday(monkeypatch, cycle)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert cycle["order"] == ["exit", "record", "open"]
    assert cycle["breach"] == [None]                      # 4 open < 5: admitted
    assert len(storage.load_open_positions(is_paper=False)) == 4


def test_flag_off_reproduces_the_blocked_slot(cycle, monkeypatch):
    monkeypatch.setattr(config, "EXIT_CHECK_BEFORE_ENTRIES", False)
    _five_live_one_resolved()
    _stub_exit_check_that_closes_yesterday(monkeypatch, cycle)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert cycle["order"] == ["record", "open", "exit"]
    assert cycle["breach"][0] is not None and "REGION_LIVE_MAX_CONCURRENT_POSITIONS" in cycle["breach"][0]


def test_a_pipeline_failure_no_longer_skips_the_exit_check(cycle, monkeypatch):
    monkeypatch.setattr(scheduler.pipeline, "run",
                        lambda station_icao, forecast_bias_c=0.0: (_ for _ in ()).throw(RuntimeError("forecast outage")))
    monkeypatch.setattr(scheduler, "_run_exit_check",
                        lambda station_icao, interval_min=None: cycle["order"].append(("exit", interval_min)))
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)
    assert cycle["order"] == [("exit", None)]              # exits ran; nothing captured (interval None)


def test_flag_off_pipeline_failure_still_skips_exits_as_before(cycle, monkeypatch):
    monkeypatch.setattr(config, "EXIT_CHECK_BEFORE_ENTRIES", False)
    monkeypatch.setattr(scheduler.pipeline, "run",
                        lambda station_icao, forecast_bias_c=0.0: (_ for _ in ()).throw(RuntimeError("forecast outage")))
    monkeypatch.setattr(scheduler, "_run_exit_check",
                        lambda station_icao, interval_min=None: cycle["order"].append("exit"))
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)
    assert cycle["order"] == []


# --- (ii) past-dated + no price -> Gamma on the first failure ----------------

@pytest.fixture
def blind(monkeypatch):
    calls = {"gamma": 0}
    monkeypatch.setattr(market_client, "get_current_price_for_side", lambda token_id, side: None)
    monkeypatch.setattr(position_manager, "_market_reported_closed",
                        lambda position: calls.__setitem__("gamma", calls["gamma"] + 1) or True)
    monkeypatch.setattr(position_manager, "_close_resolved_without_price",
                        lambda position, token_id: ExitDecision(position_id=position.position_id, should_exit=True,
                                                                reason="resolution", current_price=1.0, pnl_pct=2.0))
    position_manager._consecutive_price_failures.clear()
    return calls


def test_a_past_dated_position_asks_gamma_on_the_first_failure(blind):
    pos = _live_position("p9", date(2026, 1, 1))
    decision = position_manager._check_one_position(pos)
    assert blind["gamma"] == 1
    assert decision is not None and decision.reason == "resolution"


def test_a_same_day_position_still_waits_three_failures(blind, monkeypatch):
    monkeypatch.setattr(position_manager, "_local_today_for", lambda position: TARGET)
    pos = _live_position("p8", TARGET)
    assert position_manager._check_one_position(pos) is None
    assert position_manager._check_one_position(pos) is None
    assert blind["gamma"] == 0
    position_manager._check_one_position(pos)
    assert blind["gamma"] == 1


def test_flag_off_a_past_dated_position_waits_three_failures(blind, monkeypatch):
    monkeypatch.setattr(config, "PAST_DATED_GAMMA_ON_FIRST_FAILURE", False)
    pos = _live_position("p7", date(2026, 1, 1))
    assert position_manager._check_one_position(pos) is None
    assert position_manager._check_one_position(pos) is None
    assert blind["gamma"] == 0
    position_manager._check_one_position(pos)
    assert blind["gamma"] == 1
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave3_exit_check_first.py -v` / Expected: FAIL: `assert ['record', 'open', 'exit'] == ['exit', 'record', 'open']`, `assert ['...REGION_LIVE_MAX_CONCURRENT_POSITIONS...'] == [None]`, `assert [] == [('exit', None)]`, `assert 0 == 1` on the first-failure Gamma test; the two `flag off` scheduler tests fail with `AttributeError: EXIT_CHECK_BEFORE_ENTRIES` on the monkeypatch (raising) until the constant exists

- [ ] **Step 3: Write minimal implementation**

config.py, appended after `REGIME_BOUNDARIES: tuple = ("2026-09-20",)` (line 5133):
```python


# ===========================================================================
# WAVE 3 (~2026-09-25) -- STOP THE DATA FROM CORRUPTING ITSELF.
# docs/superpowers/specs/2026-09-17-evidence-first-remediation-design.md
#
# No statistic changes in this wave, so there is NO REGIME_BOUNDARIES stamp.
# 3a (storage read-only by default, storage.migrate() the only DDL path) and
# 3b (dashboard as ubuntu) are not flagged: they are the point. The three
# flags below default ON and are read at exactly one site each; False
# restores the pre-Wave-3 path at that site.
# ===========================================================================

# 3c. THE EXIT CHECK RUNS BEFORE THE ENTRY LEG. Read by scheduler._run_full_
# cycle. Journal-confirmed 2026-09-02: five live positions at the asia
# concurrent cap, one of them yesterday's and already resolved; every
# 05:00-05:20 SGT tick took the count (5) BEFORE the exit check closed the
# resolved row, and refused the entry. Exits first also means a pipeline.run
# failure no longer skips the station's exit check for that cycle.
EXIT_CHECK_BEFORE_ENTRIES = True

# 3c. A PAST-DATED POSITION WITH NO PRICE ASKS GAMMA ON THE FIRST FAILURE.
# Read by position_manager._check_one_position. Its market day is over, so
# the second and third blind cycles UNMONITORABLE_CYCLES_WARN used to demand
# could learn nothing a resolution lookup cannot answer now. Same-day
# positions keep the three-failure counter: a live market with a broken
# feed is a feed problem, not a resolution.
PAST_DATED_GAMMA_ON_FIRST_FAILURE = True

# 3d. THE EXIT PATH AND THE REPLAY USE THE DST-AWARE OFFSET. Read by
# position_manager._local_hour_for, risk_manager._station_offset_now and
# backtest/simclock.utc_offset_for (which engine.run threads per day). With
# the static registry int, every European station's 10:00 edge-decay
# tightening fired at 11:00 true local all summer (scheduler-timing review
# 2026-09-15, production-confirmed), and the replay agreed with the error.
DST_AWARE_LOCAL_HOUR = True
```

scheduler.py `_run_full_cycle` (line 457-462) — old:
```python
    import entry_manager

    # A full cycle records everything a collection pass does, so it counts
    # as one: without this the first monitor cycle after 08:00 would
    # re-collect a station the 07:5x entry cycle had just recorded.
    _last_collection_ts[station_icao] = time.time()
```
new:
```python
    import entry_manager

    # A full cycle records everything a collection pass does, so it counts
    # as one: without this the first monitor cycle after 08:00 would
    # re-collect a station the 07:5x entry cycle had just recorded.
    _last_collection_ts[station_icao] = time.time()

    # WAVE 3 (3c): EXITS FIRST. A position that resolved overnight must be
    # closed before the entry leg counts open positions against the caps
    # (config.EXIT_CHECK_BEFORE_ENTRIES has the 2026-09-02 case). interval_min
    # is None here for the reason given at the bottom of this function.
    if config.EXIT_CHECK_BEFORE_ENTRIES:
        _run_exit_check(station_icao, interval_min=None)
```

scheduler.py `_run_full_cycle` (line 565-581) — old:
```python
    # DELIBERATELY None: no exit-path capture inside an entry window.
    #
    # ev_engine.run_for_station_with_map() above already captured both sides of every
    # bucket this cycle, WITH ask and periodic depth, so an exit-path row
    # here would be strictly poorer duplicate data. Worse than useless: the
    # exit check runs seconds AFTER the EV leg, so its ask-less row is the
    # NEWER one, and get_price_at() returns the newest row before an
    # instant. A replay pricing an entry on any later tick would then find
    # ask_price NULL and fall back to the bid -- overstating raw edge by the
    # spread, which is precisely what the 2026-08-10 entry-pricing fix
    # removed (see engine._entry_price and its n_entry_priced_bid_fallback
    # counter). The exact-ts tie-break in get_price_at() does NOT save us;
    # these two rows land a few seconds apart, not on the same second.
    #
    # The gap this whole change exists to close is in monitor_only/risk_only,
    # where nothing captures at all. That is the only place it should write.
    _run_exit_check(station_icao, interval_min=None)
```
new:
```python
    # DELIBERATELY None on the exit check in an entry window: no exit-path
    # capture here, whichever end of the cycle it runs at.
    #
    # ev_engine.run_for_station_with_map() captures both sides of every
    # bucket this cycle, WITH ask and periodic depth, so an exit-path row
    # would be strictly poorer duplicate data. Worse than useless when the
    # exit check ran AFTER the EV leg (every cycle before Wave 3, and still
    # the EXIT_CHECK_BEFORE_ENTRIES=False path below): its ask-less row was
    # the NEWER one, and get_price_at() returns the newest row before an
    # instant, so a replay pricing an entry on any later tick found
    # ask_price NULL and fell back to the bid -- overstating raw edge by the
    # spread, precisely what the 2026-08-10 entry-pricing fix removed (see
    # engine._entry_price and its n_entry_priced_bid_fallback counter). With
    # exits first the row would be the OLDER one and harmless, but there is
    # still nothing to gain from writing it. The gap this capture exists to
    # close is in monitor_only/risk_only, where nothing captures at all.
    if not config.EXIT_CHECK_BEFORE_ENTRIES:
        _run_exit_check(station_icao, interval_min=None)
```

position_manager.py `_check_one_position` (line 317-338) — old:
```python
    if current_price is None:
        failures = _note_price_failure(position)
        # A market can resolve while its price feed is down, and a
        # position we can't price is one we'd otherwise never see
        # resolve. Once blind for long enough, ask Gamma directly.
        if failures >= UNMONITORABLE_CYCLES_WARN:
            reported_closed = _market_reported_closed(position)
            if reported_closed is True:
                return _close_resolved_without_price(position, token_id)
            # Gamma can't say (lookup failed, or the bucket is no longer
            # listed -- which is itself what a settled event looks like)
            # AND the position's own market day is over. Two independent
            # feeds are down; the observation record is not, and it is the
            # authority both of them were only ever proxies for. Still
            # returns None and stays loud if no settlement-grade reading
            # exists. Gamma reporting OPEN is NOT overridden here: a live
            # market with a broken price feed is a feed problem, and
            # closing it on the weather would settle a position that can
            # still trade.
            if reported_closed is None and position.target_date < _local_today_for(position):
                return _close_from_settlement_source(position, gamma_closed=None)
        return None
```
new:
```python
    if current_price is None:
        failures = _note_price_failure(position)
        # A market can resolve while its price feed is down, and a
        # position we can't price is one we'd otherwise never see
        # resolve. Once blind for long enough, ask Gamma directly -- and,
        # WAVE 3 (3c), at ONCE for a bucket whose market day is already
        # over: a past-dated position is resolved by definition, so a
        # second and third blind cycle buy nothing but two more cycles of
        # a live slot held by a settled market (the 2026-09-02 journal).
        past_dated = position.target_date < _local_today_for(position)
        ask_gamma_now = failures >= UNMONITORABLE_CYCLES_WARN or (
            config.PAST_DATED_GAMMA_ON_FIRST_FAILURE and past_dated
        )
        if ask_gamma_now:
            reported_closed = _market_reported_closed(position)
            if reported_closed is True:
                return _close_resolved_without_price(position, token_id)
            # Gamma can't say (lookup failed, or the bucket is no longer
            # listed -- which is itself what a settled event looks like)
            # AND the position's own market day is over. Two independent
            # feeds are down; the observation record is not, and it is the
            # authority both of them were only ever proxies for. Still
            # returns None and stays loud if no settlement-grade reading
            # exists. Gamma reporting OPEN is NOT overridden here: a live
            # market with a broken price feed is a feed problem, and
            # closing it on the weather would settle a position that can
            # still trade.
            if reported_closed is None and past_dated:
                return _close_from_settlement_source(position, gamma_closed=None)
        return None
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave3_exit_check_first.py tests/test_exit_snapshot_capture.py tests/test_wave1_entry_decisions_recorded.py tests/test_wave1_shadow_pass.py tests/test_cycle_calibration.py tests/test_collection_window.py -v` / Expected: PASS (the shadow-pass call-graph guard walks `_run_full_cycle` and finds no new forbidden call; `test_full_cycle_exit_check_passes_no_fidelity` still sees `interval_min=None`)
- [ ] **Step 5: Run the full suite** — `pytest -q` / Expected: all pass, count >= 1876
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add config.py scheduler.py position_manager.py tests/test_wave3_exit_check_first.py
git commit -m "Wave 3 (3c): exit check before the entry leg; past-dated no-price positions ask Gamma at once

_run_full_cycle runs _run_exit_check first under EXIT_CHECK_BEFORE_ENTRIES,
so a position resolved overnight frees its cap slot before the entry
count is taken (the 2026-09-02 journal: five live at the asia cap, one
resolved, every 05:00-05:20 tick refused) and a pipeline.run failure no
longer skips the station's exits. position_manager consults Gamma on the
FIRST failed price fetch for a past-dated position under
PAST_DATED_GAMMA_ON_FIRST_FAILURE; same-day positions keep the
three-failure counter. The WAVE 3 config block also declares
DST_AWARE_LOCAL_HOUR for the next task.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: 3d — the exit hour and the replay clock are DST-aware
**Files:** Modify `position_manager.py` (`_local_hour_for` 487-495; the comment at 449-455), `risk_manager.py` (`_local_hour` 161-169; `_active_thresholds` 171-184; `evaluate_exit` 355-364), `backtest/simclock.py` (imports 42-45; after `tz_for` 60; `SimClock.__init__` end 117), `backtest/engine.py` (486; 599; 665), `backtest/settings.py` (36-38), `config.py` (602-604) / Test `tests/test_wave3_dst_local_hour.py`
**Interfaces:** Consumes: `config.DST_AWARE_LOCAL_HOUR` (Task 4) / Produces: `risk_manager._local_hour(tz_offset_hours: int)` (REQUIRED, no default), `risk_manager._station_offset_now(station_icao) -> int`, `risk_manager._active_thresholds(local_hour: int)` (required), `backtest.simclock.utc_offset_for(station, day: date) -> int`, `SimClock.retune(utc_offset_hours: int) -> None`

**The `_local_hour` default: REMOVED, and why.** Fact 3: no production caller reaches it. A default that nothing reaches but that would silently apply Singapore's clock to a London position if a future caller omitted `local_hour` is exactly the "station-agnostic fallback" the scheduler review found firing an hour late. `evaluate_exit(local_hour=None)` now resolves the hour from the POSITION's station (`_station_offset_now`), which is what `tests/test_parity_exit.py` was demonstrating with a WSSS position; that test keeps passing unchanged (WSSS has no `iana_timezone`, so DST-aware and static agree at 8). `_active_thresholds` loses its `None` path for the same reason.

**The replay.** `engine.run` resolved ONE static offset per run (engine.py:486). It now resolves the offset per LOCAL DAY through `simclock.utc_offset_for(station, day)` — `config.current_utc_offset_hours` at UTC midnight of that day, the same anchor `config.local_day_bounds_utc` uses (config.py:143-158), so a replay spanning the October transition keys each half on the right clock. The `SimClock` is retuned at each day boundary; `generate_ticks` and `local_minute_to_ts` already take the offset per call. The manifest's `sim_utc_offset_hours` records the start day's offset (RJTT stays 9, the existing test holds).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave3_dst_local_hour.py
"""
Wave 3 item 3d. EGLC is UTC+0 in the registry and UTC+1 (BST) in September.
config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL = 10 must fire at 10:00 BST = 09:00Z,
not at 10:00Z = 11:00 BST -- live (position_manager._local_hour_for,
risk_manager.evaluate_exit with no hour supplied) AND in the replay
(backtest/simclock via engine.run's per-day offset). Verified against the
current code first: a live EGLC position at entry 0.40 and price 0.52 is a
take_profit at local hour 10 and a hold at 9 (the tightened take is 25% of
the risk unit, the loose one 50%).
"""
from datetime import date, datetime, timezone

import pytest

import config
import position_manager
import risk_manager
from backtest import simclock
from models import Position

SEPT_09Z = datetime(2026, 9, 15, 9, 0, 0, tzinfo=timezone.utc)   # 10:00 BST
DEC_09Z = datetime(2026, 12, 15, 9, 0, 0, tzinfo=timezone.utc)   # 09:00 GMT


class _FakeDateTime:
    fixed = SEPT_09Z

    @classmethod
    def now(cls, tz=None):
        return cls.fixed if tz is None else cls.fixed.astimezone(tz)


def _eglc(**kw):
    d = dict(position_id="EGLC:x", station_icao="EGLC", target_date=date(2026, 9, 15), bucket_c=25,
             side="YES", entry_price=0.40, size_usd=10.0, entry_time="2026-09-15T05:00:00+00:00",
             status="open", high_water_mark=0.40, is_paper=False, execution_mode="live", entry_bid=0.38)
    d.update(kw)
    return Position(**d)


@pytest.fixture
def at_0900z(monkeypatch):
    monkeypatch.setattr(_FakeDateTime, "fixed", SEPT_09Z)
    monkeypatch.setattr(position_manager, "datetime", _FakeDateTime)
    monkeypatch.setattr(risk_manager, "datetime", _FakeDateTime)
    monkeypatch.setattr(config, "_now_utc", lambda: SEPT_09Z)


# --- live -------------------------------------------------------------------

def test_egl_local_hour_is_10_at_0900z_in_september(at_0900z):
    assert position_manager._local_hour_for(_eglc()) == 10


def test_flag_off_restores_the_static_registry_hour(at_0900z, monkeypatch):
    monkeypatch.setattr(config, "DST_AWARE_LOCAL_HOUR", False)
    assert position_manager._local_hour_for(_eglc()) == 9


def test_in_december_both_say_9(monkeypatch):
    monkeypatch.setattr(_FakeDateTime, "fixed", DEC_09Z)
    monkeypatch.setattr(position_manager, "datetime", _FakeDateTime)
    assert position_manager._local_hour_for(_eglc(target_date=date(2026, 12, 15))) == 9


def test_a_non_dst_station_is_unchanged(at_0900z):
    assert position_manager._local_hour_for(_eglc(station_icao="WSSS")) == 17


def test_the_tightening_fires_at_10_true_local_when_no_hour_is_passed(at_0900z):
    """evaluate_exit with local_hour=None resolves the POSITION's station."""
    assert risk_manager.evaluate_exit(_eglc(), 0.52).reason == "take_profit"
    assert risk_manager.evaluate_exit(_eglc(), 0.52, local_hour=10).reason == "take_profit"
    assert risk_manager.evaluate_exit(_eglc(), 0.52, local_hour=9).reason == "hold"


def test_before_10_true_local_the_loose_take_still_holds(monkeypatch):
    """08:00Z is 09:00 BST: loose thresholds, hold. On the pre-Wave-3 code
    the dead UTC+8 default read this instant as 16:00 and took profit --
    this is the assertion that separates the station's clock from
    Singapore's, which the 09:00Z case above cannot (17:00 is tightened too)."""
    monkeypatch.setattr(_FakeDateTime, "fixed", datetime(2026, 9, 15, 8, 0, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(position_manager, "datetime", _FakeDateTime)
    monkeypatch.setattr(risk_manager, "datetime", _FakeDateTime)
    assert position_manager._local_hour_for(_eglc()) == 9
    assert risk_manager.evaluate_exit(_eglc(), 0.52).reason == "hold"


def test_flag_off_the_default_hour_is_the_static_one(at_0900z, monkeypatch):
    monkeypatch.setattr(config, "DST_AWARE_LOCAL_HOUR", False)
    assert risk_manager.evaluate_exit(_eglc(), 0.52).reason == "hold"


def test_local_hour_has_no_default_offset_any_more():
    with pytest.raises(TypeError):
        risk_manager._local_hour()
    with pytest.raises(TypeError):
        risk_manager._active_thresholds()


def test_station_offset_now_falls_back_only_for_an_unregistered_station(at_0900z):
    assert risk_manager._station_offset_now("EGLC") == 1
    assert risk_manager._station_offset_now("WSSS") == 8
    assert risk_manager._station_offset_now("XXXX") == config.LOCAL_UTC_OFFSET_HOURS


# --- replay -----------------------------------------------------------------

def test_the_replay_offset_for_a_summer_egl_day_is_bst():
    assert simclock.utc_offset_for("EGLC", date(2026, 9, 15)) == 1
    assert simclock.utc_offset_for("EGLC", date(2026, 12, 15)) == 0
    assert simclock.utc_offset_for("RJTT", date(2026, 9, 15)) == 9


def test_flag_off_the_replay_offset_is_the_registry_int(monkeypatch):
    monkeypatch.setattr(config, "DST_AWARE_LOCAL_HOUR", False)
    assert simclock.utc_offset_for("EGLC", date(2026, 9, 15)) == 0


def test_the_replay_local_hour_matches_live_at_0900z(at_0900z):
    day = date(2026, 9, 15)
    offset = simclock.utc_offset_for("EGLC", day)
    ts = simclock.local_minute_to_ts(day, 10 * 60, offset)
    clock = simclock.SimClock(ts, utc_offset_hours=offset)
    assert clock.utc_datetime() == SEPT_09Z                 # 10:00 local IS 09:00Z
    assert clock.local_hour() == 10 == position_manager._local_hour_for(_eglc())


def test_retune_changes_the_reported_hour_not_the_instant():
    clock = simclock.SimClock(int(SEPT_09Z.timestamp()), utc_offset_hours=1)
    assert clock.local_hour() == 10
    clock.retune(0)
    assert clock.local_hour() == 9 and clock.ts == int(SEPT_09Z.timestamp())


def test_engine_threads_the_per_day_offset(monkeypatch, tmp_path, tmp_db):
    """A one-day EGLC run in September records BST in its manifest and
    generates its first tick at 04:00 BST = 03:00Z."""
    import contextlib
    import io
    from backtest import engine, settings

    with contextlib.redirect_stdout(io.StringIO()):
        run = engine.run(
            station_icao="EGLC", start_date=date(2026, 9, 15), end_date=date(2026, 9, 15),
            depth_regime="strict", fee_rate_pct=0.0, bankroll_mode="static",
            market_db_path=str(tmp_path / "market.sqlite3"),
        )
    assert run.manifest["backtest_settings"]["sim_utc_offset_hours"] == 1
    assert run.manifest["station_config"]["utc_offset_hours"] == 0
    first_tick = simclock.generate_ticks(date(2026, 9, 15), 1)[0]
    assert datetime.fromtimestamp(first_tick.ts, timezone.utc).hour == settings.SIM_DAY_START_HOUR_LOCAL - 1
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave3_dst_local_hour.py -v` / Expected: FAIL: `assert 9 == 10` (live hour at 09:00Z), `'take_profit' == 'hold'` (the 08:00Z case: HEAD's dead UTC+8 default reads it as 16:00), `AttributeError: module 'backtest.simclock' has no attribute 'utc_offset_for'`, the two `TypeError` expectations fail because the defaults still exist, the flag-off tests fail on `monkeypatch.setattr(config, "DST_AWARE_LOCAL_HOUR", ...)` only if Task 4 was skipped. (`test_the_tightening_fires_at_10_true_local_when_no_hour_is_passed` passes on HEAD by coincidence -- 17:00 SGT is tightened too -- and is kept as the regression pin; the 08:00Z test is the discriminating one.)

- [ ] **Step 3: Write minimal implementation**

position_manager.py `_local_hour_for` (line 487-495) — old:
```python
def _local_hour_for(position: Position) -> int:
    """
    Current hour (0-23) in the position's own market timezone -- what
    risk_manager's edge-decay tightening must be evaluated against, since
    "10:00 local" is a different instant in Tokyo, Singapore and Karachi.
    """
    station = _station_for(position)
    offset = station.utc_offset_hours if station is not None else config.LOCAL_UTC_OFFSET_HOURS
    return (datetime.now(timezone.utc).hour + offset) % 24
```
new:
```python
def _local_hour_for(position: Position) -> int:
    """
    Current hour (0-23) in the position's own market timezone -- what
    risk_manager's edge-decay tightening must be evaluated against, since
    "10:00 local" is a different instant in Tokyo, Singapore and Karachi.

    WAVE 3 (3d): DST-AWARE. The static station.utc_offset_hours is the
    STANDARD-time value, so every European position tightened at 11:00
    true local all summer. config.current_utc_offset_hours resolves the
    offset at THIS instant for a station carrying an iana_timezone and
    returns the static int for every other station, so Asia is unchanged.
    DST_AWARE_LOCAL_HOUR=False restores the static read.
    """
    station = _station_for(position)
    now = datetime.now(timezone.utc)
    if station is None:
        offset = config.LOCAL_UTC_OFFSET_HOURS
    elif config.DST_AWARE_LOCAL_HOUR:
        offset = config.current_utc_offset_hours(station, at=now)
    else:
        offset = station.utc_offset_hours
    return (now.hour + offset) % 24
```

position_manager.py comment (line 449-455) — old:
```python
    # local_hour is passed EXPLICITLY, from this position's own station
    # offset. risk_manager._local_hour()'s default is UTC+8 and stays
    # that way (a station-agnostic fallback the parity tests pin), so
    # leaving this argument off would apply Singapore's edge-decay
    # tightening hour to Tokyo and Karachi -- an hour early for +9, three
    # hours late for +5. Fixing the default alone would have been a
    # no-op precisely because this call site never passed one.
```
new:
```python
    # local_hour is passed EXPLICITLY, from this position's own station
    # offset (DST-aware since Wave 3). risk_manager.evaluate_exit would
    # resolve the same hour from the position if it were omitted -- its
    # old UTC+8 default is gone -- but passing it keeps the hour this
    # cycle acted on visible at the call site.
```

risk_manager.py `_local_hour` and `_active_thresholds` (line 161-184) — old:
```python
def _local_hour(tz_offset_hours: int = 8) -> int:
    """
    Current local hour for SGT/MYT (UTC+8), both frameworks' stations.
    Hardcoded offset rather than a timezone library dependency, since
    both WSSS and WMKK share this offset -- revisit if a station in a
    different timezone is added later.
    """
    utc_now = datetime.now(timezone.utc)
    return (utc_now.hour + tz_offset_hours) % 24


def _active_thresholds(local_hour: Optional[int] = None) -> dict:
    """
    Return the full threshold set appropriate for the given time of day.

    local_hour defaults to None, which reads the real wall clock via
    _local_hour() -- unchanged behaviour for every live caller. Passing an
    explicit hour (0-23) uses that instead, which is what a simulated
    replay needs: a backtest re-running a past morning must apply the
    thresholds that were active AT THAT SIMULATED HOUR, not whatever hour
    the backtest itself happens to be executed at.
    """
    if local_hour is None:
        local_hour = _local_hour()
    if local_hour >= config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL:
```
new:
```python
def _local_hour(tz_offset_hours: int) -> int:
    """
    Current local hour at a FIXED offset the caller resolved. WAVE 3 (3d):
    NO DEFAULT. The old UTC+8 default was a station-agnostic fallback that
    no production caller reached (position_manager, engine, take_sweep and
    entry_bar_sweep all pass local_hour explicitly), and a hidden one is
    how a DST station ends up tightening at 11:00 true local.
    """
    utc_now = datetime.now(timezone.utc)
    return (utc_now.hour + tz_offset_hours) % 24


def _station_offset_now(station_icao: str) -> int:
    """
    The offset evaluate_exit() uses when no local_hour is supplied: the
    position's OWN station, DST-aware under config.DST_AWARE_LOCAL_HOUR
    (config.current_utc_offset_hours at this instant), the static registry
    int otherwise. config.LOCAL_UTC_OFFSET_HOURS only for a station that is
    no longer registered -- the same fallback position_manager._station_for
    takes, so an orphaned row is evaluated, not raised on.
    """
    try:
        station = config.get_station(station_icao)
    except KeyError:
        return config.LOCAL_UTC_OFFSET_HOURS
    if config.DST_AWARE_LOCAL_HOUR:
        return config.current_utc_offset_hours(station, at=datetime.now(timezone.utc))
    return station.utc_offset_hours


def _active_thresholds(local_hour: int) -> dict:
    """
    Return the full threshold set appropriate for the given local hour
    (0-23). REQUIRED since Wave 3: the caller resolves the hour -- the
    replay from its simulated clock, the live path from the position's own
    station -- so a backtest re-running a past morning applies the
    thresholds active AT THAT SIMULATED HOUR, never the hour the backtest
    happens to be executed at.
    """
    if local_hour >= config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL:
```

risk_manager.py `evaluate_exit` (line 355-364) — old:
```python
    local_hour is threaded straight through to _active_thresholds():
    None (the default) reads the real wall clock exactly as before, and
    an explicit hour (0-23) pins the edge-decay tightening to a
    caller-supplied time. That makes the function fully pure when the
    hour is supplied -- a replay or a unit test can then evaluate the
```
new:
```python
    local_hour is threaded straight through to _active_thresholds():
    None (the default) reads the real wall clock AT THE POSITION'S OWN
    STATION (DST-aware since Wave 3, see _station_offset_now), and an
    explicit hour (0-23) pins the edge-decay tightening to a
    caller-supplied time. That makes the function fully pure when the
    hour is supplied -- a replay or a unit test can then evaluate the
```
and the call (line 364) — old:
```python
    thresholds = _active_thresholds(local_hour=local_hour)
```
new:
```python
    if local_hour is None:
        local_hour = _local_hour(_station_offset_now(position.station_icao))
    thresholds = _active_thresholds(local_hour=local_hour)
```

backtest/simclock.py imports (line 42-45) — old:
```python
import scheduler

from backtest import settings
```
new:
```python
import config
import scheduler

from backtest import settings
```
after `tz_for` (after line 60):
```python


def utc_offset_for(station, day: date) -> int:
    """
    The UTC offset to replay `station` at on LOCAL day `day`. WAVE 3 (3d):
    DST-AWARE -- config.current_utc_offset_hours at UTC midnight of that
    day, the same anchor config.local_day_bounds_utc uses, so a replay's
    local hour on a summer EGLC day is the hour the live daemon saw (and a
    run spanning the October transition keys each half on its own clock;
    engine.run calls this per day and retunes the SimClock). A station
    without an iana_timezone gets its static int, so Asia is unchanged.
    DST_AWARE_LOCAL_HOUR=False restores the static registry int for every
    station, which is what every run before Wave 3 used.
    """
    st = config.get_station(station) if isinstance(station, str) else station
    if not config.DST_AWARE_LOCAL_HOUR:
        return st.utc_offset_hours
    anchor = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return config.current_utc_offset_hours(st, at=anchor)
```
`SimClock`, after `__init__` (after line 117, `self.tz = tz_for(self.utc_offset_hours)`):
```python

    def retune(self, utc_offset_hours: int) -> None:
        """
        Change the offset this clock reports LOCAL time at, without moving
        the instant. A multi-day replay does this at each day boundary with
        simclock.utc_offset_for(), so the half of a run after a DST
        transition keys risk_manager's tightening and observation
        visibility on the right local hour.
        """
        self.utc_offset_hours = int(utc_offset_hours)
        self.tz = tz_for(self.utc_offset_hours)
```

backtest/engine.py (line 486) — old:
```python
    local_offset = station.utc_offset_hours
```
new:
```python
    # WAVE 3 (3d): the START day's offset, DST-aware; the day loop below
    # re-resolves it per day and retunes the clock. The manifest records
    # this one as sim_utc_offset_hours and the registry int beside it.
    local_offset = simclock.utc_offset_for(station, start_date)
```
(line 599) — old:
```python
        for tick in simclock.generate_ticks(day, local_offset):
```
new:
```python
        day_offset = simclock.utc_offset_for(station, day)
        clock.retune(day_offset)
        for tick in simclock.generate_ticks(day, day_offset):
```
(line 665) — old:
```python
    end_ts = simclock.local_minute_to_ts(end_date + timedelta(days=1), 0, local_offset)
```
new:
```python
    end_ts = simclock.local_minute_to_ts(
        end_date + timedelta(days=1), 0, simclock.utc_offset_for(station, end_date + timedelta(days=1)),
    )
```

backtest/settings.py (line 36-38) — old:
```python
# Still matches risk_manager._local_hour()'s tz_offset_hours default and
# scheduler.local_now()'s, both of which hardcode 8 as their own
# station-agnostic fallback. This value is what a caller gets when it names
```
new:
```python
# Still matches scheduler.local_now()'s default, which hardcodes 8 as its
# own station-agnostic fallback (risk_manager._local_hour lost its default in
# Wave 3: it resolves the position's own station). This value is what a
# caller gets when it names
```

config.py (line 602-604) — old:
```python
    #   iana_timezone=...     -- these cities observe DST. utc_offset_hours
    #                            is ALSO set, to the STANDARD-time value,
    #                            because backtest/engine.py reads it
    #                            directly and has no moving clock.
```
new:
```python
    #   iana_timezone=...     -- these cities observe DST. utc_offset_hours
    #                            is ALSO set, to the STANDARD-time value: it
    #                            is the DST_AWARE_LOCAL_HOUR=False fallback
    #                            (Wave 3, 3d) for position_manager, risk_
    #                            manager and backtest/simclock, all of which
    #                            resolve the live offset through
    #                            current_utc_offset_hours() by default.
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave3_dst_local_hour.py tests/test_parity_exit.py tests/test_simclock_per_station.py tests/test_determinism.py tests/test_no_lookahead.py tests/test_backtest_stack.py tests/test_take_sweep.py tests/test_stop_loss_audit.py -v` / Expected: PASS (`test_run_records_the_offset_it_actually_used` still sees 9 for RJTT; the WSSS synthetic scenario is offset 8 on both paths)
- [ ] **Step 5: Run the full suite** — `pytest -q` / Expected: all pass, count >= 1890
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add position_manager.py risk_manager.py backtest/simclock.py backtest/engine.py backtest/settings.py config.py tests/test_wave3_dst_local_hour.py
git commit -m "Wave 3 (3d): the exit hour and the replay clock are DST-aware

position_manager._local_hour_for and a new risk_manager._station_offset_now
resolve the position's offset through config.current_utc_offset_hours
under DST_AWARE_LOCAL_HOUR, so EGLC tightens at 10:00 BST (09:00Z), not
11:00. risk_manager._local_hour and _active_thresholds lose their UTC+8
default: nothing in production reached it. backtest/simclock.utc_offset_for
resolves the offset per local day at the same anchor local_day_bounds_utc
uses; engine.run threads it per day and retunes the SimClock, so a summer
EGLC replay keys the same local hour live saw.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 3e — stale-docs sweep
**Files:** Modify `risk_manager.py` (67-70, 449-450), `config.py` (1446-1449, 2538-2540), `models.py` (243-249, 269, 377-380), `executor.py` (1156), `storage.py` (`close_position` docstring, 1262) / Test `tests/test_wave3_stale_docs.py`
**Interfaces:** none (comments and docstrings only)

**Verified already done:** the `storage.py` day-filter comment the spec lists was corrected in Wave 2 (`_forecast_means_in_local_day` docstring, storage.py:694-712, now describes the fetch window and explains the old `date(fetched_at) <= target_date` comparison as history); `tests/test_wave2_error_sample_window.py::test_the_stale_lookahead_comments_are_corrected` pins it. No edit.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave3_stale_docs.py
"""
Wave 3 item 3e. The 2026-09-15 reviews listed comments that describe rules
that no longer exist: "both tighten" (only the take-profit tightens since
2026-08-18), "skip BOTH price-noise exits" (the lottery carve-out skips the
stop; the take is kept), and four references to a trailing stop removed
2026-08-17. Text is pinned so the corrections cannot rot back.
"""
import pathlib

PKG = pathlib.Path(__file__).resolve().parents[1]


def _src(name):
    return (PKG / name).read_text(encoding="utf-8")


def test_risk_manager_no_longer_claims_both_thresholds_tighten():
    src = _src("risk_manager.py")
    assert "Both tighten after the edge-decay hour" not in src
    assert "Only the TAKE-PROFIT tightens after the edge-decay hour" in src
    assert "skip BOTH price-noise exits" not in src
    assert "skip the STOP-LOSS" in src


def test_config_no_longer_claims_both_thresholds_tighten():
    src = _src("config.py")
    assert "After this local hour, tighten both thresholds" not in src
    assert "After this local hour, tighten the take-profit target" in src
    assert "10:00 the stop tightens from 30% to 15%" not in src
    assert "UNTIL 2026-08-18 the stop tightened at 10:00" in src


def test_models_describe_the_trailing_stop_as_history():
    src = _src("models.py")
    assert "-- drives the trailing stop" not in src
    assert "no exit rule reads it since the trailing stop was removed 2026-08-17" in src
    assert '"trailing_stop", "take_profit" (should_exit=True) and "trailing_active"' not in src
    assert '"closed_trailing_stop" -- HISTORICAL rows only' in src
    assert "The first two closed_* strings are derived" in src


def test_executor_and_storage_no_longer_reference_a_live_trailing_stop():
    assert "typical trailing-stop gain" not in _src("executor.py")
    assert "typical take-profit gain" in _src("executor.py")
    assert "'closed_trailing_stop' (historical rows only" in _src("storage.py")
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave3_stale_docs.py -v` / Expected: FAIL on every assertion that names replacement text

- [ ] **Step 3: Write minimal implementation**

risk_manager.py (line 67-70) — old:
```
Both tighten after the edge-decay hour (config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL,
10:00 local) -- consistent with the edge-decay analysis: once the morning's
edge window closes, there's no new information coming to justify riding out
volatility, so gains and losses should both be locked in faster.
```
new:
```
Only the TAKE-PROFIT tightens after the edge-decay hour
(config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL, 10:00 local): TIGHTENED_PROFIT_TAKE_PCT
0.25 against 0.50. The stop stopped tightening on 2026-08-18
(TIGHTENED_STOP_LOSS_PCT is defined AS STOP_LOSS_PCT; stop_loss_audit.py
scored the tightened stop at -21.49 USD for nothing measurable). The
edge-decay reasoning survives on the take side only: once the morning's edge
window closes, no new information justifies riding out a winner.
```
risk_manager.py (line 449-450) — old:
```python
    # Lottery-priced entries (see LOTTERY_PRICE_THRESHOLD in config.py)
    # skip BOTH price-noise exits. Below that entry price the threshold
```
new:
```python
    # Lottery-priced entries (see LOTTERY_PRICE_THRESHOLD in config.py)
    # skip the STOP-LOSS -- the one price-noise exit left since the trailing
    # stop was removed 2026-08-17; the take below is NOT skipped, and
    # memory's wsss-2026-08-19-trade note is what that asymmetry cost.
    # Below that entry price the threshold
```
config.py (line 1446-1449) — old:
```python
# After this local hour, tighten both thresholds (see risk_manager.py) --
# reflects the edge-decay curve: be quicker to lock in gains and quicker to
# cut losses, since there's no more new edge coming to justify holding
# through volatility.
```
new:
```python
# After this local hour, tighten the take-profit target (see risk_manager.py;
# the stop has NOT tightened since 2026-08-18, TIGHTENED_STOP_LOSS_PCT below)
# -- reflects the edge-decay curve: be quicker to lock in gains, since
# there's no more new edge coming to justify holding through volatility.
```
config.py (line 2538-2540) — old:
```python
    # ONE MEASUREMENT TRAP, since it will otherwise be rediscovered. At
    # 10:00 the stop tightens from 30% to 15% of the risk unit, which
    # RAISES the trigger price, so positions already sitting between the
```
new:
```python
    # ONE MEASUREMENT TRAP, since it will otherwise be rediscovered.
    # UNTIL 2026-08-18 the stop tightened at 10:00 from 30% to 15% of the
    # risk unit (rows before that date only; TIGHTENED_STOP_LOSS_PCT is now
    # the loose distance), which RAISED the trigger price, so positions
    # already sitting between the
```
models.py (line 243-249) — old:
```python
    #   "open"                 -- executor.open_position()
    #   "closed_take_profit"   -- executor.close_position(), from ExitDecision.reason
    #   "closed_stop_loss"     -- executor.close_position(), from ExitDecision.reason
    #   "closed_trailing_stop" -- executor.close_position(), from ExitDecision.reason
    #   "closed_resolution"    -- position_manager._close_as_resolved(), passed explicitly
    # The first three closed_* strings are derived as f"closed_{decision.reason}"
    # from the reasons risk_manager.evaluate_exit() sets should_exit=True on, so
```
new:
```python
    #   "open"                 -- executor.open_position()
    #   "closed_take_profit"   -- executor.close_position(), from ExitDecision.reason
    #   "closed_stop_loss"     -- executor.close_position(), from ExitDecision.reason
    #   "closed_trailing_stop" -- HISTORICAL rows only: the trailing stop was removed
    #                             2026-08-17, nothing writes it since, but old rows carry
    #                             it and config.COOLDOWN_COUNTED_EXIT_STATUSES still matches it
    #   "closed_resolution"    -- position_manager._close_as_resolved(), passed explicitly
    # The first two closed_* strings are derived as f"closed_{decision.reason}"
    # from the reasons risk_manager.evaluate_exit() sets should_exit=True on, so
```
models.py (line 269) — old:
```python
    high_water_mark: float = None  # best price seen since entry; defaults to entry_price -- drives the trailing stop
```
new:
```python
    high_water_mark: float = None  # best price seen since entry; defaults to entry_price. A RECORD only:
                                   # no exit rule reads it since the trailing stop was removed 2026-08-17
                                   # (position_manager keeps it current so replay matches live)
```
models.py (line 377-380) — old:
```python
    # Reasons actually produced: risk_manager.evaluate_exit() sets "stop_loss",
    # "trailing_stop", "take_profit" (should_exit=True) and "trailing_active",
    # "hold" (should_exit=False); position_manager adds "resolution"
    # (should_exit=True) and "resolution_unknown" (should_exit=False).
```
new:
```python
    # Reasons actually produced: risk_manager.evaluate_exit() sets "stop_loss"
    # and "take_profit" (should_exit=True) and "hold" (should_exit=False);
    # position_manager adds "resolution" (should_exit=True) and
    # "resolution_unknown" (should_exit=False). "trailing_stop" and
    # "trailing_active" went with the trailing stop on 2026-08-17 and survive
    # only on historical rows.
```
executor.py (line 1156) — old:
```python
    typical trailing-stop gain: booking exits gross is how a strategy shows
```
new:
```python
    typical take-profit gain: booking exits gross is how a strategy shows
```
storage.py `close_position` docstring (line 1262) — old:
```python
    """Mark a position closed -- status should be one of 'closed_take_profit', 'closed_stop_loss', 'closed_trailing_stop', 'closed_resolution' (see models.Position.status)."""
```
new:
```python
    """Mark a position closed -- status should be one of 'closed_take_profit', 'closed_stop_loss', 'closed_resolution', or 'closed_trailing_stop' (historical rows only; see models.Position.status)."""
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave3_stale_docs.py tests/test_wave2_error_sample_window.py -v` / Expected: PASS
- [ ] **Step 5: Run the full suite** — `pytest -q` / Expected: all pass, count >= 1894
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add risk_manager.py config.py models.py executor.py storage.py tests/test_wave3_stale_docs.py
git commit -m "Wave 3 (3e): stale-docs sweep -- only the take tightens, the lottery carve-out skips the stop, the trailing stop is history

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: 3f — `redeem_abi_probe.py`, the read-only redemption ABI spike
**Files:** Create `redeem_abi_probe.py` / Test `tests/test_wave3_redeem_abi_probe.py`
**Interfaces:** Produces: `redeem_abi_probe.selector(signature) -> str`, `push4_immediates(bytecode_hex) -> set`, `redeem_positions_lines(source) -> list`, `main(argv) -> int` with `--offline`, `--api-key`, `--adapter`, `--rpc-url`

**What it answers and how.** `redeem.py:269` sends `[amount_base_units]` — one element — to `redeemPositions(bytes32,uint256[])`. The review read the adapter as expecting `[yesAmount, noAmount]`, forwarded as the `amounts` of an ERC1155 `safeBatchTransferFrom` over the two position ids, which reverts on a length mismatch. The probe (i) prints the selector it derives from the canonical signature (`0xdbeccb23`, equal to `clients/onchain_client.REDEEM_POSITIONS_SELECTOR` — the test asserts it); (ii) with an Etherscan/Polygonscan key, fetches the verified source and prints the `redeemPositions` lines that touch the amounts argument; (iii) without a key, `eth_getCode` over plain JSON-RPC and scans the dispatcher's PUSH4 immediates for `0xdbeccb23` and for `safeBatchTransferFrom`'s `0x2eb2c2d6` — evidence consistent with the two-element reading, not proof; (iv) offline, prints the numbers and the commands. From this dev box the RPC returns HTTP 403 (verified), so the operator runs it on the EC2 box, where `redeem.py` already reaches the same endpoint. It imports `eth_utils.keccak` (installed on the box by `py-clob-client-v2`'s dependency chain and locally) and nothing that signs; an AST test pins the import allowlist. No transaction, no key, no wallet. `redeem.py` is NOT changed in this wave.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wave3_redeem_abi_probe.py
"""
Wave 3 item 3f. The probe must build the selector redeem.py's encoder uses,
scan PUSH4 immediates correctly (a selector inside another PUSH's data is
not a dispatcher entry), read the amounts lines out of a redeemPositions
body, run offline, and never import signing code.
"""
import ast
import pathlib

import redeem_abi_probe as probe
from clients import onchain_client

SRC = pathlib.Path(probe.__file__).read_text(encoding="utf-8")

ALLOWED_IMPORTS = {"argparse", "json", "os", "sys", "urllib", "urllib.error", "urllib.request", "eth_utils"}


def test_the_selector_is_the_one_redeem_py_encodes():
    assert probe.selector("redeemPositions(bytes32,uint256[])") == "0xdbeccb23"
    assert probe.selector("redeemPositions(bytes32,uint256[])") == "0x" + onchain_client.REDEEM_POSITIONS_SELECTOR.hex()
    assert probe.selector("safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)") == "0x2eb2c2d6"


def test_push4_scanner_skips_selectors_buried_in_other_push_data():
    # PUSH5 carrying 63dbeccb23 as DATA, then a real PUSH4 dbeccb23, then a real PUSH4 2eb2c2d6.
    code = "0x" + "64" + "63dbeccb23" + "80" + "63" + "dbeccb23" + "14" + "63" + "2eb2c2d6"
    assert probe.push4_immediates(code) == {"0xdbeccb23", "0x2eb2c2d6"}


def test_source_reader_finds_the_two_element_indexing():
    source = """
contract NegRiskAdapter {
    function redeemPositions(bytes32 _conditionId, uint256[] calldata _amounts) external {
        uint256[] memory positionIds = new uint256[](2);
        positionIds[0] = getPositionId(_conditionId, true);
        positionIds[1] = getPositionId(_conditionId, false);
        ctf.safeBatchTransferFrom(msg.sender, address(this), positionIds, _amounts, "");
        uint256 payout = _amounts[0] + _amounts[1];
    }
    function other() external { uint256 amounts = 1; }
}
"""
    lines = probe.redeem_positions_lines(source)
    assert len(lines) == 3 and "function redeemPositions" in lines[0]
    assert any("_amounts[1]" in l for l in lines)
    assert not any("function other" in l for l in lines)


def test_offline_prints_the_selector_and_the_manual_steps(capsys):
    assert probe.main(["--offline"]) == 0
    out = capsys.readouterr().out
    assert "0xdbeccb23" in out and "redeemPositions(bytes32,uint256[])" in out
    assert "--api-key" in out and "OFFLINE" in out


def test_the_probe_imports_no_signing_code():
    tree = ast.parse(SRC)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= ALLOWED_IMPORTS, sorted(imported - ALLOWED_IMPORTS)
    for forbidden in ("eth_account", "clients", "redeem", "wallet_client", "py_clob_client_v2", "onchain_client"):
        assert forbidden not in imported
    assert "private_key" not in SRC
    assert "eth_sendRawTransaction" not in SRC and "Account" not in SRC
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave3_redeem_abi_probe.py -v` / Expected: FAIL with `ModuleNotFoundError: No module named 'redeem_abi_probe'`

- [ ] **Step 3: Write minimal implementation** — create `redeem_abi_probe.py`:

```python
#!/usr/bin/env python3
"""
redeem_abi_probe.py -- READ-ONLY spike (Wave 3, 3f).

QUESTION. redeem.py builds NegRiskAdapter.redeemPositions(bytes32,uint256[])
with a ONE-element amounts array ([amount]). The 2026-09-15 trade-logic review
read the adapter's source as expecting TWO elements -- [yesAmount, noAmount],
one per outcome -- forwarded as the `amounts` of an ERC1155
safeBatchTransferFrom over the two position ids, which reverts on a length
mismatch. If that is right, redemption has never been able to work. This
script settles it against the DEPLOYED contract, and does nothing else.

WHAT IT DOES (no transaction, no private key, no wallet, no signing code):
  1. prints the selector it is looking for, computed here from the canonical
     signature (0x + keccak256(sig)[:4]) so the reader can compare it with
     clients/onchain_client.REDEEM_POSITIONS_SELECTOR by eye;
  2. with an Etherscan/Polygonscan API key (--api-key or $POLYGONSCAN_API_KEY /
     $ETHERSCAN_API_KEY): fetches the VERIFIED SOURCE of the adapter and
     prints every line of redeemPositions() that touches the amounts
     parameter -- the answer is read off the source;
  3. without a key: fetches the deployed BYTECODE over plain JSON-RPC
     (eth_getCode, no key needed) and lists which of the relevant 4-byte
     selectors appear in it as PUSH4 immediates. The adapter answering to
     redeemPositions(bytes32,uint256[]) AND its bytecode carrying the
     safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)
     selector is consistent with the two-element reading; it is evidence,
     not proof -- say so when appending to memory;
  4. offline (--offline, or no network): prints step 1's numbers and the
     exact commands to run by hand. From the dev box the RPC returns 403;
     run it on the EC2 box, where redeem.py already reaches that endpoint.

WHAT IT NEVER DOES: import clients.onchain_client, clients.wallet_client,
redeem, eth_account or py_clob_client_v2. tests/test_wave3_redeem_abi_probe.py
pins the import allowlist by AST. Nothing here can spend anything.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from eth_utils import keccak

# The deployed NegRiskAdapter on Polygon (chain 137), as recorded in
# docs/superpowers/specs/2026-09-01-redemption-design.md and cross-checked
# against py_clob_client_v2.config.get_contract_config(137).neg_risk_adapter
# on 2026-09-03. Overridable with --adapter; never resolved through the
# client library here, because that library pulls in the account stack.
DEFAULT_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
DEFAULT_RPC_URL = "https://polygon.drpc.org"
POLYGON_CHAIN_ID = 137
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"

REDEEM_SIG = "redeemPositions(bytes32,uint256[])"
# What the two-element reading predicts the adapter calls internally, the
# one-element alternative a reader might expect, and two neighbours.
RELATED_SIGS = (
    "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)",
    "safeTransferFrom(address,address,uint256,uint256,bytes)",
    "getPositionId(bytes32,bool)",
    "redeemPositions(address,bytes32,bytes32,uint256[])",  # ConditionalTokens' own
)


def selector(signature: str) -> str:
    """'0x' + the first four bytes of keccak256(signature), lower-case hex."""
    return "0x" + keccak(signature.encode()).hex()[:8]


def push4_immediates(bytecode_hex: str) -> set:
    """
    Every 4-byte immediate of a PUSH4 (0x63) opcode in EVM bytecode, as
    '0x' + 8 hex chars. Solidity's function dispatcher compares the calldata
    selector against PUSH4 immediates, so the set of selectors a contract
    answers to appears here. Walks the bytecode opcode by opcode (PUSH1..
    PUSH32 carry 1..32 immediate bytes, everything else carries none) so a
    selector-looking byte pattern INSIDE another PUSH's data is not
    mistaken for a dispatcher entry.
    """
    raw = bytes.fromhex(bytecode_hex[2:] if bytecode_hex.startswith("0x") else bytecode_hex)
    found = set()
    i = 0
    while i < len(raw):
        op = raw[i]
        if 0x60 <= op <= 0x7F:              # PUSH1 (0x60) .. PUSH32 (0x7F)
            width = op - 0x5F
            if op == 0x63 and i + 4 < len(raw):
                found.add("0x" + raw[i + 1:i + 5].hex())
            i += 1 + width
        else:
            i += 1
    return found


def _rpc(method: str, params: list, rpc_url: str, timeout: int = 20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if "error" in payload:
        raise RuntimeError(f"{method}: {payload['error']}")
    return payload["result"]


def fetch_bytecode(adapter: str, rpc_url: str) -> str:
    return _rpc("eth_getCode", [adapter, "latest"], rpc_url)


def fetch_verified_source(adapter: str, api_key: str, timeout: int = 20) -> str:
    """The verified source text (all files concatenated) from Etherscan V2 for chain 137."""
    url = (f"{ETHERSCAN_V2}?chainid={POLYGON_CHAIN_ID}&module=contract&action=getsourcecode"
           f"&address={adapter}&apikey={api_key}")
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("status") != "1" or not payload.get("result"):
        raise RuntimeError(f"explorer said: {payload.get('message')} {payload.get('result')}")
    src = payload["result"][0].get("SourceCode", "") or ""
    # Multi-file verifications arrive as a JSON blob wrapped in one extra
    # pair of braces; flatten to text so line reading works either way.
    if src.startswith("{{"):
        try:
            files = json.loads(src[1:-1]).get("sources", {})
            src = "\n".join(f"// ---- {name}\n{f.get('content', '')}" for name, f in files.items())
        except ValueError:
            pass
    return src


def redeem_positions_lines(source: str) -> list:
    """The redeemPositions() body's lines that mention its amounts argument
    (plus the signature line), so the answer can be read off the source."""
    out = []
    inside = False
    depth = 0
    opened = False
    for line in source.splitlines():
        if not inside:
            if "function redeemPositions" not in line:
                continue
            inside, depth, opened = True, 0, False
        if "function redeemPositions" in line or "amount" in line.lower():
            out.append(line.rstrip())
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth <= 0:
            inside = False
    return out


def report_selectors() -> str:
    lines = [f"selector wanted  : {selector(REDEEM_SIG)}  <- {REDEEM_SIG}", "related selectors:"]
    for sig in RELATED_SIGS:
        lines.append(f"  {selector(sig)}  <- {sig}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", default=DEFAULT_ADAPTER)
    ap.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    ap.add_argument("--api-key", default=os.environ.get("POLYGONSCAN_API_KEY") or os.environ.get("ETHERSCAN_API_KEY"))
    ap.add_argument("--offline", action="store_true", help="print the selectors and the manual steps only")
    args = ap.parse_args(argv)

    print(report_selectors())
    want = selector(REDEEM_SIG)

    if args.offline:
        print("\nOFFLINE. Run by hand on the box:\n"
              "  python redeem_abi_probe.py --api-key <etherscan/polygonscan key>   # reads the verified source\n"
              "  python redeem_abi_probe.py                                        # bytecode selectors only\n"
              f"and look for {want} and, in the source, whether redeemPositions indexes\n"
              "_amounts[0] and _amounts[1] (two elements) or forwards a single amount.")
        return 0

    verdict = []
    if args.api_key:
        try:
            lines = redeem_positions_lines(fetch_verified_source(args.adapter, args.api_key))
            print("\nVERIFIED SOURCE, redeemPositions lines mentioning the amounts argument:")
            for line in lines:
                print("  " + line)
            two = any("[1]" in line for line in lines)
            verdict.append("source: TWO-element [yesAmount, noAmount]" if two
                           else "source: no [1] index seen -- READ THE LINES ABOVE BY HAND")
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            print(f"\nverified source unavailable: {exc}")

    try:
        code = fetch_bytecode(args.adapter, args.rpc_url)
        sels = push4_immediates(code)
        print(f"\nDEPLOYED BYTECODE: {len(code) // 2 - 1} bytes, {len(sels)} PUSH4 immediates")
        print(f"  {want} {REDEEM_SIG:<44}: {'PRESENT' if want in sels else 'ABSENT'}")
        for sig in RELATED_SIGS:
            s = selector(sig)
            print(f"  {s} {sig[:44]:<44}: {'present' if s in sels else 'absent'}")
        batch = selector(RELATED_SIGS[0])
        if want in sels and batch in sels:
            verdict.append("bytecode: adapter answers redeemPositions(bytes32,uint256[]) and carries the "
                           "safeBatchTransferFrom selector -- CONSISTENT with a two-element amounts array")
        elif want in sels:
            verdict.append("bytecode: redeemPositions present, safeBatchTransferFrom absent -- inconclusive")
        else:
            verdict.append("bytecode: redeemPositions(bytes32,uint256[]) NOT in the dispatcher -- wrong address?")
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        print(f"\nbytecode unavailable ({exc}) -- re-run on a box with network, or with --offline")

    print("\nVERDICT (append one paragraph to memory; the redeem.py fix is a checkpoint decision):")
    for v in verdict or ["nothing fetched"]:
        print("  - " + v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave3_redeem_abi_probe.py -v && python redeem_abi_probe.py --offline` / Expected: PASS; the offline run prints `selector wanted  : 0xdbeccb23`
- [ ] **Step 5: Run the full suite** — `pytest -q` / Expected: all pass, count >= 1899
- [ ] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add redeem_abi_probe.py tests/test_wave3_redeem_abi_probe.py
git commit -m "Wave 3 (3f): redeem_abi_probe.py, a read-only check of redeemPositions' amounts-array shape

Builds 0xdbeccb23 from the canonical signature, reads the verified source
with an explorer key or scans the deployed bytecode's PUSH4 dispatcher
without one, and prints the manual steps offline. Imports keccak and
urllib only; an AST test pins that no signing code is reachable.
redeem.py is unchanged -- the fix is a checkpoint decision.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Merge, deploy in the 15:00–19:00Z gap, falsifier, memory
**Files:** none new (the runbook) / Test: the same-day falsifier reads below
**Interfaces:** Consumes: Tasks 1–7 merged on `main`

**Shape.** Backup-and-stop, now performed BY `deploy_daemon.sh` (Task 2): pull → re-exec → pip → generators copied → mode/demotion guard → unit written → **stop timer + daemon → sqlite backup → `storage.migrate()` as ubuntu → restart daemon → start timer + dashboard**. The first run pulls the NEW script and re-execs into it before any of that, so the new order applies on the very first Wave 3 deploy. Then `setup_dashboard.sh` once, for 3b (it is idempotent now). No `REGIME_BOUNDARIES` stamp.

- [ ] **Step 1: Full suite on the branch, then merge and push**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
pytest -q
cd ..
git checkout main
git merge --no-ff feat/wave3-stop-the-corruption -m "Merge feat/wave3-stop-the-corruption: stop the data from corrupting itself (3a-3f)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
cd weather-forecast && pytest -q && cd ..
git push origin main
```

- [ ] **Step 2: Wait for the window** — deploy only between 15:00Z and 19:00Z (no region's entry window open; the Asia collection window opens 20:00Z). Record the UTC time of every step below.

- [ ] **Step 3: Pre-deploy reads on the box (read-only)** — via plink (memory `ec2-deployment.md`): `systemctl show polyweather-dashboard.service -p User` (expect empty — the 3b before-state), `ls -l ~/weather-forecast/weather-forecast/data/` (expect ubuntu:ubuntu, and note any `-journal`/`-wal` file and its owner), `ls -l /var/www/html` (expect root:root — before-state), `git -C ~/weather-forecast log -1 --oneline`.

- [ ] **Step 4: Deploy the daemon** — `~/deploy.sh` (the shim to `deploy/deploy_daemon.sh`). Expected in its output, in this order: `== stop ==`, `== backup ==` with `backup written: /home/ubuntu/polyweather-pre-deploy-<stamp>.sqlite3`, `== migrate ==` with exactly one `migrate: ok -- 8 tables: ...; 1 view(s): position_economics` line, then the restart, then `== dashboard ==`, then `active`. If the demotion guard refuses, the daemon is still running (the stop comes after the guard): fix the mode and re-run.

- [ ] **Step 5: Deploy the dashboard unit (3b)** — `bash ~/weather-forecast/deploy/setup_dashboard.sh`. Expected: `User=ubuntu` printed by `systemctl show`, `ls -l /var/www/html/*.html` shows ubuntu-owned files with a fresh mtime, nginx `HTTP 200`.

- [ ] **Step 6: Box == main** — `git -C ~/weather-forecast log -1` equals the merge commit from Step 1.

- [ ] **Step 7: Same-day falsifier (all read-only)** — append every line to a memory note `wave3-stop-the-corruption.md`:
  1. **3a, boot:** `sudo journalctl -u polyweather --since "-15 min" --no-pager | grep -c "boot: storage migrated"` is `1`, and the line carries `writable; config sha <merge sha>`.
  2. **3a, no read-only errors:** `sudo journalctl -u polyweather --since "-15 min" --no-pager | grep -c StorageReadOnlyError` is `0`; the same grep over `journalctl -u polyweather-dashboard.service` is `0`.
  3. **3a, migrate once:** the deploy output has exactly one `migrate: ok` line; `sqlite3 ~/weather-forecast/weather-forecast/data/polyweather.sqlite3 "PRAGMA table_info(positions)" | tail -5` ends with `kelly_size_preclamp_usd`.
  4. **3b, dashboards as ubuntu:** `systemctl show polyweather-dashboard.service -p User` = `User=ubuntu`; after the next timer tick (`systemctl list-timers polyweather-dashboard.timer`), `ls -l /var/www/html` shows every `.html` owned ubuntu with mtime after the deploy, and `curl -s http://localhost/ | grep -c "(journal unavailable)"` is `0` (the journal group took).
  5. **3c, exits before entries:** after the 05:00 SGT cycle (21:00Z, the SAME evening if the deploy was before 19:00Z): `sudo journalctl -u polyweather --since "21:00" --until "21:05" --no-pager | grep -n "WSSS" | grep -m2 -E "position_manager|candidate\(s\) clearing|no opportunities"` — the first `[position_manager]` line for WSSS precedes its first entry-leg line.
  6. **3d, DST hour:** on the box, `cd ~/weather-forecast/weather-forecast && .venv/bin/python -c "import position_manager, config; from datetime import date; from models import Position; p=Position(position_id='x',station_icao='EGLC',target_date=date(2026,9,25),bucket_c=25,side='YES',entry_price=0.4,size_usd=1.0,entry_time='x'); print(position_manager._local_hour_for(p), config.current_utc_offset_hours('EGLC'))"` prints `<UTC hour + 1> 1` while BST holds (until 2026-10-25).
  7. **3f:** `cd ~/weather-forecast/weather-forecast && .venv/bin/python redeem_abi_probe.py` (no key: the bytecode read) and, if an explorer key is available, `--api-key`. Append its VERDICT paragraph to memory verbatim, labelled evidence-not-proof if only the bytecode read ran.
  8. **Unchanged-behaviour check:** entry count and per-station approval rate over the next 24h versus the 7 days before (`entry_decisions` grouped by date), expected within the usual day-to-day range — 3c can only ADD approvals that the concurrent cap used to refuse, and only on live stations.

- [ ] **Step 8: Update the spec's status line** — `docs/superpowers/specs/2026-09-17-evidence-first-remediation-design.md` line 3: append `; Wave 3 MERGED <sha> + DEPLOYED <date> <hh:mm> UTC (no regime boundary)`. Commit on `main` with the trailer and push.

---

## Self-review against the spec

| Spec requirement | Task |
|---|---|
| 3a `storage.migrate()` explicit, run by the daemon at boot and by `deploy_daemon.sh`; `_connect()` issues no DDL; non-daemon processes open `mode=ro` unless they declare themselves writers | Task 1 (`migrate`/`_apply_schema`/`_connect` open-only/`mode=ro`/`StorageReadOnlyError`; AST guard in `test_no_fd_leak.py`; pre-Wave-1 migration test; read-only-cannot-write test; `tmp_db` + session isolation; 36 fixture sites) + Task 2 (`_boot_storage` in `run_forever`; `set_writable` at the three operator writers; deploy script order; writer allowlist by AST) |
| 3b `User=ubuntu` on `polyweather-dashboard.service`; dashboards open read-only | Task 3 (idempotent `setup_dashboard.sh`, `chown -R ubuntu:ubuntu /var/www/html`, journal group; generators pinned by AST to never call `set_writable`/`migrate`/`sqlite3.connect` — read-only is the default they inherit from Task 1) |
| 3c exit check before the entry leg; past-dated position with no price straight to `_market_reported_closed` | Task 4 (`EXIT_CHECK_BEFORE_ENTRIES`, `PAST_DATED_GAMMA_ON_FIRST_FAILURE`; the 2026-09-02 case reproduced with the real `_live_budget_breach`; pipeline-failure side effect pinned; flag-off paths pinned) |
| 3d `position_manager._local_hour_for` and `backtest/simclock.py` use `config.current_utc_offset_hours` | Task 5 (`DST_AWARE_LOCAL_HOUR`; `_local_hour_for`; `simclock.utc_offset_for` + `SimClock.retune`, engine per-day; `risk_manager._local_hour` default REMOVED per fact 3; EGLC September test live and replay) |
| 3e stale-docs sweep: "both tighten", "skip BOTH", trailing-stop references, the `storage.py` day-filter comment | Task 6 (exact lines and replacements; day-filter comment verified already corrected in Wave 2, pinned by the Wave 2 test) |
| 3f read-only spike on `NegRiskAdapter.redeemPositions` amounts-array shape; no transaction; one paragraph | Task 7 (`redeem_abi_probe.py`, selector/PUSH4/source-lines/offline; import allowlist; operator runs it on the box) + Task 8 step 7 (the paragraph) |
| Carried: `_config_sha` at boot; `deploy_daemon.sh` dashboard-before-daemon | Task 2 (`_boot_storage` primes the sha; dashboard start moved to the end) |
| Flags default on for behaviour changes; 3a unflagged with `set_writable`; no `REGIME_BOUNDARIES` stamp; backup-and-stop in the 15:00–19:00Z gap; never on the box; trailer | Global Constraints; Task 4's config block; Task 2's script; Task 8 |

Names used consistently across tasks: `StorageReadOnlyError`, `_WRITABLE`, `set_writable`, `is_writable`, `migrate`, `schema_summary`, `_apply_schema`, `tmp_db`, `_isolated_default_db` (Task 1 → Tasks 2, 3, 4, 5); `_boot_storage` (Task 2 → Task 8); `EXIT_CHECK_BEFORE_ENTRIES`, `PAST_DATED_GAMMA_ON_FIRST_FAILURE`, `DST_AWARE_LOCAL_HOUR` (Task 4 → Task 5); `utc_offset_for`, `retune`, `_station_offset_now` (Task 5); `selector`, `push4_immediates`, `redeem_positions_lines` (Task 7 → Task 8).

## Risks / open items (not in this wave)
- `backtest/price_store.py` still creates its schema lazily on open (price_store.py:96-110), from the daemon, the dashboards (`generate_dashboard.py:676`) and `bucket_bias._market_db`. With the dashboard as ubuntu (3b) this can no longer leave a root-owned market-data file, but it remains a second DDL-on-open path; a `price_store.migrate()` would be the same shape as Task 1 and is deferred.
- `scheduler.run_forever` still groups stations by offset ONCE at boot (scheduler.py:722-730): the DST-end restart on 2026-10-25/11-01 is still required for the SCHEDULE; 3d fixes the exit hour and the replay only.
- `usermod -aG systemd-journal ubuntu` assumes the box's `ubuntu` user is not already restricted; if the journal block still renders `(journal unavailable)` after Task 8 step 7.4, `sudo usermod -aG adm ubuntu` is the alternative Ubuntu images use.
