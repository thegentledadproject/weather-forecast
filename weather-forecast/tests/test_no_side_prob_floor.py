"""
tests/test_no_side_prob_floor.py

Veto 00c -- refuse a NO-side entry when the model's own claimed win probability
is below config.NO_SIDE_MIN_MODEL_PROB.

THE DEFECT THIS CLOSES. Measured 2026-09-09 over 715 settled rows
(2026-08-06..09-08, $5,053.75 staked) scored held-to-settlement through
cohort_monitor. The model overstates its own win rate by +10.7 points on the
traded sample (mean model_prob 0.445 against an actual win rate of 0.338); the
market, on the same trades, is off by -2.7 points and in our favour. The
overstatement concentrates on the NO side at low confidence, where a claimed
~55% is closer to a coin flip and the book pays spread and fee to take it.
Refusing NO below 0.70 moves the book from +7.7% to +17.2% held, the refused
trades are -23.9% (CI [-45.3%, -1.7%]), and a station-day clustered bootstrap
puts the improvement at +9.5 pts, 90% CI [+4.5, +14.7], P(improves) = 1.00.

WHY 0.65 AND NOT THE SWEPT PEAK OF 0.70. Peaks of a swept parameter move when
data arrives. 0.65 still improves BOTH disjoint windows (Aug 6-25 +17.2% ->
+19.3%; Aug 26-Sep 8 +3.8% -> +11.8%), which is the standard this project set
at ENTRY_PRICE_BLOCK_BAND: act only where the ordering holds per station AND
across windows.

WHY THIS IS NOT REDUNDANT WITH ADMIT_ON_CALIBRATED_EDGE (shipped the same day).
Measured, not assumed: of the 74 trades this floor refuses, 66 -- 89% -- are
STILL admitted by the calibrated gate, and those 66 are worth -28.8% held on
$432.39. The correction is far too small here, mapping a mean model_prob of
0.553 to 0.524 and leaving a calibrated edge of +0.121 against a 0.03 bar.

WHY THE GATE READS THE RAW PROBABILITY. On the pooled map (30 of 35 stations)
claims of 0.55, 0.60 and 0.65 ALL map to 0.489 -- a flat step. The calibrated
probability carries no information across exactly the range this gate must
discriminate in, so gating on it would be gating on a constant. This is the
opposite choice from ADMIT_ON_CALIBRATED_EDGE, and deliberately so: that gate
tests the model's EDGE, this one tests its CONFIDENCE.
"""
from datetime import date

import pytest

import config
import entry_manager
import probability_calibration as pc
from backtest import entry_sim
from models import EVResult

DAY = date(2026, 9, 9)

REASON = "below the NO-side confidence floor"


def _ev(side="NO", model_prob=0.55, price=0.30, calibrated=None, source=pc.NO_TIER):
    return EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=32, side=side,
        model_prob=model_prob, market_price=price,
        raw_edge=model_prob - price, estimated_slippage_pct=0.0,
        fee_rate_pct=0.0, net_ev_per_dollar=(model_prob - price) / price,
        calibrated_prob=calibrated, calibration_source=source,
    )


@pytest.fixture
def no_live_io(monkeypatch):
    """Everything evaluate_entry() would reach the network or the book for."""
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)


@pytest.fixture(autouse=True)
def floor_armed(monkeypatch):
    """
    Pin the threshold so a later tuning of the shipped value cannot silently
    rewrite what these tests assert.
    """
    monkeypatch.setattr(config, "NO_SIDE_MIN_MODEL_PROB", 0.65, raising=False)


# ---------------------------------------------------------------------------
# config.no_side_prob_is_blocked(): the one place the comparison is made
# ---------------------------------------------------------------------------

def test_a_no_side_claim_under_the_floor_is_blocked():
    assert config.no_side_prob_is_blocked("NO", 0.64) is True


