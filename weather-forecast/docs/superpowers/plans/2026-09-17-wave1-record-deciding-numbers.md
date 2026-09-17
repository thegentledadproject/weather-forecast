# Wave 1: Record the Deciding Numbers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** COMPLETE — merged 0dcc5f6, deployed 2026-09-17 17:03 UTC, 1757 tests.

**Goal:** Persist, for every entry decision the daemon makes (approved or not, primary or paper-shadow) and for every executor refusal, the exact numbers that decided it — without changing a single decision.
**Architecture:** Five nullable columns on `positions` plus a new append-only `entry_decisions` table, both migrated through `storage._connect()`'s idempotent column-list pattern; `entry_manager` stamps a `rule_id` and the four deciding numbers on every `EntryDecision` return site (mirrored in `backtest/entry_sim.py`); `scheduler._run_full_cycle` records the decisions best-effort before calling the executor, and for live stations re-runs the same cycle as a read-only paper twin through an explicit `execution_mode="paper"` override parameter (never by mutating `executor.EXECUTION_MODE`); executor refusals become `live_order_attempts` rows with `outcome='refused'` that the daily order cap ignores.
**Tech Stack:** Python 3.12, sqlite3, pytest; no numpy/scipy (not on the box)
**Spec:** docs/superpowers/specs/2026-09-17-evidence-first-remediation-design.md

## Global Constraints
- Wave 1 adds rows only: no task may change EntryDecision.approved, recommended_size_usd, reason, or any exit decision (spec Principle 2)
- Migration follows the existing idempotent column-list pattern in storage._connect (spec 1a)
- The shadow pass never calls executor.open_position or storage.open_position, and never writes positions (spec 1d)
- entry_decisions writes are best-effort: a storage failure logs and continues (spec 1b)
- Run pytest from the weather-forecast/ package dir; never on the EC2 box
- Commit after every task; commit messages end with the line: Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
---

**Paths.** The git root is `C:\Users\user\Downloads\weather-forecast`; the Python package is the nested `weather-forecast/` directory. Every path below is relative to the package dir (`weather-forecast/storage.py` == `C:\Users\user\Downloads\weather-forecast\weather-forecast\storage.py`). Every `pytest` and `git` command runs from the package dir. Line numbers are as of HEAD `326a3ef`, before any Wave 1 task; when an earlier task has shifted them, match on the quoted code, not the number. The suite collects **1681** tests at HEAD.

**One design fact that shapes Tasks 7-8.** `execution_mode` reaches the EV table as a *parameter* (`ev_engine.run_for_station_with_map(estimate, execution_mode=...)` → `compute_ev_table(execution_mode=...)`, ev_engine.py:538-634), but it reaches the entry gates by a *module-dict lookup*: `entry_manager._execution_mode()` (entry_manager.py:123-125) reads `executor.EXECUTION_MODE.get(station_icao)`, and that feeds `_candidate_is_paper` (128), `_book_has_stop` (145), `live_size_cap_usd` (158), which `evaluate_entry` (982, 1143, 1164) and `decide_portfolio_entries` (1571) call. The shadow pass therefore threads an explicit `execution_mode: Optional[str] = None` override through `decide_portfolio_entries → decide_entries → evaluate_entry → the three helpers`, defaulting to today's lookup so no existing caller changes; it never writes the dict. The EV side is not re-run at all: `run_for_station_with_map` would re-discover the market and re-write price snapshots, so the shadow re-prices the primary pass's own `ev_results` for `"paper"` with a new pure helper `ev_engine.reprice_for_mode` (only `expected_exit_fee_pct` and `net_ev_per_dollar` depend on the mode; a parity test pins that).

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `storage.py` | modify | schema (5 positions columns, `entry_decisions` table + indexes), `open_position` INSERT, `_row_to_position`, `record_entry_decisions`, `load_entry_decisions`, `count_live_order_attempts` excludes refusals |
| `models.py` | modify | six new `EntryDecision` fields, five new `Position` fields |
| `entry_manager.py` | modify | `ENTRY_RULE_IDS`, `deciding_numbers`, `preclamp_size_usd`, `rule_id` + deciding numbers at every return site, `execution_mode` override parameter |
| `backtest/entry_sim.py` | modify | mirrors the same ids and fields at every replica site |
| `executor.py` | modify | copies the five fields onto `Position`; `net_ev_at_size` at resolved size on order-path rows; refusal rows via `_record_refusal` |
| `ev_engine.py` | modify | `reprice_for_mode` (pure re-pricing of an EV table for another mode) |
| `scheduler.py` | modify | `_config_sha`, `_record_entry_decisions`, recording call in `_run_full_cycle`, `_run_shadow_pass` |
| `wave1_falsifier.py` | create | read-only operator script printing the four spec 1f checks |
| `tests/test_wave1_schema.py` | create | migration on a pre-Wave-1 schema, idempotent |
| `tests/test_wave1_deciding_numbers_persisted.py` | create | EntryDecision → Position → storage round trip |
| `tests/test_gate_census.py` | modify | `rule_id` census over live + sim + budget/veto/collection sites |
| `tests/test_wave1_deciding_numbers_carried.py` | create | the carried numbers equal the numbers the gates compared; preclamp size |
| `tests/test_wave1_resolved_net_ev.py` | create | live/simulation rows store net EV at the resolved size |
| `tests/test_wave1_entry_decisions_recorded.py` | create | scheduler records before executor, best-effort, decision identity, field parity |
| `tests/test_wave1_refusal_rows.py` | create | one refused row per executor refusal site; cap ignores refusals |
| `tests/test_wave1_mode_override.py` | create | explicit `execution_mode` override never mutates the dict; `reprice_for_mode` parity |
| `tests/test_wave1_shadow_pass.py` | create | AST call-graph guard; live station → `paper_shadow` rows, zero positions |
| `tests/test_wave1_falsifier.py` | create | the four checks on a fixture DB |

---

### Task 1: Schema — five `positions` columns and the `entry_decisions` table
**Files:** Modify `storage.py` (CREATE TABLE positions 217-245; ALTER column list 296-314; after the `ix_loa_ts` index at 375) / Test `tests/test_wave1_schema.py`
**Interfaces:** Consumes: nothing new / Produces: columns `positions.calibrated_prob REAL, calibration_source TEXT, admission_edge REAL, sizing_edge REAL, kelly_size_preclamp_usd REAL`; table `entry_decisions` with columns in the exact order of `storage.ENTRY_DECISION_COLUMNS` (defined here, used by Task 5); indexes `ix_ed_cycle`, `ix_ed_pair`

- [x] **Step 1: Write the failing test**

```python
# tests/test_wave1_schema.py
"""
Wave 1 migration: a database whose `positions` table predates the five
deciding-number columns, and which has no `entry_decisions` table, must gain
both from any storage._connect() -- and gain them once.
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


def test_connect_adds_the_five_columns_and_the_table(tmp_path, monkeypatch):
    db = _pre_wave1_db(tmp_path)
    assert "entry_decisions" not in _tables(db)
    monkeypatch.setattr(config, "DB_PATH", db)

    storage._connect().close()

    cols = _columns(db, "positions")
    for name in WAVE1_POSITION_COLUMNS:
        assert name in cols
    assert "entry_decisions" in _tables(db)
    assert _columns(db, "entry_decisions") == list(storage.ENTRY_DECISION_COLUMNS)


def test_the_five_columns_come_last_in_declared_order(tmp_path, monkeypatch):
    """_row_to_position reads SELECT * positionally, so order is load-bearing."""
    db = _pre_wave1_db(tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db)
    storage._connect().close()
    assert tuple(_columns(db, "positions")[-5:]) == WAVE1_POSITION_COLUMNS


def test_a_fresh_database_declares_the_same_order(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fresh.sqlite3"))
    storage._connect().close()
    assert tuple(_columns(config.DB_PATH, "positions")[-5:]) == WAVE1_POSITION_COLUMNS


def test_the_migration_is_idempotent_and_keeps_old_rows_null(tmp_path, monkeypatch):
    db = _pre_wave1_db(tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db)
    storage._connect().close()
    storage._connect().close()
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
    storage._connect().close()
    con = sqlite3.connect(config.DB_PATH)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    con.close()
    assert {"ix_ed_cycle", "ix_ed_pair"} <= names
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave1_schema.py -v` / Expected: FAIL with `AttributeError: module 'storage' has no attribute 'ENTRY_DECISION_COLUMNS'` and `assert 'calibrated_prob' in cols`

- [x] **Step 3: Write minimal implementation**

storage.py, module level, directly above `def _connect() -> sqlite3.Connection:` (line 189):

```python
# WAVE 1 (2026-09-17). One row per EntryDecision per cycle, approved or not,
# in the column order record_entry_decisions() writes them. The dataclass
# fields these mirror are named identically so tests/test_wave1_entry_
# decisions_recorded.py can assert parity by name. `book` is the executor
# mode the decision was made under, or 'paper_shadow' for the read-only
# paper twin a live station runs beside its primary pass.
ENTRY_DECISION_COLUMNS = (
    "cycle_ts", "station_icao", "target_date", "bucket_c", "side", "book",
    "approved", "rule_id", "reason",
    "entry_price", "entry_bid", "model_prob", "calibrated_prob", "calibration_source",
    "raw_edge", "admission_edge", "sizing_edge", "net_ev_at_size",
    "kelly_size_preclamp_usd", "recommended_size_usd", "min_net_ev",
    "station_maturity", "config_sha",
)
```

storage.py `CREATE TABLE IF NOT EXISTS positions` (line 217-245) — old:
```python
            entry_fee_per_share REAL,
            trigger_price REAL
        )
        """
    )
```
new:
```python
            entry_fee_per_share REAL,
            trigger_price REAL,
            calibrated_prob REAL,
            calibration_source TEXT,
            admission_edge REAL,
            sizing_edge REAL,
            kelly_size_preclamp_usd REAL
        )
        """
    )
```

storage.py ALTER list (line 296-314) — old:
```python
        ("trigger_price", "trigger_price REAL"),
    ):
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {column_ddl}")
```
new:
```python
        ("trigger_price", "trigger_price REAL"),
        # WAVE 1 (2026-09-17): THE NUMBERS THAT DECIDED THE TRADE. model_prob
        # and raw_edge above stay RAW (the isotonic map is fitted on
        # model_prob); these are what the gates and Kelly actually compared:
        # the P3-6 calibrated probability and its tier, the edge veto 0a2 was
        # applied to, the edge Kelly sized on, and the Kelly size BEFORE the
        # live $1.00 clamp and the exchange minimum. NULL on every earlier
        # row, never backfilled: the map that would have applied is not
        # honestly reconstructible. Same order as the CREATE TABLE above --
        # _row_to_position reads SELECT * by position.
        ("calibrated_prob", "calibrated_prob REAL"),
        ("calibration_source", "calibration_source TEXT"),
        ("admission_edge", "admission_edge REAL"),
        ("sizing_edge", "sizing_edge REAL"),
        ("kelly_size_preclamp_usd", "kelly_size_preclamp_usd REAL"),
    ):
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {column_ddl}")
```

