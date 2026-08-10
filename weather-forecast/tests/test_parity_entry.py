"""
Field-by-field parity between the live entry_manager.evaluate_entry() and
its pure replica, backtest.entry_sim.evaluate_entry_sim().

Both are fed the same EVResult and the same injected/stubbed I/O
(storage.load_open_positions, storage.load_position_history,
market_client.get_available_depth_usd, market_client.estimate_slippage)
and must produce byte-for-byte identical EntryDecision objects across
every gate 1-12 plus the exploratory-sizing approve path. A field-level
mismatch here means the replica has drifted from the live gate logic.

Adapted from the field-parity harness in scratchpad/smoke_parity.py
(section B) -- restructured into a parametrized pytest test using the
monkeypatch fixture instead of manual save/restore of module attributes.
"""

from dataclasses import asdict
from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from clients import market_client
from models import EVResult, Position
from backtest import entry_sim


def make_ev(model_prob, price, station="WSSS", bucket=32, side="YES", fee=0.0,
            spread_source="ensemble"):
    raw_edge = None if price is None else model_prob - price
    return EVResult(
        station_icao=station, target_date=date(2026, 8, 10), bucket_c=bucket, side=side,
        model_prob=model_prob, market_price=price, raw_edge=raw_edge,
        estimated_slippage_pct=0.01, fee_rate_pct=fee,
        net_ev_per_dollar=None if price in (None, 0) else raw_edge / price - 0.01 - fee,
        spread_source=spread_source,
    )


# (label, ev, open_count_for_bucket, stop_outs_for_bucket, depth_usd, slippage, min_net_ev)
# open_count_for_bucket=None replicates "open positions unreadable" (gate 3);
# stop_outs_for_bucket=None replicates "position history unreadable" (gate 5).
CASES = [
    ("gate1_raw_edge_veto",         make_ev(0.95, 0.30), 0,    0,    1000.0, 0.01, 0.15),
    ("gate2_materiality_floor",     make_ev(0.37, 0.35), 0,    0,    1000.0, 0.01, 0.15),
    ("gate2_low_conf_doubled_bar",  make_ev(0.395, 0.35, spread_source="fallback_default"),
                                                         0,    0,    1000.0, 0.01, 0.15),
    ("gate3_unreadable_count",      make_ev(0.55, 0.35), None, 0,    1000.0, 0.01, 0.15),
    ("gate4_per_bucket_cap",        make_ev(0.55, 0.35), 1,    0,    1000.0, 0.01, 0.15),
    ("gate5_unreadable_history",    make_ev(0.55, 0.35), 0,    None, 1000.0, 0.01, 0.15),
    ("gate6_stop_out_cooldown",     make_ev(0.55, 0.35), 0,    1,    1000.0, 0.01, 0.15),
    ("gate7_kelly_le_zero",         make_ev(0.20, 0.35), 0,    0,    1000.0, 0.01, 0.15),
    ("gate8_depth_unknown",         make_ev(0.55, 0.35), 0,    0,    None,   0.01, 0.15),
    ("gate9_depth_too_thin",        make_ev(0.55, 0.35), 0,    0,    2.0,    0.01, 0.15),
    ("gate10_slippage_gate",        make_ev(0.55, 0.35), 0,    0,    1000.0, 0.40, 0.15),
    ("gate11_net_ev_at_size",       make_ev(0.55, 0.35), 0,    0,    1000.0, 0.01, 0.90),
    ("gate12_approved",             make_ev(0.55, 0.35), 0,    0,    1000.0, 0.01, 0.15),
    ("gate12_approved_exploratory", make_ev(0.55, 0.35, station="WMKK"), 0, 0, 1000.0, 0.01, 0.15),
]


@pytest.mark.parametrize(
    "ev,open_count,stop_outs,depth,slip,min_net_ev",
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_entry_decision_field_parity(monkeypatch, ev, open_count, stop_outs, depth, slip, min_net_ev):
    # Pin every station to paper for the duration of this test.
    #
    # The parity contract is about the GATE CHAIN -- raw edge, materiality,
    # per-bucket cap, cooldown, Kelly, depth, slippage, net EV -- not about
    # execution. entry_manager grew one sizing input the replica has no
    # concept of: config.LIVE_TRADE_SIZE_USD, which replaces Kelly sizing
    # with a flat $1 for stations running in simulation/live mode. WSSS
    # defaults to "simulation", so without this pin the live path sizes the
    # WSSS cases at $1 while the replica sizes them at Kelly and the parity
    # test fails on a difference that is correct in both directions.
    #
    # entry_sim deliberately does NOT model the cap: a backtest of $1 trades
    # measures nothing about strategy edge, which is the only thing a
    # backtest is for. The cap's own behaviour is pinned separately in
    # test_live_execution.py.
    monkeypatch.setattr(
        executor, "EXECUTION_MODE", {icao: "paper" for icao in config.STATIONS},
    )

    # Stub live I/O so evaluate_entry sees exactly what the sim is handed.
    if open_count is None:
        def _unreadable(**kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(storage, "load_open_positions", _unreadable)
    else:
        fake_positions = [
            Position(
                position_id=f"x{i}", station_icao=ev.station_icao, target_date=ev.target_date,
                bucket_c=ev.bucket_c, side=ev.side, entry_price=0.3, size_usd=10.0,
                entry_time="2026-08-10T00:00:00+00:00",
                is_paper=(executor.EXECUTION_MODE.get(ev.station_icao, "manual_review") == "paper"),
            )
            for i in range(open_count)
        ]
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: list(fake_positions))

    if stop_outs is None:
        def _history_unreadable(*a, **kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(storage, "load_position_history", _history_unreadable)
    else:
        fake_history = [
            Position(
                position_id=f"h{i}", station_icao=ev.station_icao, target_date=ev.target_date,
                bucket_c=ev.bucket_c, side=ev.side, entry_price=0.3, size_usd=10.0,
                entry_time="2026-08-10T00:00:00+00:00",
                is_paper=(executor.EXECUTION_MODE.get(ev.station_icao, "manual_review") == "paper"),
                status="closed_stop_loss",
            )
            for i in range(stop_outs)
        ]
        monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: list(fake_history))

    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: depth)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: slip)

    live = entry_manager.evaluate_entry(ev, "TOKEN-1", min_net_ev=min_net_ev)
    sim = entry_sim.evaluate_entry_sim(
        ev=ev, token_id="TOKEN-1", open_count_for_bucket=open_count,
        stop_outs_for_bucket=stop_outs, depth_usd=depth,
        slippage_fn=lambda size_usd: slip, min_net_ev=min_net_ev,
        sizing_bankroll=config.BANKROLL_USD,
    )

    live_dict = asdict(live)
    sim_dict = asdict(sim)
    diffs = {k: (v, sim_dict[k]) for k, v in live_dict.items() if sim_dict[k] != v}
    assert not diffs, f"EntryDecision field mismatch: {diffs}"