def test_the_floor_itself_is_admitted():
    """Boundary is >=, matching the half-open convention the band gate uses."""
    assert config.no_side_prob_is_blocked("NO", 0.65) is False


def test_the_yes_side_is_never_blocked():
    """The measurement is NO-side only; YES at low confidence was not scored."""
    assert config.no_side_prob_is_blocked("YES", 0.20) is False


def test_an_unknown_probability_is_not_blocked():
    """
    FAILS OPEN, matching entry_price_is_blocked(). Every other gate already
    fails closed on its own terms, and failing closed twice would turn one
    missing field into a silent second veto whose reason names the wrong thing.
    """
    assert config.no_side_prob_is_blocked("NO", None) is False


def test_none_disables_the_gate_entirely(monkeypatch):
    """The revert path is setting the constant to None, not tuning it."""
    monkeypatch.setattr(config, "NO_SIDE_MIN_MODEL_PROB", None, raising=False)
    assert config.no_side_prob_is_blocked("NO", 0.10) is False


# ---------------------------------------------------------------------------
# The gate on the live path
# ---------------------------------------------------------------------------

def test_a_low_confidence_no_entry_is_refused(no_live_io):
    """
    THE POINT OF THE CHANGE. A claimed 55% on the NO side at an ask of 0.30
    clears every existing gate -- 25c of raw edge is materially above
    MIN_ABS_RAW_EDGE -- and this cohort returned -23.9% held.
    """
    decision = entry_manager.evaluate_entry(_ev(model_prob=0.55), token_id="tok", min_net_ev=-9.0)

    assert not decision.approved
    assert REASON in decision.reason


def test_a_confident_no_entry_still_trades(no_live_io):
    """The gate must refuse the diffident, not everything on the NO side."""
    decision = entry_manager.evaluate_entry(_ev(model_prob=0.80), token_id="tok", min_net_ev=-9.0)

    assert REASON not in decision.reason


def test_a_low_confidence_yes_entry_is_untouched(no_live_io):
    """YES at 0.20 is the lottery band -- a different, separately scored defect."""
    decision = entry_manager.evaluate_entry(
        _ev(side="YES", model_prob=0.20, price=0.05), token_id="tok", min_net_ev=-9.0,
    )

    assert REASON not in decision.reason


def test_the_gate_reads_the_raw_probability_not_the_calibrated_one(no_live_io):
    """
    A NO row claiming 0.80 whose map corrects it to 0.489 must still TRADE.
    The pooled map is a flat step across 0.55-0.65, so a calibrated reading
    would refuse on a constant rather than on the model's confidence.
    """
    decision = entry_manager.evaluate_entry(
        _ev(model_prob=0.80, calibrated=0.489, source=pc.STATION_TIER),
        token_id="tok", min_net_ev=-9.0,
    )

    assert REASON not in decision.reason


# ---------------------------------------------------------------------------
# Parity: the replay must refuse exactly what live refuses
# ---------------------------------------------------------------------------

def test_the_replay_refuses_the_same_entry_with_the_same_reason(no_live_io):
    """
    backtest/entry_sim.py is a pure replica. If the two drift, a sweep scores a
    rule production never ran -- the failure that made the exit sweeps inert.
    """
    ev = _ev(model_prob=0.55)
    live = entry_manager.evaluate_entry(ev, token_id="tok", min_net_ev=-9.0)
    sim = entry_sim.evaluate_entry_sim(
        ev, token_id="tok", open_count_for_bucket=0, opposite_count_for_bucket=0,
        stop_outs_for_bucket=0, depth_usd=100_000.0, slippage_fn=lambda s: 0.0,
        min_net_ev=-9.0, sizing_bankroll=1_000.0,
    )

    assert live.approved is False
    assert sim.approved is False
    assert REASON in live.reason
    assert live.reason == sim.reason


def test_the_replay_counts_the_new_gate():
    """GATE_COUNT is the tripwire for a gate added to one side only."""
    assert entry_sim.GATE_COUNT == 17