storage.py, directly after `conn.execute("CREATE INDEX IF NOT EXISTS ix_loa_ts ON live_order_attempts(kind, ts)")` (line 375):
```python
    # WAVE 1: every EntryDecision, every cycle, whether or not it traded.
    # `positions` records what was DONE; this records what was DECIDED and
    # the numbers it was decided on, so the funnel between the EV screen and
    # a fill can be read from rows instead of reconstructed from journal
    # prose. Append-only. Candidates refused before evaluate_entry (the
    # best_opportunities screen) do not get rows -- ev_snapshots holds that
    # universe. See ENTRY_DECISION_COLUMNS for the meaning of `book`.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entry_decisions (
            cycle_ts TEXT NOT NULL,
            station_icao TEXT NOT NULL,
            target_date TEXT NOT NULL,
            bucket_c INTEGER NOT NULL,
            side TEXT NOT NULL,
            book TEXT NOT NULL,
            approved INTEGER NOT NULL,
            rule_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            entry_price REAL,
            entry_bid REAL,
            model_prob REAL,
            calibrated_prob REAL,
            calibration_source TEXT,
            raw_edge REAL,
            admission_edge REAL,
            sizing_edge REAL,
            net_ev_at_size REAL,
            kelly_size_preclamp_usd REAL,
            recommended_size_usd REAL,
            min_net_ev REAL,
            station_maturity TEXT,
            config_sha TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ed_cycle ON entry_decisions(cycle_ts)")
    # The shadow-twin pairing key (spec 1d): entry_decisions(book='paper_shadow')
    # joined to positions(execution_mode='live') on (station, date, bucket, side).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_ed_pair ON entry_decisions"
        "(book, station_icao, target_date, bucket_c, side)"
    )
```

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave1_schema.py -v` / Expected: PASS (5 passed)
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1681
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add storage.py tests/test_wave1_schema.py
git commit -m "Wave 1 schema: five deciding-number columns on positions, entry_decisions table

Adds calibrated_prob, calibration_source, admission_edge, sizing_edge and
kelly_size_preclamp_usd to positions through the idempotent column-list
migration in storage._connect(), NULL on every pre-existing row and never
backfilled. Adds the append-only entry_decisions table (one row per
EntryDecision per cycle) with the cycle and pairing indexes. Nothing
writes the new columns yet.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Models carry the fields; storage and executor persist them
**Files:** Modify `models.py` (Position after `net_ev_at_size` at 356-358; EntryDecision after `min_net_ev` at 532), `storage.py` (`_row_to_position` 990-1032; `open_position` INSERT 1056-1108), `executor.py` (`_position` closure inside `open_position`, 828-870) / Test `tests/test_wave1_deciding_numbers_persisted.py`
**Interfaces:** Consumes: Task 1 columns / Produces: `EntryDecision.calibrated_prob: Optional[float]`, `.calibration_source: str = "uncalibrated"`, `.admission_edge: Optional[float]`, `.sizing_edge: Optional[float]`, `.kelly_size_preclamp_usd: Optional[float]`, `.rule_id: str = "unspecified"`; `Position.calibrated_prob`, `.calibration_source: Optional[str]`, `.admission_edge`, `.sizing_edge`, `.kelly_size_preclamp_usd` (all default None)

- [x] **Step 1: Write the failing test**

```python
# tests/test_wave1_deciding_numbers_persisted.py
"""
Wave 1: EntryDecision -> Position -> positions row -> Position, for the five
deciding numbers. The chain is only as good as its weakest hop, so every hop
is exercised here: the dataclass fields exist, storage writes and reads them,
and executor.open_position copies them off the decision.
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


def _position(**overrides) -> Position:
    base = dict(
        position_id="WSSS:2026-09-17:32:YES:t", station_icao="WSSS",
        target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        entry_price=0.30, size_usd=10.0, entry_time="2026-09-17T00:00:00+00:00",
        status="open", is_paper=True,
        model_prob=0.42, raw_edge=0.12, net_ev_at_size=0.08,
        calibrated_prob=0.37, calibration_source="pooled_isotonic",
        admission_edge=0.07, sizing_edge=0.07, kelly_size_preclamp_usd=23.5,
    )
    base.update(overrides)
    return Position(**base)


def _decision(**overrides) -> EntryDecision:
    base = dict(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        kelly_fraction_raw=0.2, kelly_fraction_applied=0.05,
        recommended_size_usd=10.0, available_depth_usd=200.0,
        slippage_at_size_pct=0.01, net_ev_at_size=0.08,
        approved=True, reason="test", station_maturity="mature",
        entry_price=0.30, token_id="tok-1", model_prob=0.42, raw_edge=0.12,
        calibrated_prob=0.37, calibration_source="pooled_isotonic",
        admission_edge=0.07, sizing_edge=0.07, kelly_size_preclamp_usd=23.5,
        rule_id="approved",
    )
    base.update(overrides)
    return EntryDecision(**base)


def test_entry_decision_defaults_are_unknown_not_zero():
    d = EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=0.0,
        available_depth_usd=None, slippage_at_size_pct=None, net_ev_at_size=None,
        approved=False, reason="x", station_maturity="mature",
    )
    assert d.calibrated_prob is None
    assert d.calibration_source == "uncalibrated"
    assert d.admission_edge is None and d.sizing_edge is None
    assert d.kelly_size_preclamp_usd is None
    assert d.rule_id == "unspecified"


def test_position_round_trips_the_five_fields():
    storage.open_position(_position())

    (loaded,) = storage.load_open_positions("WSSS")

    assert loaded.calibrated_prob == pytest.approx(0.37)
    assert loaded.calibration_source == "pooled_isotonic"
    assert loaded.admission_edge == pytest.approx(0.07)
    assert loaded.sizing_edge == pytest.approx(0.07)
    assert loaded.kelly_size_preclamp_usd == pytest.approx(23.5)


def test_position_round_trips_none_as_none():
    storage.open_position(_position(
        calibrated_prob=None, calibration_source=None, admission_edge=None,
        sizing_edge=None, kelly_size_preclamp_usd=None,
    ))
    (loaded,) = storage.load_open_positions("WSSS")
    assert loaded.calibrated_prob is None
    assert loaded.calibration_source is None
    assert loaded.kelly_size_preclamp_usd is None


def test_closed_history_loader_reads_them_too():
    storage.open_position(_position())
    storage.close_position("WSSS:2026-09-17:32:YES:t", 0.5, "2026-09-18T00:00:00+00:00",
                           "closed_resolution", "market_resolved")
    (loaded,) = storage.load_position_history("WSSS")
    assert loaded.sizing_edge == pytest.approx(0.07)


def test_executor_copies_the_five_fields_onto_the_position(monkeypatch):
    monkeypatch.setitem(executor.EXECUTION_MODE, "WSSS", "paper")

    executor.open_position(_decision())

    (stored,) = storage.load_open_positions("WSSS")
    assert stored.calibrated_prob == pytest.approx(0.37)
    assert stored.calibration_source == "pooled_isotonic"
    assert stored.admission_edge == pytest.approx(0.07)
    assert stored.sizing_edge == pytest.approx(0.07)
    assert stored.kelly_size_preclamp_usd == pytest.approx(23.5)
    # Untouched by this wave.
    assert stored.model_prob == pytest.approx(0.42)
    assert stored.net_ev_at_size == pytest.approx(0.08)
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave1_deciding_numbers_persisted.py -v` / Expected: FAIL with `TypeError: Position.__init__() got an unexpected keyword argument 'calibrated_prob'` (and the same for EntryDecision)

- [x] **Step 3: Write minimal implementation**

models.py `Position` — old (line 356-358):
```python
    net_ev_at_size: Optional[float] = None  # EntryDecision.net_ev_at_size -- net EV per dollar
                                              # re-checked at the size actually ordered

    def __post_init__(self):
```
new:
```python
    net_ev_at_size: Optional[float] = None  # EntryDecision.net_ev_at_size -- net EV per dollar
                                              # re-checked at the size actually ordered
    # WAVE 1 (2026-09-17): WHAT THE GATES ACTUALLY COMPARED. model_prob and
    # raw_edge above stay raw. These five are copied off the EntryDecision by
    # executor.open_position, never recomputed, and are None on every row
    # written before they existed (not backfilled: the calibration map that
    # would have applied then is gone). calibration_source is None on those
    # rows and "uncalibrated" on a new row that had no map -- different facts.
    calibrated_prob: Optional[float] = None          # P3-6 map output, sizing/admission input
    calibration_source: Optional[str] = None         # the map tier, as probability_calibration names it
    admission_edge: Optional[float] = None           # the edge veto 0a2 compared
    sizing_edge: Optional[float] = None              # the edge Kelly sized on
    kelly_size_preclamp_usd: Optional[float] = None  # the paper-equivalent stake: recommended size
                                                     # before the live $1.00 clamp and the exchange bump

    def __post_init__(self):
```

models.py `EntryDecision` — old (line 532):
```python
    min_net_ev: Optional[float] = None


@dataclass
class BacktestResult:
```
new:
```python
    min_net_ev: Optional[float] = None
    # WAVE 1 (2026-09-17): the deciding numbers, set at every return site in
    # entry_manager.evaluate_entry / apply_portfolio_budget and mirrored by
    # backtest/entry_sim.py. Persisted to entry_decisions every cycle and,
    # for a fill, onto Position. See entry_manager.deciding_numbers().
    calibrated_prob: Optional[float] = None
    calibration_source: str = "uncalibrated"
    admission_edge: Optional[float] = None
    sizing_edge: Optional[float] = None
    # recommended_size_usd BEFORE the live $1.00 clamp and before the
    # exchange 5-share bump -- what the paper path would have staked. None
    # when nothing was sized (a pre-sizing veto).
    kelly_size_preclamp_usd: Optional[float] = None
    # A stable code for WHICH rule produced this decision -- one of
    # entry_manager.ENTRY_RULE_IDS. `reason` stays the prose beside it;
    # nothing parses prose any more. "unspecified" is the value for a
    # decision built outside the gate chain (manual_trigger), and a census
    # test asserts no gate site ever leaves it there.
    rule_id: str = "unspecified"


@dataclass
class BacktestResult:
```

storage.py `_row_to_position` — old:
```python
    trigger_price = r[24] if len(r) > 24 else None
    return Position(
```
new:
```python
    trigger_price = r[24] if len(r) > 24 else None
    calibrated_prob = r[25] if len(r) > 25 else None
    calibration_source = r[26] if len(r) > 26 else None
    admission_edge = r[27] if len(r) > 27 else None
    sizing_edge = r[28] if len(r) > 28 else None
    kelly_size_preclamp_usd = r[29] if len(r) > 29 else None
    return Position(
```
and old:
```python
        entry_fee_per_share=entry_fee_per_share,
        trigger_price=trigger_price,
    )
```
new:
```python
        entry_fee_per_share=entry_fee_per_share,
        trigger_price=trigger_price,
        calibrated_prob=calibrated_prob,
        calibration_source=calibration_source,
        admission_edge=admission_edge,
        sizing_edge=sizing_edge,
        kelly_size_preclamp_usd=kelly_size_preclamp_usd,
    )
```

storage.py `open_position` — old:
```python
                model_prob, raw_edge, net_ev_at_size, entry_bid,
                exit_blocked_reason, entry_fee_per_share
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
```
new:
```python
                model_prob, raw_edge, net_ev_at_size, entry_bid,
                exit_blocked_reason, entry_fee_per_share,
                calibrated_prob, calibration_source, admission_edge,
                sizing_edge, kelly_size_preclamp_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?)
            """,
```
and old:
```python
                _entry_fee_for(position),
            ),
        )
```
new:
```python
                _entry_fee_for(position),
                position.calibrated_prob,
                position.calibration_source,
                position.admission_edge,
                position.sizing_edge,
                position.kelly_size_preclamp_usd,
            ),
        )
```

executor.py `_position` closure inside `open_position` — old (line 866-870):
```python
            model_prob=decision.model_prob,
            raw_edge=decision.raw_edge,
            net_ev_at_size=decision.net_ev_at_size,
            exit_blocked_reason=exit_blocked_reason,
        )
```
new:
```python
            model_prob=decision.model_prob,
            raw_edge=decision.raw_edge,
            net_ev_at_size=decision.net_ev_at_size,
            exit_blocked_reason=exit_blocked_reason,
            # WAVE 1: the deciding numbers, copied for the same reason
            # model_prob is -- see Position.calibrated_prob.
            calibrated_prob=decision.calibrated_prob,
            calibration_source=decision.calibration_source,
            admission_edge=decision.admission_edge,
            sizing_edge=decision.sizing_edge,
            kelly_size_preclamp_usd=decision.kelly_size_preclamp_usd,
        )
```

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave1_deciding_numbers_persisted.py -v` / Expected: PASS (6 passed)
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1687
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add models.py storage.py executor.py tests/test_wave1_deciding_numbers_persisted.py
git commit -m "Wave 1: EntryDecision and Position carry the deciding numbers; storage and executor persist them

Six new EntryDecision fields (calibrated_prob, calibration_source,
admission_edge, sizing_edge, kelly_size_preclamp_usd, rule_id) and the
first five on Position, all defaulting to unknown. storage.open_position
writes them, _row_to_position reads them at positions 25-29, and
executor.open_position copies them off the decision the way it already
copies model_prob. Nothing sets them yet, so every row still reads None.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: entry_manager and entry_sim stamp `rule_id` and the deciding numbers at every return site
**Files:** Modify `entry_manager.py` (constants after line 120; helpers after `sizing_edge` ~line 346; `collection_only_decision` 805-825; `evaluate_entry` 828-1290; `veto_same_bucket_conflicts` 1339-1378; `apply_portfolio_budget` 1435-1530), `backtest/entry_sim.py` (imports 116-127; `evaluate_entry_sim` 154-478) / Test: modify `tests/test_gate_census.py`, create `tests/test_wave1_deciding_numbers_carried.py`
**Interfaces:** Consumes: Task 2 fields / Produces: `entry_manager.ENTRY_RULE_IDS: frozenset[str]`, `entry_manager.deciding_numbers(ev_result) -> dict` (keys `calibrated_prob, calibration_source, admission_edge, sizing_edge`), `entry_manager.preclamp_size_usd(size_usd: float, depth_usd: Optional[float]) -> float`; every `EntryDecision` leaving `decide_portfolio_entries` / `decide_portfolio_entries_sim` has `rule_id in ENTRY_RULE_IDS`

Rule ids by site (spec 1b's set, plus three the spec's prose implies but its list omits — see deviations at the end):

| site | rule_id |
|---|---|
| Veto 00 price ceiling | `00` |
| Veto 00b blocked band | `00b` |
| Veto 00c NO-side floor | `00c` |
| Veto 0a plausibility | `0a` |
| Veto 0a2 materiality | `0a2` |
| Veto 0b unreadable / cap | `0b` |
| Veto 0b2 unreadable / opposite open | `0b2` |
| Veto 0c unreadable / cooldown | `0c` |
| Kelly <= 0 | `kelly_nonpositive` |
| depth unknown | `depth` |
| depth-capped < $1 | `size_floor` |
| slippage gate | `slippage` |
| net EV at size | `net_ev_bar` |
| approve | `approved` |
| collection_only_decision | `collection_gate` |
| veto_same_bucket_conflicts | `same_bucket_conflict` |
| apply_portfolio_budget exhausted | `budget_exhausted` |
| apply_portfolio_budget scaled | `budget_scaled` |

- [x] **Step 1: Write the failing test**

Append to `tests/test_gate_census.py`:

```python
# --------------------------------------------------------------------------
# Wave 1: every decision site stamps a rule_id from ENTRY_RULE_IDS
# --------------------------------------------------------------------------

def _rule_id_literals(fn):
    """
    The literal rule_id at every decision-returning statement in fn --
    `return EntryDecision(...)` and `return _rejected(...)`. The nested
    _rejected factory passes `rule_id=rule_id` (a Name); that is the shape,
    not a site, and is skipped. Any other site without a literal fails.
    """
    tree = ast.parse(inspect.cleandoc(inspect.getsource(fn)))
    literals = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Name) and call.func.id in ("EntryDecision", "_rejected")):
            continue
        by_name = {k.arg: k.value for k in call.keywords if k.arg is not None}
        assert "rule_id" in by_name, f"{fn.__qualname__} line {node.lineno}: decision site has no rule_id"
        value = by_name["rule_id"]
        if isinstance(value, ast.Name) and value.id == "rule_id":
            continue  # the _rejected factory forwarding its argument
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{fn.__qualname__} line {node.lineno}: rule_id must be a string literal"
        )
        literals.append(value.value)
    return literals


def _dict_rule_ids(fn):
    """rule_id literals written through `EntryDecision(**{**d.__dict__, "rule_id": ...})`."""
    tree = ast.parse(inspect.cleandoc(inspect.getsource(fn)))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "rule_id":
                assert isinstance(value, ast.Constant), f"{fn.__qualname__}: rule_id must be a literal"
                found.append(value.value)
    return found


def test_every_live_gate_site_has_a_known_rule_id():
    ids = _rule_id_literals(entry_manager.evaluate_entry)
    assert len(ids) == entry_sim.GATE_COUNT
    unknown = set(ids) - entry_manager.ENTRY_RULE_IDS
    assert not unknown, f"rule ids not in ENTRY_RULE_IDS: {unknown}"
    assert "approved" in ids


def test_entry_sim_uses_exactly_the_same_rule_ids_in_the_same_order():
    live = _rule_id_literals(entry_manager.evaluate_entry)
    sim = _rule_id_literals(entry_sim.evaluate_entry_sim)
    assert live == sim, f"live/sim rule_id sequences differ:\n live={live}\n sim ={sim}"


def test_budget_veto_and_collection_sites_have_rule_ids():
    assert set(_dict_rule_ids(entry_manager.apply_portfolio_budget)) == {"budget_exhausted", "budget_scaled"}
    assert set(_dict_rule_ids(entry_manager.veto_same_bucket_conflicts)) == {"same_bucket_conflict"}
    assert _rule_id_literals(entry_manager.collection_only_decision) == ["collection_gate"]


def test_the_id_set_is_closed():
    """Every id any site uses is declared, and every declared id is used somewhere."""
    used = set(_rule_id_literals(entry_manager.evaluate_entry))
    used |= set(_dict_rule_ids(entry_manager.apply_portfolio_budget))
    used |= set(_dict_rule_ids(entry_manager.veto_same_bucket_conflicts))
    used |= set(_rule_id_literals(entry_manager.collection_only_decision))
    assert used == entry_manager.ENTRY_RULE_IDS
```

Create `tests/test_wave1_deciding_numbers_carried.py`:

