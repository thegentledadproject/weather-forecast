"""
Wave 2 item 2e. decide_portfolio_entries built today_source_mix as
`frozenset(forecast_sources) if forecast_sources else None`, and the mix
guard tested `bias_source_mix and today_source_mix and ...` -- so an EMPTY
source list (every forecast fetch failed this cycle; the estimate fell
through to observed/normal) read as "mix unknown, skip the guard" and the
station kept trading on a central estimate with no forecast term at all
(edge review 2026-09-15: 'total forecast outage fails open').

`[]` and `None` are different facts. None still means "the caller was not
taught to pass a mix" and skips the guard (entry_sim, operator scripts).
[] now becomes frozenset(), the guard compares it against the fitted mix,
and refuses with every fitted source reported missing. rule_id stays
collection_gate, through collection_only_decision.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from clients import market_client
from models import EVResult

FITTED = frozenset({"open_meteo_ecmwf", "open_meteo_gfs"})
DAY = date(2026, 9, 21)


# --- the pure guard ------------------------------------------------------

def _reason(today_mix):
    return entry_manager.collection_only_reason(
        "WSSS", 999, bias_n=99, bias_stderr=0.1, enforce_bias_quality=True,
        bias_source_mix=FITTED, today_source_mix=today_mix,
    )


def test_an_empty_mix_is_refused_by_the_guard():
    reason = _reason(frozenset())
    assert reason is not None
    assert "missing open_meteo_ecmwf, open_meteo_gfs" in reason


def test_an_unknown_mix_still_skips_the_guard():
    assert _reason(None) is None


# --- the translation at the call site ------------------------------------

def test_none_stays_none_and_empty_becomes_the_empty_set():
    assert entry_manager.today_source_mix_for(None) is None
    assert entry_manager.today_source_mix_for([]) == frozenset()
    assert entry_manager.today_source_mix_for(["a", "b"]) == frozenset({"a", "b"})


def test_flag_off_folds_empty_back_to_none(monkeypatch):
    monkeypatch.setattr(config, "REFUSE_ON_TOTAL_FORECAST_OUTAGE", False)
    assert entry_manager.today_source_mix_for([]) is None
    assert entry_manager.today_source_mix_for(["a"]) == frozenset({"a"})


# --- end to end through decide_portfolio_entries -------------------------

def _ev(bucket=32):
    return EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=bucket, side="YES",
        model_prob=0.55, market_price=0.35, raw_edge=0.20, estimated_slippage_pct=0.01,
        fee_rate_pct=0.02, net_ev_per_dollar=0.20 / 0.35 - 0.03, spread_source="corrected_error",
        market_bid=0.33,
    )


@pytest.fixture
def graduated_wsss(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(executor, "EXECUTION_MODE", {icao: "paper" for icao in config.STATIONS})
    monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.1, 20, 0.1))
    monkeypatch.setattr(entry_manager, "resolution_obs_count",
                        lambda icao: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY)
    monkeypatch.setattr(entry_manager, "forecast_bias_source_mix", lambda icao: FITTED)
    monkeypatch.setattr(entry_manager, "station_error_width_ratio", lambda icao: None)
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)
    entry_manager._collection_only_logged.clear()
    return {32: {"yes_token_id": "y32", "no_token_id": "n32"}}


def test_a_total_outage_refuses_every_candidate_at_collection_gate(graduated_wsss):
    decisions = entry_manager.decide_portfolio_entries(
        [_ev()], graduated_wsss, min_net_ev=0.15, forecast_sources=[],
    )
    assert len(decisions) == 1
    assert not decisions[0].approved
    assert decisions[0].rule_id == "collection_gate"
    assert "missing open_meteo_ecmwf, open_meteo_gfs" in decisions[0].reason


def test_a_caller_that_passes_none_is_not_gated_on_the_mix(graduated_wsss):
    decisions = entry_manager.decide_portfolio_entries(
        [_ev()], graduated_wsss, min_net_ev=0.15, forecast_sources=None,
    )
    assert decisions[0].rule_id != "collection_gate"


def test_the_fitted_mix_still_passes(graduated_wsss):
    decisions = entry_manager.decide_portfolio_entries(
        [_ev()], graduated_wsss, min_net_ev=0.15, forecast_sources=sorted(FITTED),
    )
    assert decisions[0].rule_id != "collection_gate"
