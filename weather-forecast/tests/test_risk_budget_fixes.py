"""
tests/test_risk_budget_fixes.py

Regression tests for the two live defects surfaced by the backtest build
and fixed on 2026-08-02:

  1. DEAD PROFIT-TAKE: risk_manager.evaluate_exit()'s fixed profit-take
     sat below an early "trailing is active, hold" return. Since
     update_high_water_mark() guarantees hwm >= price, any pnl at the
     take level had always activated trailing first -- a reachability
     sweep found ZERO states that could produce "take_profit". The fix
     checks the fixed take before the trailing-active hold (but still
     after a genuine trailing-stop breach).

  2. PER-CYCLE BUDGET CAP: entry_manager.apply_portfolio_budget() was
     blind to earlier cycles' entries, so the "per day" exposure budget
     reset every cycle (~18x/day). It now deducts existing_exposure_usd,
     which decide_portfolio_entries() reads from storage via
     station_day_exposure_usd() (open AND same-day closed positions --
     a stopped-out leg still spent budget) and the backtest engine feeds
     from PortfolioState. Unknown spend fails closed, like the
     per-bucket cap.
"""

from datetime import date

import config
import entry_manager
import risk_manager
import storage
from models import EntryDecision, Position


def _pos(entry_price: float, hwm: float) -> Position:
    return Position(
        position_id="t:pos",
        station_icao="WSSS",
        target_date=date(2026, 8, 2),
        bucket_c=32,
        side="YES",
        entry_price=entry_price,
        size_usd=50.0,
        entry_time="2026-08-02T00:00:00+00:00",
        high_water_mark=hwm,
        is_paper=True,
    )


LOOSE_HOUR = 6  # before EDGE_DECAY_TIGHTEN_HOUR_LOCAL: PT 0.50 / act 0.25 / trail 0.15


class TestProfitTakeReachable:
    def test_straight_runup_to_take_level_exits_take_profit(self):
        # hwm == price (monotone run-up, no pullback): pnl +53% >= 50%.
        # Old code: trailing active -> early hold return, take unreachable.
        decision = risk_manager.evaluate_exit(_pos(0.30, 0.46), 0.46, local_hour=LOOSE_HOUR)
        assert decision.should_exit is True
        assert decision.reason == "take_profit"

    def test_trailing_breach_still_wins_over_take(self):
        # Peak +100%, price pulled back 16.7% from peak but still +67% --
        # both rules trip; the breach must be filed as trailing_stop.
        decision = risk_manager.evaluate_exit(_pos(0.30, 0.60), 0.50, local_hour=LOOSE_HOUR)
        assert decision.should_exit is True
        assert decision.reason == "trailing_stop"

    def test_trailing_active_below_take_still_holds(self):
        # +33%: trailing armed (>=25%), below the 50% take, no pullback.
        decision = risk_manager.evaluate_exit(_pos(0.30, 0.40), 0.40, local_hour=LOOSE_HOUR)
        assert decision.should_exit is False
        assert decision.reason == "trailing_active"

    def test_take_profit_reachable_under_tightened_thresholds(self):
        # After the tighten hour PT drops to 0.25: +27.5% straight run-up.
        hour = config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL
        decision = risk_manager.evaluate_exit(_pos(0.40, 0.51), 0.51, local_hour=hour)
        assert decision.should_exit is True
        assert decision.reason == "take_profit"

    def test_reachability_sweep_finds_take_profit_states(self):
        # The original finding: a full sweep of hwm >= price states found
        # zero take_profit outcomes. Guard against the branch going dead
        # again under any future threshold reshuffle.
        reasons = set()
        for hour in (LOOSE_HOUR, config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL):
            for price_c in range(1, 100):
                price = price_c / 100
                for hwm_c in range(price_c, 100):
                    decision = risk_manager.evaluate_exit(
                        _pos(0.30, hwm_c / 100), price, local_hour=hour
                    )
                    reasons.add(decision.reason)
        assert "take_profit" in reasons


def _decision(size: float, approved: bool = True) -> EntryDecision:
    return EntryDecision(
        station_icao="WSSS",
        target_date=date(2026, 8, 2),
        bucket_c=32,
        side="YES",
        kelly_fraction_raw=0.1,
        kelly_fraction_applied=0.025,
        recommended_size_usd=size,
        available_depth_usd=1000.0,
        slippage_at_size_pct=0.01,
        net_ev_at_size=0.2,
        approved=approved,
        reason="approved",
        station_maturity="mature",
        entry_price=0.30,
        token_id="tok",
    )


class TestPortfolioBudgetDeductsExistingExposure:
    def test_zero_existing_keeps_old_behavior(self):
        decisions = entry_manager.apply_portfolio_budget(
            [_decision(100.0), _decision(100.0)], max_total_usd=250.0
        )
        assert [d.recommended_size_usd for d in decisions] == [100.0, 100.0]

    def test_existing_exposure_shrinks_remaining_budget(self):
        decisions = entry_manager.apply_portfolio_budget(
            [_decision(100.0), _decision(100.0)],
            max_total_usd=250.0,
            existing_exposure_usd=100.0,
        )
        # remaining 150 across 200 requested -> 75% scale
        assert [d.recommended_size_usd for d in decisions] == [75.0, 75.0]
        assert all(d.approved for d in decisions)

    def test_exhausted_budget_rejects_all_legs(self):
        decisions = entry_manager.apply_portfolio_budget(
            [_decision(100.0), _decision(50.0, approved=False)],
            max_total_usd=250.0,
            existing_exposure_usd=250.0,
        )
        assert decisions[0].approved is False
        assert decisions[0].recommended_size_usd == 0.0
        assert "budget exhausted" in decisions[0].reason
        # already-rejected legs pass through untouched
        assert decisions[1].reason == "approved"

    def test_station_day_exposure_counts_open_and_same_day_closed(self, monkeypatch):
        day = date(2026, 8, 2)
        open_pos = [_pos(0.30, 0.30)]                      # $50, target 2026-08-02
        closed_same_day = _pos(0.30, 0.30)
        closed_same_day.size_usd = 70.0
        closed_other_day = _pos(0.30, 0.30)
        closed_other_day.target_date = date(2026, 8, 1)    # must NOT count
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: list(open_pos))
        monkeypatch.setattr(
            storage, "load_position_history",
            lambda station_icao, limit=1000, is_paper=None: [closed_same_day, closed_other_day],
        )
        assert entry_manager.station_day_exposure_usd("WSSS", day, is_paper=True) == 120.0

    def test_station_day_exposure_fails_closed_on_storage_error(self, monkeypatch):
        def _boom(**kw):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(storage, "load_open_positions", _boom)
        assert entry_manager.station_day_exposure_usd("WSSS", date(2026, 8, 2), is_paper=True) is None