```python
"""
Wave 1: the numbers an EntryDecision carries are the numbers its gates
compared -- not a recomputation, and not the raw ones under a new name.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from clients import market_client
from models import EVResult


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", True)


def _ev(calibrated=None, source="uncalibrated", model_prob=0.55, price=0.35, side="YES"):
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side=side,
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02,
        net_ev_per_dollar=(model_prob - price) / price - 0.03, spread_source="ensemble",
        market_bid=price - 0.02, calibrated_prob=calibrated, calibration_source=source,
    )


def test_calibrated_numbers_are_carried_verbatim(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    ev = _ev(calibrated=0.50, source="pooled_isotonic")

    d = entry_manager.evaluate_entry(ev, "TOK", min_net_ev=0.15)

    assert d.approved and d.rule_id == "approved"
    assert d.calibrated_prob == pytest.approx(0.50)
    assert d.calibration_source == "pooled_isotonic"
    assert d.admission_edge == pytest.approx(entry_manager.admission_edge(ev))
    assert d.sizing_edge == pytest.approx(entry_manager.sizing_edge(ev))
    assert d.admission_edge == pytest.approx(0.15)      # calibrated - price, not raw
    assert d.raw_edge == pytest.approx(0.20)            # raw stays raw
    assert d.model_prob == pytest.approx(0.55)


def test_uncalibrated_numbers_fall_back_to_raw_and_say_so(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert d.calibrated_prob is None
    assert d.calibration_source == "uncalibrated"
    assert d.admission_edge == pytest.approx(0.20)
    assert d.sizing_edge == pytest.approx(0.20)


def test_a_pre_sizing_veto_carries_the_numbers_but_no_size(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(price=0.90, model_prob=0.95), "TOK", min_net_ev=0.15)
    assert d.rule_id == "00"
    assert d.admission_edge == pytest.approx(0.05)
    assert d.kelly_size_preclamp_usd is None


def test_the_0a2_veto_records_the_edge_it_compared(monkeypatch):
    """Calibrated 0.36 vs price 0.35 is 0.01 -- under MIN_ABS_RAW_EDGE (0.03) -- while raw is 0.20."""
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(calibrated=0.36, source="station_isotonic"), "TOK", min_net_ev=0.15)
    assert d.rule_id == "0a2"
    assert d.admission_edge == pytest.approx(0.01)


def test_preclamp_size_equals_the_paper_recommended_size(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert d.approved
    assert d.kelly_size_preclamp_usd == pytest.approx(d.recommended_size_usd)
    assert d.kelly_size_preclamp_usd > config.LIVE_TRADE_SIZE_USD


def test_preclamp_size_survives_the_live_clamp(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert d.approved
    assert d.recommended_size_usd == pytest.approx(config.LIVE_TRADE_SIZE_USD)
    assert d.kelly_size_preclamp_usd > config.LIVE_TRADE_SIZE_USD


def test_preclamp_size_is_depth_capped_where_depth_is_known(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 20.0)
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert d.kelly_size_preclamp_usd == pytest.approx(20.0 * config.MAX_DEPTH_UTILIZATION_PCT)


def test_preclamp_helper_rounds_like_the_approval_size():
    assert entry_manager.preclamp_size_usd(12.3456, None) == 12.35
    assert entry_manager.preclamp_size_usd(12.3456, 8.0) == round(8.0 * config.MAX_DEPTH_UTILIZATION_PCT, 2)


def test_collection_gate_decision_is_stamped():
    ev = _ev(calibrated=0.5, source="pooled_isotonic")
    d = entry_manager.collection_only_decision(ev, "TOK", "Collection-only: test")
    assert d.rule_id == "collection_gate"
    assert d.calibrated_prob == pytest.approx(0.5)
    assert d.admission_edge == pytest.approx(0.15)


def test_budget_sites_relabel_but_keep_the_numbers(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    d = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    scaled = entry_manager.apply_portfolio_budget([d], max_total_usd=d.recommended_size_usd / 2)
    assert scaled[0].approved and scaled[0].rule_id == "budget_scaled"
    assert scaled[0].kelly_size_preclamp_usd == pytest.approx(d.kelly_size_preclamp_usd)
    exhausted = entry_manager.apply_portfolio_budget([d], max_total_usd=10.0, existing_exposure_usd=10.0)
    assert not exhausted[0].approved and exhausted[0].rule_id == "budget_exhausted"
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_gate_census.py tests/test_wave1_deciding_numbers_carried.py -v` / Expected: FAIL with `AttributeError: module 'entry_manager' has no attribute 'ENTRY_RULE_IDS'` and `decision site has no rule_id`

- [x] **Step 3: Write minimal implementation**

entry_manager.py — after `max_plausible_edge_for = config.max_plausible_edge_for` (line 120):

```python
# WAVE 1 (2026-09-17). The closed set of EntryDecision.rule_id values. Every
# `return EntryDecision(...)` / `return _rejected(...)` site in evaluate_entry,
# the budget and same-bucket sites, and collection_only_decision stamps one of
# these; backtest/entry_sim.py stamps the SAME ones at the same sites, so the
# replay funnel and the live funnel share a key (tests/test_gate_census.py).
# The prose `reason` stays beside it -- nothing parses prose any more.
ENTRY_RULE_IDS = frozenset({
    "00", "00b", "00c", "0a", "0a2", "0b", "0b2", "0c",
    "kelly_nonpositive", "depth", "size_floor", "slippage", "net_ev_bar",
    "approved", "collection_gate", "same_bucket_conflict",
    "budget_exhausted", "budget_scaled",
})
```

entry_manager.py — after the `sizing_edge` function body (its last line is `return _calibrated_or_raw_edge(ev_result)`, line ~346):

```python
def deciding_numbers(ev_result: EVResult) -> dict:
    """
    The four Wave 1 fields every EntryDecision carries off its EVResult,
    as keyword arguments: the calibrated probability and its tier exactly
    as the EV table stamped them, the edge veto 0a2 is measured against,
    and the edge Kelly sizes on. ONE computation per candidate -- the same
    values evaluate_entry then compares, so what is recorded is what
    decided, not a recomputation that could drift. getattr for the
    duck-typed stubs the replay feeds, as _calibrated_or_raw_edge explains.
    """
    return {
        "calibrated_prob": getattr(ev_result, "calibrated_prob", None),
        "calibration_source": getattr(ev_result, "calibration_source", probability_calibration.NO_TIER),
        "admission_edge": admission_edge(ev_result),
        "sizing_edge": sizing_edge(ev_result),
    }


def preclamp_size_usd(size_usd: float, depth_usd: Optional[float]) -> float:
    """
    EntryDecision.kelly_size_preclamp_usd: recommended_size_usd as the PAPER
    path would have produced it -- the Kelly size after every risk cap and the
    haircut, BEFORE the live fixed-size clamp (config.LIVE_TRADE_SIZE_USD) and
    before the exchange minimum bump, depth-capped where depth is known and
    rounded like the approval size. On a paper station it equals
    recommended_size_usd before budget scaling; on a live station it is the
    stake the same ticket would have carried on paper. Shared with
    backtest/entry_sim.py so the two cannot drift.
    """
    if depth_usd is not None:
        size_usd = min(size_usd, depth_usd * config.MAX_DEPTH_UTILIZATION_PCT)
    return round(size_usd, 2)
```

entry_manager.py `collection_only_decision` — old:
```python
        model_prob=ev_result.model_prob,
        raw_edge=ev_result.raw_edge,
    )


def evaluate_entry(
```
new:
```python
        model_prob=ev_result.model_prob,
        raw_edge=ev_result.raw_edge,
        **deciding_numbers(ev_result),
        rule_id="collection_gate",
    )


def evaluate_entry(
```

entry_manager.py `evaluate_entry` — the nested factory, old:
```python
    station_icao = ev_result.station_icao
    maturity = config.STATION_MATURITY.get(station_icao, "exploratory")

    def _rejected(reason: str) -> EntryDecision:
        """Uniform shape for a pre-sizing rejection -- nothing was sized, so every sizing field is empty."""
        return EntryDecision(
```
new:
```python
    station_icao = ev_result.station_icao
    maturity = config.STATION_MATURITY.get(station_icao, "exploratory")
    # WAVE 1: computed once, compared below, carried on every return.
    deciding = deciding_numbers(ev_result)

    def _rejected(reason: str, rule_id: str) -> EntryDecision:
        """Uniform shape for a pre-sizing rejection -- nothing was sized, so every sizing field is empty."""
        return EntryDecision(
```
and the factory's trailing kwargs, old:
```python
            raw_edge=ev_result.raw_edge,
            min_net_ev=min_net_ev,
        )

    # Veto 00: entry price ceiling.
```
new:
```python
            raw_edge=ev_result.raw_edge,
            min_net_ev=min_net_ev,
            **deciding,
            rule_id=rule_id,
        )

    # Veto 00: entry price ceiling.
```

Each `_rejected(...)` call gains a trailing `rule_id=` keyword; the reason text is unchanged. The eleven sites:

```python
        return _rejected(
            f"Entry price {ev_result.market_price:.3f} above MAX_ENTRY_PRICE "
            f"({config.MAX_ENTRY_PRICE:.2f}) -- too little upside left to justify the stake.",
            rule_id="00",
        )
```
```python
        return _rejected(
            f"Entry price {ev_result.market_price:.3f} is inside the blocked "
            f"{low:.2f}-{high:.2f} band (ENTRY_PRICE_BLOCK_BAND).",
            rule_id="00b",
        )
```
```python
        return _rejected(
            f"model_prob {ev_result.model_prob:.3f} is below the NO-side confidence floor "
            f"({config.NO_SIDE_MIN_MODEL_PROB:.2f}, NO_SIDE_MIN_MODEL_PROB) -- the model is barely "
            f"better than a coin flip here and this cohort loses held to settlement.",
            rule_id="00c",
        )
```
```python
        return _rejected(
            f"VETOED: raw edge {raw_edge:+.1%} exceeds the plausibility ceiling "
            f"({max_plausible_edge:.1%} at price {ev_result.market_price}) -- presumed data error, not alpha.",
            rule_id="0a",
        )
```
Veto 0a2 — old `gate_edge = admission_edge(ev_result)` becomes `gate_edge = deciding["admission_edge"]` (the same call's result, so the recorded number IS the compared number), and:
```python
        return _rejected(
            f"Absolute edge {gate_edge:+.3f} below required minimum {min_abs_edge:.3f}{low_conf_note}{basis_note} "
            f"-- inside book noise, not a tradeable disagreement.",
            rule_id="0a2",
        )
```
```python
        return _rejected("Open positions unreadable -- per-bucket cap unenforceable, refusing to open blind.", rule_id="0b")
```
```python
        return _rejected(
            f"Per-bucket cap: {open_count} position(s) already open on this bucket/side "
            f"(max {config.MAX_OPEN_POSITIONS_PER_BUCKET}).",
            rule_id="0b",
        )
```
```python
        return _rejected(
            "Open positions unreadable -- opposite-side lock unenforceable, refusing to open blind.",
            rule_id="0b2",
        )
```
```python
        return _rejected(
            f"Opposite side already open: {opposite_count} {opposite_side} position(s) on this bucket.",
            rule_id="0b2",
        )
```
```python
        return _rejected("Position history unreadable -- stop-out cooldown unenforceable, refusing to open blind.", rule_id="0c")
```
```python
        return _rejected(
            f"Stop-out cooldown: {stop_outs} stop-loss exit(s) on this bucket/side today "
            f"(max {config.MAX_STOP_OUTS_PER_BUCKET_PER_DAY}).",
            rule_id="0c",
        )
```

The six direct `return EntryDecision(...)` sites each gain, after `min_net_ev=min_net_ev,`, the lines shown. Kelly site:
```python
            min_net_ev=min_net_ev,
            **deciding,
            rule_id="kelly_nonpositive",
        )
```
Then capture the pre-clamp size. Old:
```python
    live_cap = live_size_cap_usd(station_icao)
    if live_cap is not None:
        size_usd = min(size_usd, live_cap)
```
new:
```python
    # WAVE 1: the paper-equivalent stake, captured BEFORE the live clamp.
    preclamp_usd = size_usd

    live_cap = live_size_cap_usd(station_icao)
    if live_cap is not None:
        size_usd = min(size_usd, live_cap)
```
depth-unknown site:
```python
            min_net_ev=min_net_ev,
            **deciding,
            kelly_size_preclamp_usd=preclamp_size_usd(preclamp_usd, None),
            rule_id="depth",
        )
```
depth-too-thin site:
```python
            min_net_ev=min_net_ev,
            **deciding,
            kelly_size_preclamp_usd=preclamp_size_usd(preclamp_usd, depth_usd),
            rule_id="size_floor",
        )
```
slippage site: same three lines with `rule_id="slippage"`; net-EV site: `rule_id="net_ev_bar"`; approve site: `rule_id="approved"` (all with `kelly_size_preclamp_usd=preclamp_size_usd(preclamp_usd, depth_usd)`).

`veto_same_bucket_conflicts` — old:
```python
                    result.append(EntryDecision(
                        **{**d.__dict__, "approved": False,
                           "reason": "VETOED: same-bucket YES+NO conflict -- see entry_manager logs."}
                    ))
```
new:
```python
                    result.append(EntryDecision(
                        **{**d.__dict__, "approved": False,
                           "rule_id": "same_bucket_conflict",
                           "reason": "VETOED: same-bucket YES+NO conflict -- see entry_manager logs."}
                    ))
```
`apply_portfolio_budget` exhausted branch — old:
```python
                result.append(EntryDecision(
                    **{**d.__dict__,
                       "approved": False,
                       "recommended_size_usd": 0.0,
                       "reason": d.reason + f" [rejected: {binding_label} budget exhausted "
```
new:
```python
                result.append(EntryDecision(
                    **{**d.__dict__,
                       "approved": False,
                       "recommended_size_usd": 0.0,
                       "rule_id": "budget_exhausted",
                       "reason": d.reason + f" [rejected: {binding_label} budget exhausted "
```
scaled branch — old:
```python
                result.append(EntryDecision(
                    **{**d.__dict__,
                       "recommended_size_usd": round(d.recommended_size_usd * scale, 2),
```
new:
```python
                result.append(EntryDecision(
                    **{**d.__dict__,
                       "recommended_size_usd": round(d.recommended_size_usd * scale, 2),
                       "rule_id": "budget_scaled",
```

backtest/entry_sim.py imports — old:
```python
from entry_manager import (
    _calibration_note,
    admission_edge,
    compute_kelly_fraction,
```
new:
```python
from entry_manager import (
    _calibration_note,
    admission_edge,
    compute_kelly_fraction,
    deciding_numbers,
    preclamp_size_usd,
```
`evaluate_entry_sim` — after `maturity = _maturity_for(station_icao, station_maturity)` add `deciding = deciding_numbers(ev)`; `def _rejected(reason: str, rule_id: str)` with `**deciding, rule_id=rule_id,` after `min_net_ev=min_net_ev,`; every `_rejected(...)` call gets the same `rule_id="..."` as the live site it mirrors — gate 0 → `"00"`, 0b → `"00b"`, 0c → `"00c"`, 1 → `"0a"`, 2 → `"0a2"`, 3 → `"0b"`, 4 → `"0b"`, 4b → `"0b2"`, 4c → `"0b2"`, 5 → `"0c"`, 6 → `"0c"`; gate 2's `gate_edge = admission_edge(ev)` becomes `gate_edge = deciding["admission_edge"]`; gate 7 adds `**deciding, rule_id="kelly_nonpositive",`; after the haircut block add `preclamp_usd = size_usd`; gates 8-12 add `**deciding, kelly_size_preclamp_usd=preclamp_size_usd(preclamp_usd, depth_usd), rule_id=...` with `depth`, `size_floor`, `slippage`, `net_ev_bar`, `approved` (gate 8 passes `preclamp_size_usd(preclamp_usd, None)` since `depth_usd` is None there). `GATE_COUNT` stays 17.

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_gate_census.py tests/test_wave1_deciding_numbers_carried.py tests/test_parity_entry.py -v` / Expected: PASS (the parity test is the proof that live and sim stamp identical values at every gate)
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1701
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add entry_manager.py backtest/entry_sim.py tests/test_gate_census.py tests/test_wave1_deciding_numbers_carried.py
git commit -m "Wave 1: stamp rule_id and the deciding numbers at every entry decision site

ENTRY_RULE_IDS is the closed id set; deciding_numbers() computes the
calibrated probability, its tier, the admission edge and the sizing edge
once per candidate and evaluate_entry compares the same values it
carries. kelly_size_preclamp_usd is captured before the live clamp via
preclamp_size_usd(). entry_sim mirrors every site; the field-parity test
proves live and replay stamp identical values. No decision changes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Order-path rows store `net_ev_at_size` at the resolved size
**Files:** Modify `executor.py` (`_resolved_size_ok` signature 261 and the `net_ev = ...` line 377; `_position` closure 828; `_open_via_order_path` 972, 1024-1028, 1051-1057) / Test `tests/test_wave1_resolved_net_ev.py`
**Interfaces:** Consumes: Task 2 / Produces: `executor._resolved_size_ok(spec, decision, out: Optional[dict] = None) -> tuple` writing `out["net_ev_at_size"]` when it computes one; `make_position(..., net_ev_at_size=None)` closure parameter (Task 6 reuses `out`)

- [x] **Step 1: Write the failing test**

```python
# tests/test_wave1_resolved_net_ev.py
"""
Wave 1 (spec 1a, last paragraph): net_ev_at_size on a live row is the figure
_resolved_size_ok computed at the RESOLVED size, not the $1.00 figure the
decision carried. Only slippage moves with size, plus the limit pad.
"""
from datetime import date

import pytest

import config
import executor
import storage
from clients import market_client, wallet_client
from models import EntryDecision


def _decision(net_ev=0.30, slip=0.01, price=0.30):
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=1.0, available_depth_usd=1000.0,
        slippage_at_size_pct=slip, net_ev_at_size=net_ev,
        approved=True, reason="test", station_maturity="mature",
        entry_price=price, token_id="TOK", min_net_ev=0.15,
    )


