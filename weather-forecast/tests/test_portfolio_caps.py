"""
tests/test_portfolio_caps.py

Regression tests for the 2026-08-05 station-expansion risk additions
(design doc F1/F2), mirrored between live and the backtest replay:

  F1. Portfolio-wide daily cap (config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD)
      alongside the existing per-station cap in
      entry_manager.apply_portfolio_budget() -- whichever binds tighter
      wins, and an already-exhausted portfolio budget rejects approvals
      with a reason naming which cap did it.

  F2. Collection-first gate (config.MIN_RESOLUTION_OBS_BEFORE_ENTRY): a
      station without enough settlement-grade observations gets ONLY
      collection-only decisions, whatever the EV table says. Mirrored in
      backtest/entry_sim.decide_portfolio_entries_sim() so a replay and
      live agree; parity is checked field-by-field like
      tests/test_parity_entry.py does for the per-candidate gates.
"""

from datetime import date

import config
import entry_manager
import storage
from clients import market_client
from models import EntryDecision, EVResult
from backtest import entry_sim


def _decision(size: float, approved: bool = True, station="WSSS") -> EntryDecision:
    return EntryDecision(
        station_icao=station,
        target_date=date(2026, 8, 6),
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


class TestTighterCapBinds:
    def test_station_cap_binds_when_tighter(self):
        # station remaining = 250 - 200 = 50; portfolio remaining = 400 - 300 = 100.
        # Station is tighter -- scale = 50/100 = 0.5.
        decisions = entry_manager.apply_portfolio_budget(
            [_decision(100.0)],
            max_total_usd=250.0, existing_exposure_usd=200.0,
            max_portfolio_usd=400.0, portfolio_exposure_usd=300.0,
        )
        assert decisions[0].recommended_size_usd == 50.0
        assert decisions[0].approved is True

    def test_portfolio_cap_binds_when_tighter(self):
        # station remaining = 250 - 50 = 200; portfolio remaining = 400 - 380 = 20.
        # Portfolio is tighter -- scale = 20/100 = 0.2.
        decisions = entry_manager.apply_portfolio_budget(
            [_decision(100.0)],
            max_total_usd=250.0, existing_exposure_usd=50.0,
            max_portfolio_usd=400.0, portfolio_exposure_usd=380.0,
        )
        assert decisions[0].recommended_size_usd == 20.0
        assert decisions[0].approved is True

    def test_defaults_pull_from_config(self):
        # No explicit max_total_usd/max_portfolio_usd -- must read
        # config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD and
        # config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD.
        decisions = entry_manager.apply_portfolio_budget([_decision(1.0)])
        assert decisions[0].recommended_size_usd == 1.0
        assert decisions[0].approved is True


class TestPortfolioBudgetExhaustionNamesTheBindingCap:
    def test_portfolio_exposure_near_cap_blocks_with_portfolio_day_reason(self):
        # Portfolio already fully deployed for the day; station has ample
        # room left. The portfolio cap must be the one that blocks, and
        # the rejection reason must name it (not the station cap).
        decisions = entry_manager.apply_portfolio_budget(
            [_decision(50.0)],
            max_total_usd=250.0, existing_exposure_usd=0.0,
            max_portfolio_usd=config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD,
            portfolio_exposure_usd=config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD,
        )
        assert decisions[0].approved is False
        assert decisions[0].recommended_size_usd == 0.0
        assert "portfolio/day" in decisions[0].reason
        assert "budget exhausted" in decisions[0].reason

    def test_station_exposure_near_cap_blocks_with_station_day_reason(self):
        # Mirror case: station fully deployed, portfolio has ample room.
        # The station cap must be the one named.
        decisions = entry_manager.apply_portfolio_budget(
            [_decision(50.0)],
            max_total_usd=250.0, existing_exposure_usd=250.0,
            max_portfolio_usd=config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD,
            portfolio_exposure_usd=0.0,
        )
        assert decisions[0].approved is False
        assert "station/day" in decisions[0].reason
        assert "budget exhausted" in decisions[0].reason


def _make_ev(station="WSSS", bucket=32, side="YES") -> EVResult:
    return EVResult(
        station_icao=station, target_date=date(2026, 8, 6), bucket_c=bucket, side=side,
        model_prob=0.55, market_price=0.35, raw_edge=0.20,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02,
        net_ev_per_dollar=0.30, spread_source="ensemble",
    )


class TestCollectionFirstGate:
    """
    A station below config.MIN_RESOLUTION_OBS_BEFORE_ENTRY settlement-grade
    observations gets NO entries at all -- station-level, before any
    per-candidate sizing gate runs.
    """

    def test_live_decide_portfolio_entries_yields_only_collection_only(self, monkeypatch):
        ev_results = [_make_ev(bucket=32), _make_ev(bucket=33)]
        token_map = {
            32: {"yes_token_id": "y32", "no_token_id": "n32"},
            33: {"yes_token_id": "y33", "no_token_id": "n33"},
        }
        # Fewer than MIN_RESOLUTION_OBS_BEFORE_ENTRY (5) settlement-grade
        # observations on record for this station.
        monkeypatch.setattr(storage, "count_observations_from_source", lambda icao, source: 2)

        decisions = entry_manager.decide_portfolio_entries(ev_results, token_map, min_net_ev=0.15)

        assert len(decisions) == len(ev_results)
        assert all(d.approved is False for d in decisions)
        assert all("Collection-only" in d.reason for d in decisions)
        assert all(d.recommended_size_usd == 0.0 for d in decisions)

    def test_live_decide_portfolio_entries_trades_normally_once_graduated(self, monkeypatch):
        # Sanity counterpart: at/above the threshold, the gate does not
        # fire and candidates reach normal per-leg evaluation (whatever
        # that evaluation decides is a separate concern from this gate).
        # Every other I/O the per-leg path touches is stubbed so this
        # exercises only the gate boundary, not the network or storage.
        ev_results = [_make_ev(bucket=32)]
        token_map = {32: {"yes_token_id": "y32", "no_token_id": "n32"}}
        monkeypatch.setattr(
            storage, "count_observations_from_source",
            lambda icao, source: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY,
        )
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
        monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
        monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
        monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)

        decisions = entry_manager.decide_portfolio_entries(ev_results, token_map, min_net_ev=0.15)
        assert not any("Collection-only" in d.reason for d in decisions)

    def test_entry_sim_mirrors_the_live_collection_gate(self):
        # entry_sim.decide_portfolio_entries_sim's collection gate is OFF
        # by default and reuses entry_manager.collection_only_reason() and
        # .collection_only_decision() verbatim -- the same objects live
        # imports, not a re-implementation -- so field parity here is a
        # parity check on how those shared functions are invoked, not on
        # two independently written gates.
        ev_results = [_make_ev(bucket=32), _make_ev(bucket=33)]
        candidates = [(ev, f"tok-{ev.bucket_c}") for ev in ev_results]

        sim_decisions = entry_sim.decide_portfolio_entries_sim(
            candidates,
            portfolio=None,
            fill_model=None,
            price_lookup=lambda token_id: None,
            min_net_ev=0.15,
            sizing_bankroll=config.BANKROLL_USD,
            resolution_obs_count=0,
            enforce_collection_gate=True,
        )

        assert len(sim_decisions) == len(ev_results)
        assert all(d.approved is False for d in sim_decisions)
        assert all("Collection-only" in d.reason for d in sim_decisions)

        # Field-by-field parity against the live collection_only_decision
        # shape for the same inputs (same reason text, since both come
        # from the identical shared collection_only_reason() call).
        expected_reason = entry_manager.collection_only_reason("WSSS", 0)
        for ev, token_id in candidates:
            expected = entry_manager.collection_only_decision(ev, token_id, expected_reason)
            actual = next(d for d in sim_decisions if d.bucket_c == ev.bucket_c)
            assert actual == expected

    def test_entry_sim_gate_off_by_default_does_not_short_circuit(self):
        # enforce_collection_gate defaults to False specifically so a
        # replay that doesn't reconstruct a point-in-time observation
        # count isn't silently forced into collection-only for every
        # candidate ever (see entry_sim's module docstring). With the
        # gate off, resolution_obs_count is irrelevant and the per-
        # candidate loop must actually run (it will reach price_lookup).
        ev_results = [_make_ev(bucket=32)]
        candidates = [(ev_results[0], "tok-32")]
        calls = []

        def _price_lookup(token_id):
            calls.append(token_id)
            return None

        class _NullFillModel:
            def depth_at(self, snapshot):
                return None

            def slippage(self, size_usd, depth_usd):
                return 0.0

        class _EmptyPortfolio:
            open = {}
            closed = []

        entry_sim.decide_portfolio_entries_sim(
            candidates,
            portfolio=_EmptyPortfolio(),
            fill_model=_NullFillModel(),
            price_lookup=_price_lookup,
            min_net_ev=0.15,
            sizing_bankroll=config.BANKROLL_USD,
            resolution_obs_count=0,
            enforce_collection_gate=False,
        )
        assert calls == ["tok-32"]
