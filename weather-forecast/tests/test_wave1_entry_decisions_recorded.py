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
    storage.migrate()
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
        contract_status="VALID",
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
    # best_opportunities ranks by net-EV-per-share, not input order, so the
    # vetoed 33-bucket (higher per-share edge) sorts before the approved
    # 32-bucket -- match by bucket_c rather than assuming position 0.
    opened_by_bucket = {d.bucket_c: d for d in cycle["opened"]}
    assert by_bucket[32]["recommended_size_usd"] == pytest.approx(opened_by_bucket[32].recommended_size_usd)
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
    """
    Every column in ENTRY_DECISION_COLUMNS is value-asserted against its
    source -- either the EntryDecision field it mirrors or the explicit
    record_entry_decisions() argument -- for two decisions: one with every
    field set to a distinct non-default value, one left at the
    dataclass's defaults. This is the field-parity check for VALUES, not
    just column names: it is what would catch record_entry_decisions()
    writing a correct-looking row into the wrong columns after a reorder.
    """
    d_full = EntryDecision(
        station_icao=STATION, target_date=TARGET, bucket_c=32, side="NO",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=12.5,
        available_depth_usd=None, slippage_at_size_pct=None, net_ev_at_size=0.05,
        approved=False, reason="model_prob below floor", station_maturity="mature",
        entry_price=0.60, entry_bid=0.58, model_prob=0.60, raw_edge=0.0, min_net_ev=0.15,
        rule_id="00c", calibrated_prob=0.42, calibration_source="isotonic",
        admission_edge=0.11, sizing_edge=0.09, kelly_size_preclamp_usd=20.0,
    )
    d_defaults = EntryDecision(
        station_icao=STATION, target_date=TARGET, bucket_c=33, side="YES",
        kelly_fraction_raw=0.02, kelly_fraction_applied=0.01, recommended_size_usd=5.0,
        available_depth_usd=500.0, slippage_at_size_pct=0.01, net_ev_at_size=0.2,
        approved=True, reason="approved", station_maturity="exploratory",
    )
    n = storage.record_entry_decisions(
        [d_full, d_defaults], book="paper_shadow",
        cycle_ts="2026-09-17T21:00:00+00:00", config_sha="deadbeef",
    )
    assert n == 2
    rows = {r["bucket_c"]: r for r in storage.load_entry_decisions(book="paper_shadow")}

    expected_full = {
        "cycle_ts": "2026-09-17T21:00:00+00:00", "station_icao": STATION,
        "target_date": "2026-09-17", "bucket_c": 32, "side": "NO", "book": "paper_shadow",
        "approved": 0, "rule_id": "00c", "reason": "model_prob below floor",
        "entry_price": 0.60, "entry_bid": 0.58, "model_prob": 0.60,
        "calibrated_prob": 0.42, "calibration_source": "isotonic",
        "raw_edge": 0.0, "admission_edge": 0.11, "sizing_edge": 0.09,
        "net_ev_at_size": 0.05, "kelly_size_preclamp_usd": 20.0,
        "recommended_size_usd": 12.5, "min_net_ev": 0.15,
        "station_maturity": "mature", "config_sha": "deadbeef",
    }
    assert set(expected_full) == set(storage.ENTRY_DECISION_COLUMNS)
    for col, val in expected_full.items():
        assert rows[32][col] == val, f"column {col!r}: expected {val!r}, got {rows[32][col]!r}"

    expected_defaults = {
        "cycle_ts": "2026-09-17T21:00:00+00:00", "station_icao": STATION,
        "target_date": "2026-09-17", "bucket_c": 33, "side": "YES", "book": "paper_shadow",
        "approved": 1, "rule_id": "unspecified", "reason": "approved",
        "entry_price": None, "entry_bid": None, "model_prob": None,
        "calibrated_prob": None, "calibration_source": "uncalibrated",
        "raw_edge": None, "admission_edge": None, "sizing_edge": None,
        "net_ev_at_size": 0.2, "kelly_size_preclamp_usd": None,
        "recommended_size_usd": 5.0, "min_net_ev": None,
        "station_maturity": "exploratory", "config_sha": "deadbeef",
    }
    assert set(expected_defaults) == set(storage.ENTRY_DECISION_COLUMNS)
    for col, val in expected_defaults.items():
        assert rows[33][col] == val, f"column {col!r}: expected {val!r}, got {rows[33][col]!r}"

    assert storage.load_entry_decisions(book="live") == []
    assert storage.record_entry_decisions([], book="paper", cycle_ts="x", config_sha=None) == 0