@pytest.fixture
def live(monkeypatch):
    opened, specs = [], []
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    monkeypatch.setattr(storage, "open_position", lambda p: opened.append(p))
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(storage, "record_live_order_attempt", lambda **kw: None)
    monkeypatch.setattr(storage, "count_live_order_attempts", lambda kind, since, station_icaos=None: 0)
    monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
    monkeypatch.setattr(wallet_client, "_book_constraints", lambda token_id: ("0.01", None))
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions, **_: wallet_client.Reconciliation(ok=True, checked=True, reason="stubbed"),
    )
    real_build = wallet_client.build_entry_order

    def _build(**kw):
        spec = real_build(**kw)
        specs.append(spec)
        return spec

    monkeypatch.setattr(wallet_client, "build_entry_order", _build)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.03)
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=True, simulated=False, spec=spec,
            order_id="0xabc", fill_price=spec.expected_price, fill_shares=spec.size_shares,
        ),
    )
    return {"opened": opened, "specs": specs}


def test_live_row_stores_net_ev_at_the_resolved_size(live):
    decision = _decision(net_ev=0.30, slip=0.01)

    executor.open_position(decision)

    (pos,) = live["opened"]
    (spec,) = live["specs"]
    expected = 0.30 - (0.03 - 0.01) - spec.pad_cost_pct
    assert pos.net_ev_at_size == pytest.approx(expected)
    assert pos.net_ev_at_size < decision.net_ev_at_size


def test_a_decision_without_a_figure_keeps_none(live):
    executor.open_position(_decision(net_ev=None, slip=None))
    (pos,) = live["opened"]
    assert pos.net_ev_at_size is None


def test_resolved_size_ok_reports_the_figure_through_out(monkeypatch):
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.02)
    spec = wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=0.30, size_shares=5.0,
        notional_usd=1.5, expected_price=0.30,
    )
    out = {}
    ok, _ = executor._resolved_size_ok(spec, _decision(net_ev=0.30, slip=0.01), out=out)
    assert ok
    assert out["net_ev_at_size"] == pytest.approx(0.30 - 0.01)


def test_resolved_size_ok_still_works_without_out(monkeypatch):
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.02)
    spec = wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=0.30, size_shares=5.0,
        notional_usd=1.5, expected_price=0.30,
    )
    ok, note = executor._resolved_size_ok(spec, _decision())
    assert ok and "net EV" in note
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave1_resolved_net_ev.py -v` / Expected: FAIL with `TypeError: _resolved_size_ok() got an unexpected keyword argument 'out'` and `assert 0.30 == approx(0.28...)`

- [x] **Step 3: Write minimal implementation**

executor.py — old:
```python
def _resolved_size_ok(spec, decision) -> tuple:
```
new:
```python
def _resolved_size_ok(spec, decision, out: Optional[dict] = None) -> tuple:
```
and inside its docstring's end add one paragraph:
```python
    `out`, when given, receives "net_ev_at_size": the net EV re-derived at the
    resolved size (WAVE 1) -- the figure the stored live row now carries
    instead of the $1.00 one. Only written on the branch that computes it.
```
old:
```python
        pad_cost = getattr(spec, "pad_cost_pct", 0.0)
        net_ev = decision.net_ev_at_size - (slippage - decision.slippage_at_size_pct) - pad_cost
```
new:
```python
        pad_cost = getattr(spec, "pad_cost_pct", 0.0)
        net_ev = decision.net_ev_at_size - (slippage - decision.slippage_at_size_pct) - pad_cost
        if out is not None:
            out["net_ev_at_size"] = net_ev
```

`open_position`'s closure — old:
```python
    def _position(size_usd: float, size_shares=None, entry_price=None, order_id=None,
                  exit_blocked_reason=None) -> Position:
```
new:
```python
    def _position(size_usd: float, size_shares=None, entry_price=None, order_id=None,
                  exit_blocked_reason=None, net_ev_at_size=None) -> Position:
```
and old:
```python
            net_ev_at_size=decision.net_ev_at_size,
            exit_blocked_reason=exit_blocked_reason,
```
new:
```python
            # WAVE 1: the order path passes the figure re-derived at the
            # RESOLVED size; the paper/manual paths pass nothing and keep the
            # decision's own figure, which is the size they record at.
            net_ev_at_size=net_ev_at_size if net_ev_at_size is not None else decision.net_ev_at_size,
            exit_blocked_reason=exit_blocked_reason,
```

`_open_via_order_path` — old:
```python
    size_ok, size_note = _resolved_size_ok(spec, decision)
```
new:
```python
    resolved = {}
    size_ok, size_note = _resolved_size_ok(spec, decision, out=resolved)
```
simulation record — old:
```python
        storage.open_position(make_position(
            size_usd=spec.notional_usd,
            size_shares=spec.size_shares,
            entry_price=spec.expected_price,
        ))
        return
```
new:
```python
        storage.open_position(make_position(
            size_usd=spec.notional_usd,
            size_shares=spec.size_shares,
            entry_price=spec.expected_price,
            net_ev_at_size=resolved.get("net_ev_at_size"),
        ))
        return
```
live record — old:
```python
        order_id=result.order_id,
        exit_blocked_reason=blocked,
    ))
```
new:
```python
        order_id=result.order_id,
        exit_blocked_reason=blocked,
        net_ev_at_size=resolved.get("net_ev_at_size"),
    ))
```

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave1_resolved_net_ev.py tests/test_live_execution.py tests/test_entry_bar_basis.py -v` / Expected: PASS
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1705
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add executor.py tests/test_wave1_resolved_net_ev.py
git commit -m "Wave 1: order-path rows store net_ev_at_size at the resolved size

_resolved_size_ok already re-derives net EV at the notional the exchange
minimum forces; it now hands that figure back through an optional out
dict and the simulation/live rows record it instead of the \$1.00 figure.
Paper and manual_review rows are untouched. No gate moves.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `storage.record_entry_decisions` and the scheduler records before the executor
**Files:** Modify `storage.py` (new functions after `load_live_order_attempts`, end of file), `scheduler.py` (new helpers after `_last_collection_ts` at 96; `_run_full_cycle` 380-411) / Test `tests/test_wave1_entry_decisions_recorded.py`
**Interfaces:** Consumes: `ENTRY_DECISION_COLUMNS` (Task 1), Task 3 fields / Produces: `storage.record_entry_decisions(decisions, *, book: str, cycle_ts: str, config_sha: Optional[str]) -> int`, `storage.load_entry_decisions(book: Optional[str] = None, cycle_ts: Optional[str] = None, limit: int = 1000) -> List[dict]`, `scheduler._config_sha() -> Optional[str]`, `scheduler._record_entry_decisions(decisions, station_icao, book, cycle_ts) -> None` (Task 8 reuses it), `cycle_ts`/`book` locals in `_run_full_cycle`

- [x] **Step 1: Write the failing test**

```python
# tests/test_wave1_entry_decisions_recorded.py
"""
Wave 1 (spec 1b, 1e): every EntryDecision of a cycle becomes an
entry_decisions row, written by _run_full_cycle right after
decide_portfolio_entries and BEFORE any executor call; a storage failure
there never blocks a trade; and recording changes no decision.
"""
import dataclasses
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

import config
import entry_manager
import ev_engine
import executor
import scheduler
import storage
from clients import market_client
from models import EntryDecision, EVResult

STATION = "WSSS"
TARGET = date(2026, 9, 17)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    storage._connect().close()
    return config.DB_PATH


def _ev(bucket=32, side="YES", model_prob=0.55, price=0.35):
    return EVResult(
        station_icao=STATION, target_date=TARGET, bucket_c=bucket, side=side,
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02,
        net_ev_per_dollar=(model_prob - price) / price - 0.03, spread_source="ensemble",
        market_bid=price - 0.02,
    )


def _token_map():
    return {b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in (31, 32, 33)}


@pytest.fixture
def cycle(monkeypatch, temp_db):
    """
    _run_full_cycle with every network seam stubbed and the REAL
    entry_manager path (gates, sizing, budget) running on a WSSS that has
    graduated the collection gate. Returns what executor.open_position saw.
    """
    seen = {"opened": [], "order": []}
    monkeypatch.setattr(executor, "EXECUTION_MODE", {icao: "paper" for icao in config.STATIONS})
    monkeypatch.setattr(scheduler.pipeline, "run",
                        lambda station_icao, forecast_bias_c=0.0: {"estimate": SimpleNamespace(inputs_used=["open_meteo_ecmwf"])})
    monkeypatch.setattr(scheduler.pipeline, "print_summary", lambda r: None)
    monkeypatch.setattr(scheduler, "_run_exit_check", lambda *a, **kw: None)
    monkeypatch.setattr(ev_engine, "save_ev_snapshot", lambda icao, results: None)
    # Two candidates that both clear the best_opportunities screen: 32 YES
    # approves; 33 YES (raw edge 0.60 > the 0.25 plausibility ceiling) is
    # refused INSIDE evaluate_entry by veto 0a, so it gets a row too.
    ev_run = ev_engine.StationEVRun(
        station_icao=STATION, target_date=TARGET, token_map=_token_map(),
        bucket_min_c=31, bucket_max_c=33,
        ev_results=[_ev(32), _ev(33, model_prob=0.95)],
    )
    monkeypatch.setattr(ev_engine, "run_for_station_with_map", lambda estimate, **kw: ev_run)
    # The collection gate, satisfied.
    monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.1, 20, 0.1))
    monkeypatch.setattr(entry_manager, "resolution_obs_count", lambda icao: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY)
    monkeypatch.setattr(entry_manager, "forecast_bias_source_mix", lambda icao: None)
    monkeypatch.setattr(entry_manager, "station_error_width_ratio", lambda icao: None)
    # Sizing I/O.
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)

    def _open(decision):
        seen["order"].append("open")
        seen["opened"].append(decision)

    monkeypatch.setattr(executor, "open_position", _open)
    real_record = storage.record_entry_decisions

    def _record(decisions, **kw):
        seen["order"].append("record")
        return real_record(decisions, **kw)

    monkeypatch.setattr(storage, "record_entry_decisions", _record)
    return seen


def _rows(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM entry_decisions ORDER BY bucket_c")]
    finally:
        con.close()


def test_one_row_per_decision_written_before_the_executor(cycle, temp_db):
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    rows = _rows(temp_db)
    assert len(rows) == 2 == len(cycle["opened"])
    assert cycle["order"][0] == "record" and cycle["order"][1:] == ["open", "open"]
    by_bucket = {r["bucket_c"]: r for r in rows}
    assert by_bucket[32]["approved"] == 1 and by_bucket[32]["rule_id"] == "approved"
    assert by_bucket[33]["approved"] == 0 and by_bucket[33]["rule_id"] == "0a"
    assert {r["book"] for r in rows} == {"paper"}
    assert len({r["cycle_ts"] for r in rows}) == 1
    assert by_bucket[32]["recommended_size_usd"] == pytest.approx(cycle["opened"][0].recommended_size_usd)
    assert by_bucket[32]["admission_edge"] == pytest.approx(0.20)
    assert by_bucket[32]["min_net_ev"] == pytest.approx(0.15)
    assert by_bucket[32]["target_date"] == "2026-09-17"


def test_a_storage_failure_logs_and_the_executor_still_runs(cycle, monkeypatch, capsys):
    def _boom(decisions, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "record_entry_decisions", _boom)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert len(cycle["opened"]) == 2
    assert "could not record entry decisions" in capsys.readouterr().out


def test_decisions_are_identical_with_recording_on_and_off(cycle, monkeypatch):
    def _tuples():
        return sorted((d.bucket_c, d.side, d.approved, d.recommended_size_usd, d.reason) for d in cycle["opened"])

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)
    with_recording = _tuples()
    cycle["opened"].clear()

    monkeypatch.setattr(storage, "record_entry_decisions", lambda decisions, **kw: 0)
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)
    without_recording = _tuples()

    assert with_recording == without_recording
    assert any(approved for _, _, approved, _, _ in with_recording)


def test_config_sha_is_recorded_and_cached(cycle, temp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "_current_git_sha", lambda: calls.append(1) or "abc123")
    monkeypatch.setattr(scheduler, "_config_sha_cache", {})

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert {r["config_sha"] for r in _rows(temp_db)} == {"abc123"}
    assert len(calls) == 1


def test_every_deciding_field_on_entry_decision_has_a_column(temp_db):
    """Spec 1e field parity. A new EntryDecision field must either get a column or be listed here as bookkeeping."""
    bookkeeping = {"kelly_fraction_raw", "kelly_fraction_applied", "available_depth_usd",
                   "slippage_at_size_pct", "token_id"}
    con = sqlite3.connect(temp_db)
    columns = {r[1] for r in con.execute("PRAGMA table_info(entry_decisions)")}
    con.close()
    for f in dataclasses.fields(EntryDecision):
        if f.name in bookkeeping:
            continue
        assert f.name in columns, f"EntryDecision.{f.name} has no entry_decisions column"
    assert set(storage.ENTRY_DECISION_COLUMNS) == columns


def test_record_and_load_round_trip(temp_db):
    d = EntryDecision(
        station_icao=STATION, target_date=TARGET, bucket_c=32, side="NO",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=0.0,
        available_depth_usd=None, slippage_at_size_pct=None, net_ev_at_size=None,
        approved=False, reason="model_prob below floor", station_maturity="mature",
        entry_price=0.60, entry_bid=0.58, model_prob=0.60, raw_edge=0.0, min_net_ev=0.15,
        rule_id="00c",
    )
    n = storage.record_entry_decisions([d], book="paper_shadow", cycle_ts="2026-09-17T21:00:00+00:00", config_sha=None)
    assert n == 1
    (row,) = storage.load_entry_decisions(book="paper_shadow")
    assert row["rule_id"] == "00c" and row["approved"] == 0 and row["side"] == "NO"
    assert row["calibration_source"] == "uncalibrated" and row["calibrated_prob"] is None
    assert storage.load_entry_decisions(book="live") == []
    assert storage.record_entry_decisions([], book="paper", cycle_ts="x", config_sha=None) == 0
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave1_entry_decisions_recorded.py -v` / Expected: FAIL with `AttributeError: module 'storage' has no attribute 'record_entry_decisions'`

- [x] **Step 3: Write minimal implementation**

storage.py, appended at the end of the file:

