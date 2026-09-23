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
def db(tmp_db):
    return tmp_db


@pytest.fixture
def live(monkeypatch, db):
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


def test_resolved_size_ok_reports_the_figure_through_out(db, monkeypatch):
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


def test_resolved_size_ok_still_works_without_out(db, monkeypatch):
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.02)
    spec = wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=0.30, size_shares=5.0,
        notional_usd=1.5, expected_price=0.30,
    )
    ok, note = executor._resolved_size_ok(spec, _decision())
    assert ok and "net EV" in note
