"""
Wave 2 item 2c. Veto 0a2 compared abs(gate_edge) against MIN_ABS_RAW_EDGE.
gate_edge is ALREADY SIDE-ADJUSTED on both bases -- ev_engine stores
side_model_prob = P(this side wins) and raw_edge = side_model_prob - price,
and the calibrated edge is apply_map(side_model_prob) - price -- so a
NEGATIVE gate_edge means this side is overpriced, and abs() was admitting
it as if it were a disagreement in our favour. On the raw basis it never
mattered live (best_opportunities screens on net_ev first); on the
calibrated basis it does: the isotonic map has a hard zero under ~0.096, so
a model_prob 0.09 at ask 0.05 has raw edge +0.04 (passes) and calibrated
edge -0.05 (abs 0.05 -- passes 0a2, then fails Kelly). The refusal was
right by accident and recorded under the wrong rule_id. Signed compare,
same rule_id, mirrored in entry_sim.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
import probability_calibration as pc
from backtest import entry_sim
from models import EVResult

DAY = date(2026, 9, 21)


def _ev(model_prob, price, calibrated=None, source=pc.NO_TIER, side="YES"):
    return EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=32, side=side,
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.0, fee_rate_pct=0.0,
        net_ev_per_dollar=(model_prob - price) / price,
        calibrated_prob=calibrated, calibration_source=source,
        # A measured source, so the LOW_CONFIDENCE doubling of the bar does
        # not confound the sign test (EVResult defaults to fallback_default).
        spread_source="corrected_error",
    )


@pytest.fixture
def no_live_io(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(executor, "EXECUTION_MODE", {icao: "paper" for icao in config.STATIONS})
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)
    monkeypatch.setattr(entry_manager, "count_stop_outs_for_bucket", lambda *a, **k: 0)


def _sim(ev):
    return entry_sim.evaluate_entry_sim(
        ev, token_id="tok", open_count_for_bucket=0, opposite_count_for_bucket=0,
        stop_outs_for_bucket=0, depth_usd=100_000.0, slippage_fn=lambda s: 0.0,
        min_net_ev=-9.0, sizing_bankroll=1_000.0,
    )


# The hard-zero case: raw +0.04 clears the 0.03 bar, calibrated is -0.05.
HARD_ZERO = dict(model_prob=0.09, price=0.05, calibrated=0.0, source=pc.STATION_TIER)


def test_a_negative_calibrated_edge_is_refused_at_0a2_not_at_kelly(no_live_io):
    decision = entry_manager.evaluate_entry(_ev(**HARD_ZERO), token_id="tok", min_net_ev=-9.0)
    assert not decision.approved
    assert decision.rule_id == "0a2"
    assert decision.admission_edge == pytest.approx(-0.05)
    assert pc.STATION_TIER in decision.reason


def test_the_replica_refuses_at_the_same_rule(no_live_io):
    assert _sim(_ev(**HARD_ZERO)).rule_id == "0a2"


def test_flag_off_restores_the_abs_compare(no_live_io, monkeypatch):
    """The revert path: abs(-0.05) >= 0.03 passes 0a2 and Kelly refuses."""
    monkeypatch.setattr(config, "SIGNED_ADMISSION_EDGE", False)
    live = entry_manager.evaluate_entry(_ev(**HARD_ZERO), token_id="tok", min_net_ev=-9.0)
    assert live.rule_id == "kelly_nonpositive"
    assert _sim(_ev(**HARD_ZERO)).rule_id == "kelly_nonpositive"


def test_a_positive_edge_that_clears_the_bar_still_trades(no_live_io):
    decision = entry_manager.evaluate_entry(
        _ev(0.46, 0.30, calibrated=0.38, source=pc.STATION_TIER), token_id="tok", min_net_ev=-9.0)
    assert decision.approved


def test_a_small_positive_edge_is_still_refused(no_live_io):
    decision = entry_manager.evaluate_entry(
        _ev(0.432, 0.30, calibrated=0.301, source=pc.STATION_TIER), token_id="tok", min_net_ev=-9.0)
    assert decision.rule_id == "0a2"


def test_the_raw_basis_is_side_adjusted_so_signed_is_right_there_too(no_live_io):
    """A NO with P(NO wins)=0.66 at a NO ask of 0.70 is a -0.04 edge: an
    overpriced side. abs() admitted it; signed refuses it at 0a2."""
    decision = entry_manager.evaluate_entry(
        _ev(0.66, 0.70, side="NO"), token_id="tok", min_net_ev=-9.0)
    assert decision.rule_id == "0a2"
    good = entry_manager.evaluate_entry(_ev(0.70, 0.60, side="NO"), token_id="tok", min_net_ev=-9.0)
    assert good.rule_id != "0a2"


def test_the_helper_is_the_one_both_sides_call():
    import inspect
    assert "edge_misses_bar(" in inspect.getsource(entry_manager.evaluate_entry)
    assert "edge_misses_bar(" in inspect.getsource(entry_sim.evaluate_entry_sim)
    assert entry_manager.edge_misses_bar(-0.05, 0.03) is True
    assert entry_manager.edge_misses_bar(0.05, 0.03) is False
    assert entry_manager.edge_misses_bar(0.02, 0.03) is True