```python
# --------------------------------------------------------------------------
# Entry decisions (Wave 1)
# --------------------------------------------------------------------------

def record_entry_decisions(
    decisions,
    *,
    book: str,
    cycle_ts: str,
    config_sha: Optional[str],
) -> int:
    """
    Append one entry_decisions row per EntryDecision. Returns the row count.

    Column order is ENTRY_DECISION_COLUMNS, and the values are read straight
    off the decision -- copied, never recomputed, for the same reason
    Position.model_prob is. The caller (scheduler._record_entry_decisions)
    owns the try/except: this function raises on a storage error so the
    caller can log it, and the caller must never let it block a trade.
    """
    if not decisions:
        return 0
    rows = [
        (
            cycle_ts, d.station_icao, d.target_date.isoformat(), d.bucket_c, d.side, book,
            int(bool(d.approved)), d.rule_id, d.reason,
            d.entry_price, d.entry_bid, d.model_prob, d.calibrated_prob, d.calibration_source,
            d.raw_edge, d.admission_edge, d.sizing_edge, d.net_ev_at_size,
            d.kelly_size_preclamp_usd, d.recommended_size_usd, d.min_net_ev,
            d.station_maturity, config_sha,
        )
        for d in decisions
    ]
    placeholders = ", ".join("?" for _ in ENTRY_DECISION_COLUMNS)
    with _db() as conn:
        conn.executemany(
            f"INSERT INTO entry_decisions ({', '.join(ENTRY_DECISION_COLUMNS)}) "
            f"VALUES ({placeholders})",
            rows,
        )
    return len(rows)


def load_entry_decisions(
    book: Optional[str] = None,
    cycle_ts: Optional[str] = None,
    limit: int = 1000,
) -> List[dict]:
    """Rows as dicts keyed by ENTRY_DECISION_COLUMNS, newest cycle first. For tests and operator scripts."""
    query = f"SELECT {', '.join(ENTRY_DECISION_COLUMNS)} FROM entry_decisions"
    clauses, params = [], []
    if book is not None:
        clauses.append("book = ?")
        params.append(book)
    if cycle_ts is not None:
        clauses.append("cycle_ts = ?")
        params.append(cycle_ts)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY cycle_ts DESC, station_icao, bucket_c, side LIMIT ?"
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(zip(ENTRY_DECISION_COLUMNS, r)) for r in rows]
```

scheduler.py — after `_last_collection_ts: Dict[str, float] = {}` (line 96):

```python
# WAVE 1. The git sha stamped on every entry_decisions row, resolved once per
# process: config._current_git_sha() shells out to git, and a subprocess per
# station-cycle is not a cost the entry leg should carry. Keyed dict rather
# than a bare Optional so "not yet asked" and "asked, no git" stay distinct.
_config_sha_cache: Dict[str, Optional[str]] = {}


def _config_sha() -> Optional[str]:
    if "sha" not in _config_sha_cache:
        try:
            _config_sha_cache["sha"] = config._current_git_sha()
        except Exception:  # noqa: BLE001 -- provenance must never break a cycle
            _config_sha_cache["sha"] = None
    return _config_sha_cache["sha"]


def _record_entry_decisions(decisions, station_icao: str, book: str, cycle_ts: str) -> None:
    """
    Persist a cycle's EntryDecisions as entry_decisions rows. BEST-EFFORT:
    a storage failure here is printed and swallowed, because this runs
    between the decision and the executor and must never be the reason a
    trade did not happen (spec 1b).
    """
    try:
        n = storage.record_entry_decisions(
            decisions, book=book, cycle_ts=cycle_ts, config_sha=_config_sha(),
        )
        print(f"[scheduler] {station_icao}: recorded {n} entry decision(s) as book={book!r}.")
    except Exception as exc:  # noqa: BLE001 -- recording is not trading
        print(
            f"[scheduler] {station_icao}: could not record entry decisions "
            f"(book={book!r}): {exc} -- continuing, the trade path is unaffected."
        )
```

