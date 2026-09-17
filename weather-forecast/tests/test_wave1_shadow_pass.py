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
from executor import open_position
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
WALKED_MODULES = ("scheduler", "entry_manager", "ev_engine", "storage", "executor")


def _reachable(fn, seen=None):
    """Transitively walk fn's calls into scheduler/entry_manager/ev_engine/storage/executor, failing on a forbidden call."""
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
            module = sys.modules.get(f.value.id)
            target = getattr(module, f.attr, None) if module is not None else None
        elif isinstance(f, ast.Name):
            target = fn.__globals__.get(f.id)
        else:
            continue
        # Checked on the RESOLVED target's own (__module__, __name__), not
        # the source text of the call -- so a bare open_position(...) call
        # (an ast.Name, reachable via `from executor import open_position`)
        # is caught exactly like the ast.Attribute form executor.open_
        # position(...) is, and an aliased `import executor as ex` would be
        # too, because the check no longer cares what name the caller wrote.
        pair = (getattr(target, "__module__", None), getattr(target, "__name__", None))
        assert pair not in FORBIDDEN, f"{fn.__module__}.{fn.__qualname__} reaches {pair[0]}.{pair[1]}"
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


def test_the_guard_catches_a_bare_open_position_call():
    """
    Negative control for hole (a): `from executor import open_position` (see
    the module import above) then calling it BARE -- an ast.Name call, not
    an ast.Attribute access like executor.open_position(...) -- must be
    caught too. Before the fix, the FORBIDDEN check only ran in the
    ast.Attribute branch, so this exact evasion was invisible.
    """
    def _sneaky_bare_call():
        open_position(None)
    with pytest.raises(AssertionError, match="executor.open_position"):
        _reachable(_sneaky_bare_call)


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
    exit_calls = []
    monkeypatch.setattr(scheduler, "_run_exit_check", lambda *a, **kw: exit_calls.append((a, kw)))

    def _boom(results, execution_mode):
        raise RuntimeError("shadow blew up")
    monkeypatch.setattr(ev_engine, "reprice_for_mode", _boom)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert len(live_cycle) == 2
    assert len(storage.load_entry_decisions(book="live")) == 2
    assert "shadow pass failed" in capsys.readouterr().out
    # The shadow's failure must not skip the rest of the cycle either.
    assert len(exit_calls) == 1


def test_shadow_still_records_when_order_placement_fails(live_cycle, monkeypatch, capsys):
    """
    Controller ruling (fix round 1, item 2): "the primary raised" means the
    EVALUATION raised (pipeline -> EV -> decide -> record), not that every
    order placed cleanly. primary_ok now flips right after the primary's
    decisions are recorded, before the executor loop -- so a CLOB/network
    error placing one of them still lets the shadow pass run for the same
    cycle_ts, and the primary's own except still logs the order failure.
    """
    calls = []

    def _flaky_open(d):
        calls.append(d)
        if len(calls) == 2:
            raise RuntimeError("CLOB order failed")
    monkeypatch.setattr(executor, "open_position", _flaky_open)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    live_rows = storage.load_entry_decisions(book="live")
    shadow_rows = storage.load_entry_decisions(book="paper_shadow")
    assert len(live_rows) == 2
    assert len(shadow_rows) == 2
    assert {r["cycle_ts"] for r in live_rows} == {r["cycle_ts"] for r in shadow_rows}
    assert "EV computation failed this cycle" in capsys.readouterr().out


def test_shadow_reads_the_paper_book_and_restores_the_log_dedup_sets(live_cycle, monkeypatch):
    reads = []
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: reads.append(kw) or [])
    before = set(entry_manager._bucket_cap_vetoes_logged)

    scheduler._run_full_cycle(STATION, min_net_ev=0.15)

    assert any(kw.get("is_paper") is True for kw in reads)     # the shadow's cap/budget reads
    assert any(kw.get("is_paper") is False for kw in reads)    # the primary's
    assert entry_manager._bucket_cap_vetoes_logged == before
