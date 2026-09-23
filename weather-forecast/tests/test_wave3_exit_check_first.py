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
from datetime import date
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


@pytest.fixture
def gamma_unreachable(monkeypatch):
    """
    Gamma itself can't say (lookup failed / None), as opposed to `blind`'s
    definite True. The FIRST-FAILURE change only moves the ASK earlier; the
    observation-record fallback for an unreachable Gamma still needs the
    same three-failure UNMONITORABLE_CYCLES_WARN cushion it always did.
    """
    calls = {"gamma": 0, "settlement": 0}
    monkeypatch.setattr(market_client, "get_current_price_for_side", lambda token_id, side: None)
    monkeypatch.setattr(position_manager, "_market_reported_closed",
                        lambda position: calls.__setitem__("gamma", calls["gamma"] + 1) or None)
    monkeypatch.setattr(position_manager, "_close_from_settlement_source",
                        lambda position, gamma_closed=None: calls.__setitem__("settlement", calls["settlement"] + 1) or
                            ExitDecision(position_id=position.position_id, should_exit=True,
                                        reason="resolution", current_price=0.0, pnl_pct=-100.0))
    position_manager._consecutive_price_failures.clear()
    return calls


def test_a_past_dated_position_with_gamma_unreachable_still_waits_three_failures(gamma_unreachable):
    """
    The controller ruling: 3c moved the ASK earlier, not the fallback. On
    the first failure Gamma is asked (past-dated) but comes back None, so
    the position stays open one and two failures in; only the third failure
    -- the same UNMONITORABLE_CYCLES_WARN cushion as before Wave 3 -- falls
    back to the observation record.
    """
    pos = _live_position("p6", date(2026, 1, 1))
    assert position_manager._check_one_position(pos) is None
    assert gamma_unreachable["gamma"] == 1
    assert gamma_unreachable["settlement"] == 0

    assert position_manager._check_one_position(pos) is None
    assert gamma_unreachable["settlement"] == 0

    decision = position_manager._check_one_position(pos)
    assert gamma_unreachable["settlement"] == 1
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