scheduler.py `_run_full_cycle` — old:
```python
    try:
        estimate = result["estimate"]
        # ONE discovery per station-cycle. The EV table, the bucket bounds
```
new:
```python
    # WAVE 1: one timestamp per cycle, shared by every row this cycle records
    # (and by the paper shadow rows, so they pair on it); one book label per
    # station, which is its executor mode.
    cycle_ts = datetime.now(timezone.utc).isoformat()
    book = executor.EXECUTION_MODE.get(station_icao, "manual_review")

    try:
        estimate = result["estimate"]
        # ONE discovery per station-cycle. The EV table, the bucket bounds
```
old:
```python
                entry_manager.print_entry_decisions(entry_decisions)
                for decision in entry_decisions:
                    executor.open_position(decision)
```
new:
```python
                entry_manager.print_entry_decisions(entry_decisions)
                # WAVE 1: recorded BEFORE any executor call, best-effort.
                _record_entry_decisions(entry_decisions, station_icao, book, cycle_ts)
                for decision in entry_decisions:
                    executor.open_position(decision)
```

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave1_entry_decisions_recorded.py -v` / Expected: PASS (6 passed)
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1711
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add storage.py scheduler.py tests/test_wave1_entry_decisions_recorded.py
git commit -m "Wave 1: record every entry decision, best-effort, before the executor runs

storage.record_entry_decisions writes one entry_decisions row per
EntryDecision in ENTRY_DECISION_COLUMNS order; scheduler._run_full_cycle
calls it right after decide_portfolio_entries, wrapped so a storage
failure prints and continues. Decisions are proven identical with
recording on and off; every deciding field has a column by name.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Executor refusals become `live_order_attempts` rows
**Files:** Modify `executor.py` (`_live_budget_breach` 140-246; `_resolved_size_ok` 261-432; new `_record_refusal` after `_record_attempt` ~555; `open_position` 820-822; `_open_via_order_path` 949-983), `storage.py` (`count_live_order_attempts` 1283-1322) / Test `tests/test_wave1_refusal_rows.py`
**Interfaces:** Consumes: Task 4's `out` dict / Produces: `executor._record_refusal(decision, code: str, message: str, *, mode: str, spec=None) -> None`; `out["refusal_code"]` from `_resolved_size_ok` and `_live_budget_breach(size_usd, station_icao, out=None)`; refusal rows have `kind='entry'`, `outcome='refused'`, `detail='<code>: <message>'`; `count_live_order_attempts` ignores `outcome='refused'`

Refusal sites and codes (executor.py at HEAD):

| line | site | code |
|---|---|---|
| 822 | approved but no entry_price | `no_entry_price` |
| 951 | no token_id | `no_token_id` |
| 965 | `spec.ok` False | `order_not_placeable` |
| 970 | `_price_drift_ok` | `drift` |
| 975 | `_resolved_size_ok`: depth re-read raised / None | `resolved_depth_unreadable` |
| 975 | depth ceiling | `resolved_depth` |
| 975 | slippage re-estimate raised | `resolved_slippage_unreadable` |
| 975 | slippage gate | `resolved_slippage` |
| 975 | net-EV under the bar | `resolved_net_ev` |
| 975 | `_day_budget_breach` (station/day or portfolio/day) | `day_budget` |
| 983 | `_live_budget_breach`: reconciliation | `recon` |
| 983 | region concurrent cap | `region_concurrent` |
| 983 | region exposure cap | `region_exposure` |
| 983 | order count unreadable | `orders_per_day_unreadable` |
| 983 | orders-per-day cap | `orders_per_day` |

Line 1040 (unfilled order) is already recorded by `_record_attempt` as `killed`/`not_submitted` and is not a refusal. Rows are written only when the station's mode is `live` — the table is the live audit trail and `_record_attempt` has the same scope.

- [x] **Step 1: Write the failing test**

```python
# tests/test_wave1_refusal_rows.py
"""
Wave 1 (spec 1c): every executor-level refusal that used to be only a print
is one live_order_attempts row with outcome='refused' and detail
'<code>: <message>'. Journal lines are unchanged. The daily order cap
counts SUBMISSIONS and must not count these.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from clients import market_client, wallet_client
from models import EntryDecision, Position

REGION = config.region_of("WSSS")


def _decision(price=0.30, token_id="TOK", net_ev=0.30, slip=0.01):
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 8, 10), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=1.0, available_depth_usd=1000.0,
        slippage_at_size_pct=slip, net_ev_at_size=net_ev,
        approved=True, reason="test", station_maturity="mature",
        entry_price=price, token_id=token_id,
    )


def _live_position(size_usd=1.0):
    return Position(
        position_id="p", station_icao="WSSS", target_date=date(2026, 8, 10),
        bucket_c=33, side="NO", entry_price=0.30, size_usd=size_usd,
        entry_time="2026-08-10T00:00:00+00:00", status="open", token_id="T2",
        is_paper=False, size_shares=3.33, execution_mode="live",
    )


def _explode(*a, **kw):
    raise AssertionError("submit_order must not be reached by a refused entry")


def _no_position(p):
    raise AssertionError("a refused entry must not write a position")


def _wsss_only(positions):
    """
    load_open_positions stub that hands `positions` to the region backstop
    (no station filter) and to WSSS's own day-budget read, and nothing to any
    other station -- portfolio_day_exposure_usd sums every station in the
    region, and an unfiltered stub would multiply the fixture 15x.
    """
    def _load(**kw):
        return list(positions) if kw.get("station_icao") in (None, "WSSS") else []
    return _load


@pytest.fixture
def live(monkeypatch):
    rows = []
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    monkeypatch.setattr(storage, "open_position", _no_position)
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
    monkeypatch.setattr(storage, "record_live_order_attempt", lambda **kw: rows.append(kw))
    monkeypatch.setattr(storage, "count_live_order_attempts", lambda kind, since, station_icaos=None: 0)
    monkeypatch.setattr(wallet_client, "_book_constraints", lambda token_id: ("0.01", None))
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions, **_: wallet_client.Reconciliation(ok=True, checked=True, reason="stubbed"),
    )
    monkeypatch.setattr(wallet_client, "submit_order", _explode)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    return rows


def _setup_no_entry_price(mp):
    return _decision(price=None)


def _setup_no_token_id(mp):
    return _decision(token_id="")


def _setup_order_not_placeable(mp):
    mp.setattr(wallet_client, "build_entry_order", lambda **kw: wallet_client.OrderSpec(
        ok=False, token_id="TOK", side="BUY", limit_price=0.0, size_shares=0.0,
        notional_usd=0.0, reason="stubbed refusal"))
    return _decision()


def _setup_drift(mp):
    mp.setattr(wallet_client, "_book_constraints", lambda token_id: ("0.1", None))
    return _decision(price=0.31)


def _setup_resolved_depth_unreadable(mp):
    mp.setattr(market_client, "get_available_depth_usd", lambda t: None)
    return _decision()


def _setup_resolved_depth(mp):
    mp.setattr(market_client, "get_available_depth_usd", lambda t: 2.0)   # 25% of 2 = $0.50 < $1
    return _decision()


def _setup_resolved_slippage_unreadable(mp):
    def _raise(t, s):
        raise RuntimeError("book down")
    mp.setattr(market_client, "estimate_slippage", _raise)
    return _decision()


def _setup_resolved_slippage(mp):
    mp.setattr(market_client, "estimate_slippage", lambda t, s: config.MAX_ACCEPTABLE_SLIPPAGE_PCT + 0.01)
    return _decision()


def _setup_resolved_net_ev(mp):
    mp.setattr(market_client, "estimate_slippage", lambda t, s: 0.05)
    return _decision(net_ev=0.02, slip=0.01)


def _setup_day_budget(mp):
    mp.setattr(entry_manager, "station_day_exposure_usd",
               lambda *a, **kw: config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD)
    return _decision()


def _setup_recon(mp):
    mp.setattr(wallet_client, "reconcile_cached",
               lambda positions, **_: wallet_client.Reconciliation(ok=False, checked=True, reason="db_only"))
    return _decision()


def _setup_region_concurrent(mp):
    n = config.REGION_LIVE_MAX_CONCURRENT_POSITIONS[REGION]   # 5 x $1 stays under the $8 exposure cap
    mp.setattr(storage, "load_open_positions", _wsss_only([_live_position() for _ in range(n)]))
    return _decision()


def _setup_region_exposure(mp):
    cap = config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD[REGION]   # one $8 position + $1 breaches; count 1 < 5
    mp.setattr(storage, "load_open_positions", _wsss_only([_live_position(size_usd=cap)]))
    return _decision()


def _setup_orders_per_day_unreadable(mp):
    mp.setattr(storage, "count_live_order_attempts", lambda kind, since, station_icaos=None: None)
    return _decision()


def _setup_orders_per_day(mp):
    mp.setattr(storage, "count_live_order_attempts",
               lambda kind, since, station_icaos=None: config.REGION_LIVE_MAX_ORDERS_PER_DAY[REGION])
    return _decision()


SITES = [
    ("no_entry_price", _setup_no_entry_price),
    ("no_token_id", _setup_no_token_id),
    ("order_not_placeable", _setup_order_not_placeable),
    ("drift", _setup_drift),
    ("resolved_depth_unreadable", _setup_resolved_depth_unreadable),
    ("resolved_depth", _setup_resolved_depth),
    ("resolved_slippage_unreadable", _setup_resolved_slippage_unreadable),
    ("resolved_slippage", _setup_resolved_slippage),
    ("resolved_net_ev", _setup_resolved_net_ev),
    ("day_budget", _setup_day_budget),
    ("recon", _setup_recon),
    ("region_concurrent", _setup_region_concurrent),
    ("region_exposure", _setup_region_exposure),
    ("orders_per_day_unreadable", _setup_orders_per_day_unreadable),
    ("orders_per_day", _setup_orders_per_day),
]


@pytest.mark.parametrize("code,setup", SITES, ids=[s[0] for s in SITES])
def test_each_refusal_site_records_exactly_one_refused_row(live, monkeypatch, code, setup):
    decision = setup(monkeypatch)

    executor.open_position(decision)

    assert len(live) == 1, f"{code}: expected one row, got {live}"
    row = live[0]
    assert row["kind"] == "entry" and row["outcome"] == "refused"
    assert row["station_icao"] == "WSSS" and row["bucket_c"] == 32 and row["side"] == "YES"
    assert row["detail"].startswith(f"{code}: "), row["detail"]
    assert row["order_id"] is None


def test_an_unapproved_decision_records_nothing(live):
    d = _decision()
    d.approved = False
    executor.open_position(d)
    assert live == []


def test_paper_refusals_are_not_written_to_the_live_audit(live, monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    executor.open_position(_decision(price=None))
    assert live == []


def test_a_recording_failure_never_raises(live, monkeypatch, capsys):
    def _boom(**kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(storage, "record_live_order_attempt", _boom)
    executor.open_position(_decision(price=None))
    assert "could not record the refusal" in capsys.readouterr().out


def test_refused_rows_do_not_count_toward_the_daily_order_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    storage._connect().close()
    storage.record_live_order_attempt(kind="entry", station_icao="WSSS", outcome="filled", detail="ok")
    storage.record_live_order_attempt(kind="entry", station_icao="WSSS", outcome="killed", detail="fok")
    storage.record_live_order_attempt(kind="entry", station_icao="WSSS", outcome="refused", detail="drift: x")

    assert storage.count_live_order_attempts("entry", "2000-01-01") == 2
    assert storage.count_live_order_attempts("entry", "2000-01-01", station_icaos=["WSSS"]) == 2
    assert len(storage.load_live_order_attempts()) == 3
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave1_refusal_rows.py -v` / Expected: FAIL with `assert 0 == 1` for every parametrised site, `AttributeError` for `could not record the refusal`, and `assert 3 == 2` on the cap test

- [x] **Step 3: Write minimal implementation**

storage.py `count_live_order_attempts` — the two queries, old:
```python
                    "SELECT COUNT(*) FROM live_order_attempts WHERE kind = ? AND ts >= ?",
```
new:
```python
                    "SELECT COUNT(*) FROM live_order_attempts "
                    "WHERE kind = ? AND ts >= ? AND outcome != 'refused'",
```
and old:
```python
                    f"SELECT COUNT(*) FROM live_order_attempts "
                    f"WHERE kind = ? AND ts >= ? AND station_icao IN ({placeholders})",
```
new:
```python
                    f"SELECT COUNT(*) FROM live_order_attempts "
                    f"WHERE kind = ? AND ts >= ? AND outcome != 'refused' "
                    f"AND station_icao IN ({placeholders})",
```
and add to that function's docstring:
```python
    `outcome='refused'` rows (WAVE 1: executor refusals that never built or
    never submitted an order) are EXCLUDED. This cap counts submissions, and
    a refusal is the one outcome that is not one -- counting them would let
    a morning of refusals exhaust the real order budget, which would be a
    trading change wearing a recording change's clothes.
```

executor.py `_live_budget_breach` — old:
```python
def _live_budget_breach(size_usd: float, station_icao: str) -> Optional[str]:
```
new:
```python
def _live_budget_breach(size_usd: float, station_icao: str, out: Optional[dict] = None) -> Optional[str]:
```
add after the docstring's first line a sentence: `\`out\`, when given, receives "refusal_code" naming which backstop refused (WAVE 1).` Then add a nested helper right after the docstring:
```python
    def _refuse(code: str, message: str) -> str:
        if out is not None:
            out["refusal_code"] = code
        return message

```
and wrap each of the five `return (f"...")` refusal strings: `return _refuse("recon", (...))`, `_refuse("region_concurrent", ...)`, `_refuse("region_exposure", ...)`, `_refuse("orders_per_day_unreadable", ...)`, `_refuse("orders_per_day", ...)`. Message text unchanged. The final `return None` stays.

executor.py `_resolved_size_ok` — add, right after its docstring:
```python
    def _refuse(code: str, message: str) -> tuple:
        if out is not None:
            out["refusal_code"] = code
        return False, message

```
and change each `return False, ...` to `return _refuse(code, ...)` with: depth re-read exception → `"resolved_depth_unreadable"`; `depth is None` → `"resolved_depth_unreadable"`; ceiling → `"resolved_depth"`; slippage exception → `"resolved_slippage_unreadable"`; slippage gate → `"resolved_slippage"`; `too_low` → `"resolved_net_ev"`; budget → `return _refuse("day_budget", budget_breach)`. Message text unchanged.

executor.py — new function after `_record_attempt` (before `_price_drift_ok`):
```python
def _record_refusal(decision, code: str, message: str, *, mode: str, spec=None) -> None:
    """
    WAVE 1: an executor refusal becomes one live_order_attempts row,
    outcome='refused', detail='<code>: <message>' -- so a live entry that
    was approved and then refused leaves a row and not only a print. Only
    in live mode: this table is the live audit trail (see _record_attempt).
    The daily order cap excludes these rows (storage.count_live_order_
    attempts). Journal lines are untouched. Never raises.
    """
    if mode != "live":
        return
    built = spec is not None and getattr(spec, "ok", False)
    try:
        storage.record_live_order_attempt(
            kind="entry", station_icao=decision.station_icao, outcome="refused",
            target_date=decision.target_date, bucket_c=decision.bucket_c, side=decision.side,
            notional_usd=spec.notional_usd if built else decision.recommended_size_usd,
            size_shares=spec.size_shares if built else None,
            limit_price=spec.limit_price if built else decision.entry_price,
            order_id=None,
            detail=f"{code}: {message}",
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[executor] WARNING: could not record the refusal ({code}) for "
            f"{decision.station_icao} {decision.bucket_c}°{decision.side}: {exc}."
        )
```

executor.py `open_position` — old:
```python
    if decision.entry_price is None:
        print(f"[executor] {decision.station_icao} {decision.bucket_c}°{decision.side}: approved but no entry_price recorded -- refusing to open blind.")
        return
```
new:
```python
    if decision.entry_price is None:
        print(f"[executor] {decision.station_icao} {decision.bucket_c}°{decision.side}: approved but no entry_price recorded -- refusing to open blind.")
        _record_refusal(decision, "no_entry_price", "approved but no entry_price recorded -- refusing to open blind",
                        mode=EXECUTION_MODE.get(decision.station_icao, "manual_review"))
        return
```

executor.py `_open_via_order_path` — the five sites, old → new:
```python
    if not decision.token_id:
        print(f"[executor] {tag}: {label} has no token_id -- cannot build an order, skipping.")
        _record_refusal(decision, "no_token_id", "no token_id -- cannot build an order", mode=mode)
        return
```
```python
    if not spec.ok:
        print(f"[executor] {tag}: {label} order NOT placeable -- {spec.reason}")
        _record_refusal(decision, "order_not_placeable", spec.reason, mode=mode, spec=spec)
        return
```
```python
    drift_ok, drift_note = _price_drift_ok(spec.limit_price, decision.entry_price)
    if not drift_ok:
        print(f"[executor] {tag}: {label} order abandoned -- {drift_note}")
        _record_refusal(decision, "drift", drift_note, mode=mode, spec=spec)
        return
```
```python
    resolved = {}
    size_ok, size_note = _resolved_size_ok(spec, decision, out=resolved)
    if not size_ok:
        print(f"[executor] {tag}: {label} order abandoned -- {size_note}")
        _record_refusal(decision, resolved.get("refusal_code", "resolved_size"), size_note, mode=mode, spec=spec)
        return
```
```python
    if mode == "live":
        backstop = {}
        breach = _live_budget_breach(spec.notional_usd, decision.station_icao, out=backstop)
        if breach:
            print(f"[executor] LIVE: {label} entry BLOCKED by a risk backstop -- {breach}")
            _record_refusal(decision, backstop.get("refusal_code", "live_backstop"), breach, mode=mode, spec=spec)
            return
```

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave1_refusal_rows.py tests/test_live_execution.py -v` / Expected: PASS (20 passed in the new file)
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1731
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add executor.py storage.py tests/test_wave1_refusal_rows.py
git commit -m "Wave 1: executor refusals become live_order_attempts rows

Every executor-level refusal on the live path -- no entry price, no
token, unplaceable order, drift, the six resolved-size re-checks, the
day budget and the five region backstops -- records one row with
outcome='refused' and detail='<code>: <message>' via the existing
record_live_order_attempt. Journal lines are unchanged. The daily order
cap now excludes refused rows so it still counts submissions only.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Explicit `execution_mode` override through the entry path; `ev_engine.reprice_for_mode`
**Files:** Modify `entry_manager.py` (`_execution_mode` 123, `_candidate_is_paper` 128, `_book_has_stop` 145, `live_size_cap_usd` 158, `evaluate_entry` signature 828 and lines 982/1143/1164, `decide_entries` 1313, `decide_portfolio_entries` 1543/1571), `ev_engine.py` (new function after `compute_ev_table`, before `book_dislocation` at 393) / Test `tests/test_wave1_mode_override.py`
**Interfaces:** Consumes: Task 3 / Produces: `entry_manager.evaluate_entry(ev_result, token_id, min_net_ev=0.15, execution_mode: Optional[str] = None)`, `entry_manager.decide_entries(ev_results, token_map, min_net_ev=0.15, execution_mode=None)`, `entry_manager.decide_portfolio_entries(ev_results, token_map, min_net_ev=0.15, forecast_sources=None, execution_mode=None)`, the three helpers with `execution_mode: Optional[str] = None`; `ev_engine.reprice_for_mode(results: List[EVResult], execution_mode: Optional[str]) -> List[EVResult]`

- [x] **Step 1: Write the failing test**

```python
# tests/test_wave1_mode_override.py
"""
Wave 1 (spec 1d): the paper shadow pass needs the entry path evaluated AS
PAPER for a station whose executor.EXECUTION_MODE is live -- through an
explicit parameter, never by writing that dict. And it needs the paper EV
table for the same cycle without re-fetching the book:
ev_engine.reprice_for_mode must equal compute_ev_table run in that mode.
"""
import dataclasses
from datetime import date

import pytest

import config
import entry_manager
import ev_engine
import executor
import storage
from clients import market_client
from models import CalibratedEstimate, EVResult, MarketQuote


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})


def _ev():
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 9, 17), bucket_c=32, side="YES",
        model_prob=0.55, market_price=0.35, raw_edge=0.20,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02, net_ev_per_dollar=0.54,
        spread_source="ensemble", market_bid=0.33,
    )


def test_helpers_default_to_the_module_dict_and_accept_an_override():
    assert entry_manager._execution_mode("WSSS") == "live"
    assert entry_manager._execution_mode("WSSS", "paper") == "paper"
    assert entry_manager._candidate_is_paper("WSSS") is False
    assert entry_manager._candidate_is_paper("WSSS", "paper") is True
    assert entry_manager._book_has_stop("WSSS") is True
    assert entry_manager._book_has_stop("WSSS", "paper") is False
    assert entry_manager.live_size_cap_usd("WSSS") == config.LIVE_TRADE_SIZE_USD
    assert entry_manager.live_size_cap_usd("WSSS", "paper") is None


def test_override_sizes_as_paper_and_never_touches_the_dict(monkeypatch):
    reads = []
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: reads.append(kw) or [])

    as_paper = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15, execution_mode="paper")
    assert executor.EXECUTION_MODE == {"WSSS": "live"}
    as_live = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)

    assert as_live.recommended_size_usd == pytest.approx(config.LIVE_TRADE_SIZE_USD)
    assert as_paper.recommended_size_usd > config.LIVE_TRADE_SIZE_USD
    assert as_paper.recommended_size_usd == pytest.approx(as_paper.kelly_size_preclamp_usd)
    # The paper override reads the PAPER book for the cap/cooldown counts.
    paper_reads = [kw["is_paper"] for kw in reads[:2]]
    assert paper_reads == [True, True]


def test_override_equals_actually_being_in_that_mode(monkeypatch):
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    via_override = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15, execution_mode="paper")
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    via_dict = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)
    assert dataclasses.asdict(via_override) == dataclasses.asdict(via_dict)


def test_decide_portfolio_entries_threads_the_override(monkeypatch):
    reads = []
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: reads.append(kw) or [])
    monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.1, 20, 0.1))
    monkeypatch.setattr(entry_manager, "resolution_obs_count", lambda icao: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY)
    monkeypatch.setattr(entry_manager, "forecast_bias_source_mix", lambda icao: None)
    monkeypatch.setattr(entry_manager, "station_error_width_ratio", lambda icao: None)
    token_map = {32: {"yes_token_id": "y", "no_token_id": "n"}}

    decisions = entry_manager.decide_portfolio_entries([_ev()], token_map, min_net_ev=0.15, execution_mode="paper")

    assert decisions[0].approved and decisions[0].recommended_size_usd > config.LIVE_TRADE_SIZE_USD
    assert all(kw.get("is_paper") is True for kw in reads)
    assert executor.EXECUTION_MODE == {"WSSS": "live"}


# --- ev_engine.reprice_for_mode ------------------------------------------

def _estimate():
    return CalibratedEstimate(station_icao="WSSS", target_date=date(2026, 9, 17),
                              central_estimate_c=32.0, std_dev_c=1.0, monsoon_phase="southwest",
                              spread_source="ensemble")


def _token_map():
    return {b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in (31, 32, 33)}


def _quotes():
    return {
        31: MarketQuote(bucket_c=31, yes_price=0.20, no_price=0.81, yes_bid=0.19, no_bid=0.80),
        32: MarketQuote(bucket_c=32, yes_price=0.35, no_price=0.66, yes_bid=0.33, no_bid=0.64),
        33: MarketQuote(bucket_c=33, yes_price=None, no_price=None),   # unpriced bucket
    }


def _probs():
    return {31: 0.25, 32: 0.55, 33: 0.20}


def test_reprice_for_mode_equals_compute_ev_table_in_that_mode():
    live = ev_engine.compute_ev_table(_estimate(), _token_map(), quotes=_quotes(),
                                      model_probs=_probs(), execution_mode="live")
    paper = ev_engine.compute_ev_table(_estimate(), _token_map(), quotes=_quotes(),
                                       model_probs=_probs(), execution_mode="paper")

    repriced = ev_engine.reprice_for_mode(live, "paper")

    assert [dataclasses.asdict(r) for r in repriced] == [dataclasses.asdict(r) for r in paper]
    # And it really changed something: live pays an exit fee, paper does not.
    priced = [r for r in live if r.market_price is not None]
    assert all(r.expected_exit_fee_pct > 0 for r in priced)
    assert all(r.expected_exit_fee_pct == 0.0 for r in repriced if r.market_price is not None)


def test_reprice_is_pure_and_leaves_the_input_alone():
    live = ev_engine.compute_ev_table(_estimate(), _token_map(), quotes=_quotes(),
                                      model_probs=_probs(), execution_mode="live")
    before = [dataclasses.asdict(r) for r in live]
    ev_engine.reprice_for_mode(live, "paper")
    assert [dataclasses.asdict(r) for r in live] == before


def test_reprice_round_trips():
    live = ev_engine.compute_ev_table(_estimate(), _token_map(), quotes=_quotes(),
                                      model_probs=_probs(), execution_mode="live")
    back = ev_engine.reprice_for_mode(ev_engine.reprice_for_mode(live, "paper"), "live")
    assert [dataclasses.asdict(r) for r in back] == [dataclasses.asdict(r) for r in live]
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave1_mode_override.py -v` / Expected: FAIL with `TypeError: _execution_mode() takes 1 positional argument but 2 were given` and `AttributeError: module 'ev_engine' has no attribute 'reprice_for_mode'`

- [x] **Step 3: Write minimal implementation**

entry_manager.py — old (123-164):
```python
def _execution_mode(station_icao: str) -> str:
    """This station's executor mode, defaulted the same way executor does."""
    return executor.EXECUTION_MODE.get(station_icao, "manual_review")


def _candidate_is_paper(station_icao: str) -> bool:
```
new:
```python
def _execution_mode(station_icao: str, execution_mode: Optional[str] = None) -> str:
    """
    This station's executor mode, defaulted the same way executor does.

    `execution_mode`, when given, OVERRIDES the module-dict lookup for this
    one evaluation (WAVE 1: the paper shadow pass evaluates a live station
    as paper). It is threaded as a parameter and never written into
    executor.EXECUTION_MODE, because a mutate-and-restore on the dict the
    live order path reads would be a race with that path.
    """
    if execution_mode is not None:
        return execution_mode
    return executor.EXECUTION_MODE.get(station_icao, "manual_review")


def _candidate_is_paper(station_icao: str, execution_mode: Optional[str] = None) -> bool:
```
and its body `return _execution_mode(station_icao) != "live"` → `return _execution_mode(station_icao, execution_mode) != "live"`;
`def _book_has_stop(station_icao: str, execution_mode: Optional[str] = None) -> bool:` with body `return _execution_mode(station_icao, execution_mode) not in config.HOLD_TO_SETTLEMENT_MODES`;
`def live_size_cap_usd(station_icao: str, execution_mode: Optional[str] = None) -> Optional[float]:` with body `return config.live_size_cap_usd(station_icao, _execution_mode(station_icao, execution_mode))`.

`evaluate_entry` — old:
```python
def evaluate_entry(
    ev_result: EVResult,
    token_id: str,
    min_net_ev: float = 0.15,
) -> EntryDecision:
```
new:
```python
def evaluate_entry(
    ev_result: EVResult,
    token_id: str,
    min_net_ev: float = 0.15,
    execution_mode: Optional[str] = None,
) -> EntryDecision:
```
and inside: `candidate_is_paper = _candidate_is_paper(station_icao)` → `candidate_is_paper = _candidate_is_paper(station_icao, execution_mode)`; `_has_stop = _book_has_stop(station_icao)` → `_has_stop = _book_has_stop(station_icao, execution_mode)`; `live_cap = live_size_cap_usd(station_icao)` → `live_cap = live_size_cap_usd(station_icao, execution_mode)`.

`decide_entries` — old:
```python
def decide_entries(
    ev_results: List[EVResult],
    token_map: dict,
    min_net_ev: float = 0.15,
) -> List[EntryDecision]:
```
new:
```python
def decide_entries(
    ev_results: List[EVResult],
    token_map: dict,
    min_net_ev: float = 0.15,
    execution_mode: Optional[str] = None,
) -> List[EntryDecision]:
```
and its body: `evaluate_entry(result, token_id, min_net_ev=min_net_ev, execution_mode=execution_mode)`.

`decide_portfolio_entries` — old:
```python
    min_net_ev: float = 0.15,
    forecast_sources: Optional[list] = None,
) -> List[EntryDecision]:
```
new:
```python
    min_net_ev: float = 0.15,
    forecast_sources: Optional[list] = None,
    execution_mode: Optional[str] = None,
) -> List[EntryDecision]:
```
and inside: `candidate_is_paper = _candidate_is_paper(station_icao)` → `candidate_is_paper = _candidate_is_paper(station_icao, execution_mode)`; `decisions = decide_entries(ev_results, token_map, min_net_ev=min_net_ev)` → `decisions = decide_entries(ev_results, token_map, min_net_ev=min_net_ev, execution_mode=execution_mode)`. Add to the docstring: `execution_mode overrides executor.EXECUTION_MODE for this evaluation only (see _execution_mode); None keeps today's lookup.`

ev_engine.py — add `import dataclasses` after `import time` (line 68), and this function after `compute_ev_table` (before `def book_dislocation`):

```python
def reprice_for_mode(results: List[EVResult], execution_mode: Optional[str]) -> List[EVResult]:
    """
    The same EV table as it would have been computed under `execution_mode`,
    WITHOUT re-reading the book. Only two fields depend on the mode in
    compute_ev_table -- expected_exit_fee_pct (charged only on a book that
    sells before settlement) and net_ev_per_dollar, which subtracts it --
    so those two are recomputed from the row's own price, edge, slippage and
    entry fee, and every other field is copied. Unpriced rows are returned
    as they are.

    WAVE 1: the paper shadow pass prices a live station's cycle as paper
    from the primary pass's own results, so both books see the identical
    quotes and slippage. Re-running run_for_station_with_map would
    re-discover the market and write a second set of price snapshots.

    Valid for tables built with fee_rate_pct=None (the production default):
    a flat fee override suppresses the exit fee in compute_ev_table, and a
    row cannot say whether it was built under one. tests/test_wave1_mode_
    override.py pins this against compute_ev_table field for field.
    """
    sells = _sells_before_settlement(execution_mode)
    out = []
    for r in results:
        if r.market_price is None or r.market_price <= 0 or r.raw_edge is None:
            out.append(r)
            continue
        exit_fee_pct = expected_exit_fee_pct_of_notional(r.market_price) if sells else 0.0
        net_ev = (r.raw_edge / r.market_price) - r.estimated_slippage_pct - r.fee_rate_pct - exit_fee_pct
        out.append(dataclasses.replace(r, expected_exit_fee_pct=exit_fee_pct, net_ev_per_dollar=net_ev))
    return out
```

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave1_mode_override.py tests/test_parity_entry.py tests/test_haircut_on_a_stopless_book.py tests/test_live_execution.py -v` / Expected: PASS
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1738
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add entry_manager.py ev_engine.py tests/test_wave1_mode_override.py
git commit -m "Wave 1: explicit execution_mode override through the entry path; reprice_for_mode

entry_manager's mode helpers, evaluate_entry, decide_entries and
decide_portfolio_entries accept execution_mode=None, defaulting to the
executor.EXECUTION_MODE lookup they always did; a value overrides it for
one evaluation and never writes the dict. ev_engine.reprice_for_mode
rebuilds an EV table for another mode from its own rows (exit fee and
net EV only), pinned equal to compute_ev_table field for field. No
existing caller changes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: The paper shadow twin for live stations
**Files:** Modify `scheduler.py` (`_run_full_cycle` try/except block; new `_run_shadow_pass` after `_record_entry_decisions`) / Test `tests/test_wave1_shadow_pass.py`
**Interfaces:** Consumes: `_record_entry_decisions` (Task 5), `decide_portfolio_entries(..., execution_mode="paper")` and `ev_engine.reprice_for_mode` (Task 7) / Produces: `scheduler._run_shadow_pass(station_icao: str, min_net_ev: float, cycle_ts: str, ev_run: ev_engine.StationEVRun, forecast_sources: Optional[list]) -> list`

The signature carries the primary pass's `ev_run` (its `ev_results` and `token_map`) rather than re-discovering: the shadow is "the same cycle" only if it prices the same book, and `run_for_station_with_map` would write a second set of price snapshots.

- [x] **Step 1: Write the failing test**

```python
# tests/test_wave1_shadow_pass.py
"""
Wave 1 (spec 1d): for a station whose executor.EXECUTION_MODE is live,
_run_full_cycle runs a second, read-only evaluation of the same cycle as
paper and records it as book='paper_shadow'. It never opens a position --
asserted structurally over its call graph and behaviourally on a database.
"""
import ast
import inspect
import sqlite3
import sys
import textwrap
from datetime import date
from types import SimpleNamespace

import pytest

import config
import entry_manager
import ev_engine
import executor
import scheduler
import storage
from clients import market_client
from models import EVResult

STATION = "WSSS"
TARGET = date(2026, 9, 17)

# --------------------------------------------------------------------------
# Structural guard: the shadow pass cannot reach a position writer
# --------------------------------------------------------------------------

FORBIDDEN = {
    ("executor", "open_position"), ("executor", "_open_via_order_path"),
    ("storage", "open_position"),
}
WALKED_MODULES = ("scheduler", "entry_manager", "ev_engine", "storage")


def _reachable(fn, seen=None):
    """Transitively walk fn's calls into scheduler/entry_manager/ev_engine/storage, failing on a forbidden call."""
    seen = set() if seen is None else seen
    key = (fn.__module__, fn.__qualname__)
    if key in seen:
        return seen
    seen.add(key)
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            pair = (f.value.id, f.attr)
            assert pair not in FORBIDDEN, f"{fn.__module__}.{fn.__qualname__} reaches {pair[0]}.{pair[1]}"
            module = sys.modules.get(f.value.id)
            target = getattr(module, f.attr, None) if module is not None else None
        elif isinstance(f, ast.Name):
            target = fn.__globals__.get(f.id)
        else:
            continue
        if inspect.isfunction(target) and target.__module__ in WALKED_MODULES:
            _reachable(target, seen)
    return seen


def test_shadow_pass_call_graph_never_reaches_a_position_writer():
    seen = _reachable(scheduler._run_shadow_pass)
    # The walk went through the real entry path, not around it.
    assert ("entry_manager", "decide_portfolio_entries") in seen
    assert ("entry_manager", "evaluate_entry") in seen
    assert ("storage", "record_entry_decisions") in seen


def test_the_guard_can_see_a_forbidden_call():
    """Negative control: the primary cycle DOES call executor.open_position, and the walker must say so."""
    with pytest.raises(AssertionError, match="executor.open_position"):
        _reachable(scheduler._run_full_cycle)


# --------------------------------------------------------------------------
# Behaviour on a database
# --------------------------------------------------------------------------

def _ev(bucket=32, model_prob=0.55, price=0.35):
    return EVResult(
        station_icao=STATION, target_date=TARGET, bucket_c=bucket, side="YES",
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02,
        net_ev_per_dollar=(model_prob - price) / price - 0.03 - ev_engine.expected_exit_fee_pct_of_notional(price),
        expected_exit_fee_pct=ev_engine.expected_exit_fee_pct_of_notional(price),
        spread_source="ensemble", market_bid=price - 0.02,
    )


@pytest.fixture
def live_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    storage._connect().close()
    opened = []
    monkeypatch.setattr(executor, "EXECUTION_MODE", {icao: ("live" if icao == STATION else "paper") for icao in config.STATIONS})
    monkeypatch.setattr(executor, "open_position", lambda d: opened.append(d))
    monkeypatch.setattr(scheduler.pipeline, "run",
                        lambda station_icao, forecast_bias_c=0.0: {"estimate": SimpleNamespace(inputs_used=["open_meteo_ecmwf"])})
    monkeypatch.setattr(scheduler.pipeline, "print_summary", lambda r: None)
    monkeypatch.setattr(scheduler, "_run_exit_check", lambda *a, **kw: None)
    monkeypatch.setattr(ev_engine, "save_ev_snapshot", lambda icao, results: None)
    token_map = {b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in (31, 32, 33)}
    ev_run = ev_engine.StationEVRun(
        station_icao=STATION, target_date=TARGET, token_map=token_map,
        bucket_min_c=31, bucket_max_c=33, ev_results=[_ev(32), _ev(33, model_prob=0.95)],  # 33: veto 0a
    )
    monkeypatch.setattr(ev_engine, "run_for_station_with_map", lambda estimate, **kw: ev_run)
    monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.1, 20, 0.1))
    monkeypatch.setattr(entry_manager, "resolution_obs_count", lambda icao: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY)
    monkeypatch.setattr(entry_manager, "forecast_bias_source_mix", lambda icao: None)
    monkeypatch.setattr(entry_manager, "station_error_width_ratio", lambda icao: None)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    return opened


def test_a_live_station_records_paper_shadow_rows_and_no_positions(live_cycle):
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    live_rows = storage.load_entry_decisions(book="live")
    shadow_rows = storage.load_entry_decisions(book="paper_shadow")
    assert len(live_rows) == 2 and len(shadow_rows) == 2
    assert {r["cycle_ts"] for r in live_rows} == {r["cycle_ts"] for r in shadow_rows}
    # Same verdicts, different sizing: the live row is the $1 clamp, the shadow is paper Kelly.
    live32 = next(r for r in live_rows if r["bucket_c"] == 32)
    shadow32 = next(r for r in shadow_rows if r["bucket_c"] == 32)
    assert live32["approved"] == 1 and shadow32["approved"] == 1
    assert live32["recommended_size_usd"] == pytest.approx(config.LIVE_TRADE_SIZE_USD)
    assert shadow32["recommended_size_usd"] > config.LIVE_TRADE_SIZE_USD
    assert shadow32["recommended_size_usd"] == pytest.approx(live32["kelly_size_preclamp_usd"])
    # Nothing was opened by the shadow: the executor saw only the primary's decisions.
    assert len(live_cycle) == 2
    assert storage.load_open_positions() == []
    con = sqlite3.connect(config.DB_PATH)
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    con.close()


def test_the_dict_is_untouched_after_the_shadow(live_cycle):
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)
    assert executor.EXECUTION_MODE[STATION] == "live"


def test_a_paper_station_gets_no_shadow(live_cycle, monkeypatch):
    monkeypatch.setitem(executor.EXECUTION_MODE, STATION, "paper")
    scheduler._run_full_cycle(STATION, min_net_ev=0.15)
    assert storage.load_entry_decisions(book="paper_shadow") == []
    assert len(storage.load_entry_decisions(book="paper")) == 2


def test_shadow_is_skipped_with_a_log_line_when_the_primary_raised(live_cycle, monkeypatch, capsys):
    def _boom(*a, **kw):
        raise RuntimeError("primary blew up")
    monkeypatch.setattr(entry_manager, "decide_portfolio_entries", _boom)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert storage.load_entry_decisions(book="paper_shadow") == []
    assert "shadow pass skipped" in capsys.readouterr().out


def test_a_shadow_failure_never_touches_the_primary(live_cycle, monkeypatch, capsys):
    def _boom(results, execution_mode):
        raise RuntimeError("shadow blew up")
    monkeypatch.setattr(ev_engine, "reprice_for_mode", _boom)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert len(live_cycle) == 2
    assert len(storage.load_entry_decisions(book="live")) == 2
    assert "shadow pass failed" in capsys.readouterr().out


def test_shadow_reads_the_paper_book_and_restores_the_log_dedup_sets(live_cycle, monkeypatch):
    reads = []
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: reads.append(kw) or [])
    before = set(entry_manager._bucket_cap_vetoes_logged)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert any(kw.get("is_paper") is True for kw in reads)     # the shadow's cap/budget reads
    assert any(kw.get("is_paper") is False for kw in reads)    # the primary's
    assert entry_manager._bucket_cap_vetoes_logged == before
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave1_shadow_pass.py -v` / Expected: FAIL with `AttributeError: module 'scheduler' has no attribute '_run_shadow_pass'` and `assert 0 == 2` for the shadow rows

- [x] **Step 3: Write minimal implementation**

scheduler.py — new function after `_record_entry_decisions`:

```python
def _run_shadow_pass(station_icao: str, min_net_ev: float, cycle_ts: str,
                     ev_run, forecast_sources) -> list:
    """
    WAVE 1 (spec 1d): the paper twin of a live station's entry cycle.

    Re-prices the PRIMARY pass's own EV table for execution_mode="paper"
    (same quotes, same slippage, no exit fee), screens it the same way, and
    runs decide_portfolio_entries with the explicit execution_mode="paper"
    override -- paper gates, paper Kelly (no $1 clamp), paper cap/budget
    reads -- then records every decision as book='paper_shadow'.

    READ-ONLY, by construction rather than by convention:
      - never calls executor.open_position (tests/test_wave1_shadow_pass.py
        walks the call graph and asserts it);
      - never writes positions; the paper book's cap/budget state is read,
        not consumed;
      - never touches executor.EXECUTION_MODE -- the override is a parameter;
      - restores entry_manager's once-per-day log dedup sets, so a shadow
        veto cannot silence the primary's journal line for the same bucket;
      - its own gate chatter is captured rather than printed, so the journal
        does not show two VETOED lines per bucket per cycle.
    """
    import contextlib
    import io
    import entry_manager

    paper_results = ev_engine.reprice_for_mode(ev_run.ev_results, execution_mode="paper")
    best = ev_engine.best_opportunities(paper_results, min_net_ev=min_net_ev)
    if not best:
        print(f"[scheduler] {station_icao}: paper shadow -- no candidate clears the screen on the paper table; nothing recorded.")
        return []

    dedup_sets = (
        entry_manager._bucket_cap_vetoes_logged,
        entry_manager._opposite_side_vetoes_logged,
        entry_manager._cooldown_vetoes_logged,
        entry_manager._collection_only_logged,
    )
    saved = [set(s) for s in dedup_sets]
    chatter = io.StringIO()
    try:
        with contextlib.redirect_stdout(chatter):
            decisions = entry_manager.decide_portfolio_entries(
                best, ev_run.token_map, min_net_ev=min_net_ev,
                forecast_sources=forecast_sources, execution_mode="paper",
            )
    finally:
        for live_set, before in zip(dedup_sets, saved):
            live_set.clear()
            live_set.update(before)

    _record_entry_decisions(decisions, station_icao, "paper_shadow", cycle_ts)
    approved = sum(1 for d in decisions if d.approved)
    print(
        f"[scheduler] {station_icao}: paper shadow -- {len(decisions)} decision(s), "
        f"{approved} approved, recorded as book='paper_shadow'; no position opened."
    )
    return decisions
```

scheduler.py `_run_full_cycle` — the block introduced in Task 5, old:
```python
    cycle_ts = datetime.now(timezone.utc).isoformat()
    book = executor.EXECUTION_MODE.get(station_icao, "manual_review")

    try:
        estimate = result["estimate"]
```
new:
```python
    cycle_ts = datetime.now(timezone.utc).isoformat()
    book = executor.EXECUTION_MODE.get(station_icao, "manual_review")
    # WAVE 1: what the paper shadow pass below needs from the primary pass,
    # hoisted out of the try so a raise leaves them at their sentinels.
    ev_run = None
    forecast_sources = None
    primary_ok = False

    try:
        estimate = result["estimate"]
        forecast_sources = list(getattr(estimate, "inputs_used", None) or [])
```
old:
```python
            else:
                print(f"[scheduler] {station_icao}: no opportunities clearing the {config.entry_bar_label(min_net_ev)} net EV threshold this cycle.")
    except Exception as exc:
        print(f"[scheduler] {station_icao}: EV computation failed this cycle: {exc}")
```
new:
```python
            else:
                print(f"[scheduler] {station_icao}: no opportunities clearing the {config.entry_bar_label(min_net_ev)} net EV threshold this cycle.")
        primary_ok = True
    except Exception as exc:
        print(f"[scheduler] {station_icao}: EV computation failed this cycle: {exc}")

    # WAVE 1: the paper shadow twin, for live stations only, AFTER the
    # primary pass and only if it completed. Its own failure is contained.
    if executor.EXECUTION_MODE.get(station_icao) == "live":
        if not primary_ok:
            print(f"[scheduler] {station_icao}: paper shadow pass skipped -- the primary pass raised.")
        elif ev_run is None or ev_run.veto_reason or not ev_run.ev_results:
            print(f"[scheduler] {station_icao}: paper shadow pass skipped -- no EV table this cycle.")
        else:
            try:
                _run_shadow_pass(station_icao, min_net_ev, cycle_ts, ev_run, forecast_sources)
            except Exception as exc:  # noqa: BLE001 -- the shadow must never take the cycle down
                print(f"[scheduler] {station_icao}: paper shadow pass failed: {exc} -- primary decisions unaffected.")
```

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave1_shadow_pass.py tests/test_wave1_entry_decisions_recorded.py tests/test_cycle_calibration.py tests/test_exit_snapshot_capture.py -v` / Expected: PASS
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1746
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add scheduler.py tests/test_wave1_shadow_pass.py
git commit -m "Wave 1: paper shadow twin for live stations

After a live station's primary pass, _run_full_cycle re-prices the same
EV table as paper, runs decide_portfolio_entries with the explicit
execution_mode='paper' override and records every decision as
book='paper_shadow'. It never opens a position (an AST walk over its
call graph asserts it), never writes executor.EXECUTION_MODE, restores
the log dedup sets, is skipped with a log line if the primary raised,
and its own failure cannot touch the primary.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: `wave1_falsifier.py` — the four spec 1f checks, read-only
**Files:** Create `wave1_falsifier.py` / Test `tests/test_wave1_falsifier.py`
**Interfaces:** Consumes: `entry_decisions`, `positions` (Tasks 1-8) / Produces: `wave1_falsifier.run(db_path: str, deploy_ts: str) -> dict` with keys `refusals_by_cycle: list[tuple[str, int]]`, `refusals_total: int`, `calibration_gap_rows: int`, `paper_shadow_rows: int`, `entries_per_station_day_before: dict[str, dict[str, int]]`, `entries_per_station_day_after: dict[str, dict[str, int]]`; CLI `python wave1_falsifier.py --deploy-ts 2026-09-19T05:00:00+00:00 [--db PATH]`

- [x] **Step 1: Write the failing test**

```python
# tests/test_wave1_falsifier.py
"""The four Wave 1 falsifier checks, on a fixture database, through a read-only connection."""
import sqlite3
from datetime import date

import pytest

import config
import storage
import wave1_falsifier
from models import EntryDecision, Position

DEPLOY = "2026-09-19T05:00:00+00:00"


def _decision(bucket, approved, rule_id):
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 19), bucket_c=bucket, side="YES",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=1.0 if approved else 0.0,
        available_depth_usd=None, slippage_at_size_pct=None, net_ev_at_size=None,
        approved=approved, reason="r", station_maturity="mature", entry_price=0.3, rule_id=rule_id,
    )


def _position(pid, entry_time, calibrated_prob, source, station="WSSS"):
    return Position(
        position_id=pid, station_icao=station, target_date=date(2026, 9, 19), bucket_c=32, side="YES",
        entry_price=0.3, size_usd=1.0, entry_time=entry_time, status="open", is_paper=False,
        execution_mode="live", calibrated_prob=calibrated_prob, calibration_source=source,
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.sqlite3")
    monkeypatch.setattr(config, "DB_PATH", path)
    storage._connect().close()
    storage.record_entry_decisions([_decision(32, True, "approved"), _decision(33, False, "0a2")],
                                   book="live", cycle_ts="2026-09-19T05:00:10+00:00", config_sha="s")
    storage.record_entry_decisions([_decision(32, False, "0b"), _decision(33, False, "0a2")],
                                   book="live", cycle_ts="2026-09-19T05:10:10+00:00", config_sha="s")
    storage.record_entry_decisions([_decision(32, True, "approved")],
                                   book="paper_shadow", cycle_ts="2026-09-19T05:00:10+00:00", config_sha="s")
    storage.open_position(_position("before-1", "2026-09-15T05:00:00+00:00", None, None))
    storage.open_position(_position("after-ok", "2026-09-19T05:30:00+00:00", 0.4, "pooled_isotonic"))
    storage.open_position(_position("after-uncal", "2026-09-19T05:31:00+00:00", None, "uncalibrated"))
    storage.open_position(_position("after-gap", "2026-09-19T05:32:00+00:00", None, "station_isotonic"))
    return path


def test_the_four_checks(db):
    out = wave1_falsifier.run(db, DEPLOY)

    assert out["refusals_total"] == 3
    assert out["refusals_by_cycle"] == [("2026-09-19T05:00:10+00:00", 1), ("2026-09-19T05:10:10+00:00", 2)]
    assert out["calibration_gap_rows"] == 1            # after-gap only; pre-deploy NULL source is not a gap
    assert out["paper_shadow_rows"] == 1
    assert out["entries_per_station_day_before"] == {"WSSS": {"2026-09-15": 1}}
    assert out["entries_per_station_day_after"] == {"WSSS": {"2026-09-19": 3}}


def test_the_connection_is_read_only(db, monkeypatch):
    calls = []
    real = sqlite3.connect

    def _spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(wave1_falsifier.sqlite3, "connect", _spy)
    wave1_falsifier.run(db, DEPLOY)
    assert calls and calls[0][0][0].endswith("?mode=ro") and calls[0][1].get("uri") is True


def test_main_prints_every_check(db, capsys):
    wave1_falsifier.main(["--db", db, "--deploy-ts", DEPLOY])
    out = capsys.readouterr().out
    for label in ("refused decisions", "calibration gap", "paper_shadow rows", "entries per station-day"):
        assert label in out
```

- [x] **Step 2: Run test to verify it fails** — Run: `pytest tests/test_wave1_falsifier.py -v` / Expected: FAIL with `ModuleNotFoundError: No module named 'wave1_falsifier'`

- [x] **Step 3: Write minimal implementation**

```python
# wave1_falsifier.py
"""
wave1_falsifier.py -- the four Wave 1 checks from the spec (section 1f),
run the day of deploy and appended to memory.

    python wave1_falsifier.py --deploy-ts 2026-09-19T05:00:00+00:00

Opens the database READ-ONLY (mode=ro): this is an operator read on the
live box and must not be able to write, migrate, or take a write lock.
It therefore does not go through storage._connect(), which issues DDL.

  1. entry_decisions WHERE approved=0 grows every entry cycle
     -> printed per cycle_ts, newest last; a flat count across cycles is
        the failure.
  2. positions WHERE calibrated_prob IS NULL AND calibration_source !=
     'uncalibrated' AND entry_time > deploy_ts  -> must be 0. Pre-deploy
     rows have calibration_source NULL and are excluded by the comparison.
  3. entry_decisions WHERE book='paper_shadow'  -> > 0 once a live station
     has run an entry cycle (WSSS/RCSS at 05:00 SGT), wallet funded or not.
     0 is only a failure if check 1 shows live-book rows for that station
     in the same cycle: the shadow screen is the paper table, which has no
     exit fee, so it clears whenever the live screen did.
  4. entries per station-day, 7 days before vs 7 days after deploy, from
     positions -- a record-only wave must not move them.
"""
import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import config


def _ro_connect(db_path: str) -> sqlite3.Connection:
    # A URI filename wants forward slashes, on Windows too (dev box); on the
    # Linux box as_posix() is the identity.
    return sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)


def _iso_plus_days(ts: str, days: int) -> str:
    return (datetime.fromisoformat(ts) + timedelta(days=days)).isoformat()


def run(db_path: str, deploy_ts: str) -> dict:
    con = _ro_connect(db_path)
    try:
        by_cycle = con.execute(
            "SELECT cycle_ts, COUNT(*) FROM entry_decisions WHERE approved = 0 "
            "GROUP BY cycle_ts ORDER BY cycle_ts"
        ).fetchall()
        refusals_total = sum(n for _, n in by_cycle)
        calibration_gap_rows = con.execute(
            "SELECT COUNT(*) FROM positions WHERE calibrated_prob IS NULL "
            "AND calibration_source != 'uncalibrated' AND entry_time > ?",
            (deploy_ts,),
        ).fetchone()[0]
        paper_shadow_rows = con.execute(
            "SELECT COUNT(*) FROM entry_decisions WHERE book = 'paper_shadow'"
        ).fetchone()[0]

        def _per_station_day(lo: str, hi: str) -> dict:
            rows = con.execute(
                "SELECT station_icao, substr(entry_time, 1, 10) AS day, COUNT(*) FROM positions "
                "WHERE entry_time >= ? AND entry_time < ? GROUP BY station_icao, day "
                "ORDER BY station_icao, day",
                (lo, hi),
            ).fetchall()
            out: dict = {}
            for icao, day, n in rows:
                out.setdefault(icao, {})[day] = n
            return out

        before = _per_station_day(_iso_plus_days(deploy_ts, -7), deploy_ts)
        after = _per_station_day(deploy_ts, _iso_plus_days(deploy_ts, 7))
    finally:
        con.close()
    return {
        "refusals_by_cycle": [(ts, n) for ts, n in by_cycle],
        "refusals_total": refusals_total,
        "calibration_gap_rows": calibration_gap_rows,
        "paper_shadow_rows": paper_shadow_rows,
        "entries_per_station_day_before": before,
        "entries_per_station_day_after": after,
    }


def _print(out: dict, deploy_ts: str) -> None:
    print(f"Wave 1 falsifier -- deploy_ts {deploy_ts}\n")
    print(f"1. refused decisions: {out['refusals_total']} total, by cycle (must grow every entry cycle):")
    for ts, n in out["refusals_by_cycle"][-20:]:
        print(f"     {ts}  {n}")
    print(f"2. calibration gap rows (calibrated_prob NULL with a calibrated source, after deploy): "
          f"{out['calibration_gap_rows']}  -> must be 0")
    print(f"3. paper_shadow rows: {out['paper_shadow_rows']}  -> > 0 once a live station ran an entry cycle")
    print("4. entries per station-day, 7d before -> 7d after (record-only wave: unchanged):")
    stations = sorted(set(out["entries_per_station_day_before"]) | set(out["entries_per_station_day_after"]))
    for icao in stations:
        b = out["entries_per_station_day_before"].get(icao, {})
        a = out["entries_per_station_day_after"].get(icao, {})
        print(f"     {icao}: before {sum(b.values())} over {len(b)} day(s), after {sum(a.values())} over {len(a)} day(s)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deploy-ts", required=True, help="ISO-8601 UTC deploy timestamp, e.g. 2026-09-19T05:00:00+00:00")
    parser.add_argument("--db", default=str(config.DB_PATH), help="database path (opened read-only)")
    args = parser.parse_args(argv)
    _print(run(args.db, args.deploy_ts), args.deploy_ts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 4: Run test to verify it passes** — Run: `pytest tests/test_wave1_falsifier.py -v` / Expected: PASS (3 passed)
- [x] **Step 5: Run the full suite** — `pytest -q` from weather-forecast/ / Expected: all pass, count >= 1749
- [x] **Step 6: Commit**

```bash
cd "C:/Users/user/Downloads/weather-forecast/weather-forecast"
git add wave1_falsifier.py tests/test_wave1_falsifier.py
git commit -m "Wave 1: wave1_falsifier.py prints the four deploy-day checks read-only

Refusals per cycle, the calibration-gap count on post-deploy positions,
the paper_shadow row count, and entries per station-day seven days
either side of the deploy -- over a mode=ro connection that bypasses
storage._connect() so an operator read can never migrate or lock the
live database.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Deploy notes (after all nine tasks, per spec "Rollout")

1. `pytest -q` locally: all green, count >= 1749. Never on the box.
2. Merge the branch to `main`; push.
3. Schema changes -> backup-and-stop deploy (the P1-8(b) shape), not during 05:00-08:00 SGT. The migration runs on the daemon's first `storage._connect()`.
4. On the box: `git log -1` == main; then `python wave1_falsifier.py --deploy-ts <UTC deploy ts>` after the next 05:00 SGT entry cycle; append the output to memory.
5. Spec 1d's pairing query for later analysis: `entry_decisions(book='paper_shadow', approved=1)` joined to `positions(execution_mode='live')` on `(station_icao, target_date, bucket_c, side)`; `ix_ed_pair` covers it.

## Spec coverage

| spec | task |
|---|---|
| 1a five columns + migration + `net_ev_at_size` at resolved size | 1, 2, 4 |
| 1b `entry_decisions` table, `rule_id` at every site incl. entry_sim, written after `decide_portfolio_entries` before the executor, best-effort | 1, 3, 5 |
| 1c executor refusals as `live_order_attempts` rows | 6 |
| 1d paper shadow twin: paper EV table, paper gates/sizing/budget view, no `open_position`, skipped if primary raised | 7, 8 |
| 1e decision-identity, field-parity, `rule_id` census, shadow AST guard, migration, refusal-row tests | 5, 5, 3, 8, 1, 6 |
| 1f falsifier queries | 9 |

## Where the code forced a deviation from the spec

1. **Three `rule_id`s the spec's list omits: `budget_exhausted`, `same_bucket_conflict`, `collection_gate` is listed but `same_bucket_conflict` and `budget_exhausted` are not.** `veto_same_bucket_conflicts` and the exhausted branch of `apply_portfolio_budget` both rebuild an `EntryDecision` with `approved=False`; `rule_id` is `NOT NULL` and every decision gets a row, so each needs an id, and filing a rejection under `budget_scaled` would be wrong. (Task 3)
2. **`entry_decisions` carries `min_net_ev` in addition to the spec's columns.** The bar is per-scan-window, and a `net_ev_bar` refusal is unreadable without the bar it was measured against. The field-parity test would otherwise have had to list `min_net_ev` as bookkeeping, which it is not. (Tasks 1, 5)
3. **`kind` on refusal rows stays `'entry'` and `count_live_order_attempts` excludes `outcome='refused'`.** That counter is the daily order cap and it filters on `kind` and `ts` only; writing refusals under `kind='entry'` without the exclusion would have consumed the real order budget — a trading change. Refusal rows are also written only in live mode, matching `_record_attempt`'s scope, so simulation refusals do not enter the live audit trail. (Task 6)
4. **`net_ev_at_size` at the resolved size is recorded on simulation rows too, not only live.** Both rungs record at the resolved notional through the same code path; leaving simulation on the $1.00 figure would make the two rungs incomparable for no gain. (Task 4)
5. **`_run_shadow_pass` takes the primary pass's `ev_run` and `forecast_sources`, not just `(station_icao, min_net_ev, cycle_ts)`.** The mode reaches the EV table as a parameter, but `run_for_station_with_map` also re-discovers the market and writes price snapshots, so "the same cycle as paper" is obtained by re-pricing the primary's own results (`ev_engine.reprice_for_mode`), not by a second fetch. (Tasks 7-8)
6. **The entry gates read the mode from `executor.EXECUTION_MODE` (a module dict), so the shadow pass threads an explicit `execution_mode` override parameter** through `decide_portfolio_entries → decide_entries → evaluate_entry → _candidate_is_paper / _book_has_stop / live_size_cap_usd`, all defaulting to today's lookup. The dict is never written. (Task 7)
7. **The shadow pass restores `entry_manager`'s four log-dedup sets and captures its own stdout.** Not in the spec; without it a shadow veto would consume the once-per-day journal key for a bucket and silence the primary's line, and the journal would show two `VETOED` lines per bucket per cycle. (Task 8)
