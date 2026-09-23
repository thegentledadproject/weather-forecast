"""
Wave 1 (spec 1c): every executor-level refusal that used to be only a print
is one live_order_attempts row with outcome='refused' and detail
'<code>: <message>'. Journal lines are unchanged. The daily order cap
counts SUBMISSIONS and must not count these.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from clients import market_client, wallet_client
from models import EntryDecision, Position

REGION = config.region_of("WSSS")


def _decision(price=0.30, token_id="TOK", net_ev=0.30, slip=0.01):
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 8, 10), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=1.0, available_depth_usd=1000.0,
        slippage_at_size_pct=slip, net_ev_at_size=net_ev,
        approved=True, reason="test", station_maturity="mature",
        entry_price=price, token_id=token_id,
    )


def _live_position(size_usd=1.0):
    return Position(
        position_id="p", station_icao="WSSS", target_date=date(2026, 8, 10),
        bucket_c=33, side="NO", entry_price=0.30, size_usd=size_usd,
        entry_time="2026-08-10T00:00:00+00:00", status="open", token_id="T2",
        is_paper=False, size_shares=3.33, execution_mode="live",
    )


def _explode(*a, **kw):
    raise AssertionError("submit_order must not be reached by a refused entry")


def _no_position(p):
    raise AssertionError("a refused entry must not write a position")


def _wsss_only(positions):
    """
    load_open_positions stub that hands `positions` to the region backstop
    (no station filter) and to WSSS's own day-budget read, and nothing to any
    other station -- portfolio_day_exposure_usd sums every station in the
    region, and an unfiltered stub would multiply the fixture 15x.
    """
    def _load(**kw):
        return list(positions) if kw.get("station_icao") in (None, "WSSS") else []
    return _load


@pytest.fixture
def live(monkeypatch):
    rows = []
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    monkeypatch.setattr(storage, "open_position", _no_position)
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
    monkeypatch.setattr(storage, "record_live_order_attempt", lambda **kw: rows.append(kw))
    monkeypatch.setattr(storage, "count_live_order_attempts", lambda kind, since, station_icaos=None: 0)
    monkeypatch.setattr(wallet_client, "_book_constraints", lambda token_id: ("0.01", None))
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions, **_: wallet_client.Reconciliation(ok=True, checked=True, reason="stubbed"),
    )
    monkeypatch.setattr(wallet_client, "submit_order", _explode)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    return rows


def _setup_no_entry_price(mp):
    return _decision(price=None)


def _setup_no_token_id(mp):
    return _decision(token_id="")


def _setup_order_not_placeable(mp):
    mp.setattr(wallet_client, "build_entry_order", lambda **kw: wallet_client.OrderSpec(
        ok=False, token_id="TOK", side="BUY", limit_price=0.0, size_shares=0.0,
        notional_usd=0.0, reason="stubbed refusal"))
    return _decision()


def _setup_drift(mp):
    mp.setattr(wallet_client, "_book_constraints", lambda token_id: ("0.1", None))
    return _decision(price=0.31)


def _setup_resolved_depth_unreadable(mp):
    mp.setattr(market_client, "get_available_depth_usd", lambda t: None)
    return _decision()


def _setup_resolved_depth(mp):
    mp.setattr(market_client, "get_available_depth_usd", lambda t: 2.0)   # 25% of 2 = $0.50 < $1
    return _decision()


def _setup_resolved_slippage_unreadable(mp):
    def _raise(t, s):
        raise RuntimeError("book down")
    mp.setattr(market_client, "estimate_slippage", _raise)
    return _decision()


def _setup_resolved_slippage(mp):
    mp.setattr(market_client, "estimate_slippage", lambda t, s: config.MAX_ACCEPTABLE_SLIPPAGE_PCT + 0.01)
    return _decision()


def _setup_resolved_net_ev(mp):
    mp.setattr(market_client, "estimate_slippage", lambda t, s: 0.05)
    return _decision(net_ev=0.02, slip=0.01)


def _setup_day_budget(mp):
    mp.setattr(entry_manager, "station_day_exposure_usd",
               lambda *a, **kw: config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD)
    return _decision()


def _setup_recon(mp):
    mp.setattr(wallet_client, "reconcile_cached",
               lambda positions, **_: wallet_client.Reconciliation(ok=False, checked=True, reason="db_only"))
    return _decision()


def _setup_region_concurrent(mp):
    n = config.REGION_LIVE_MAX_CONCURRENT_POSITIONS[REGION]   # 5 x $1 stays under the $8 exposure cap
    mp.setattr(storage, "load_open_positions", _wsss_only([_live_position() for _ in range(n)]))
    return _decision()


def _setup_region_exposure(mp):
    cap = config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD[REGION]   # one $8 position + $1 breaches; count 1 < 5
    mp.setattr(storage, "load_open_positions", _wsss_only([_live_position(size_usd=cap)]))
    return _decision()


def _setup_orders_per_day_unreadable(mp):
    mp.setattr(storage, "count_live_order_attempts", lambda kind, since, station_icaos=None: None)
    return _decision()


def _setup_orders_per_day(mp):
    mp.setattr(storage, "count_live_order_attempts",
               lambda kind, since, station_icaos=None: config.REGION_LIVE_MAX_ORDERS_PER_DAY[REGION])
    return _decision()


SITES = [
    ("no_entry_price", _setup_no_entry_price),
    ("no_token_id", _setup_no_token_id),
    ("order_not_placeable", _setup_order_not_placeable),
    ("drift", _setup_drift),
    ("resolved_depth_unreadable", _setup_resolved_depth_unreadable),
    ("resolved_depth", _setup_resolved_depth),
    ("resolved_slippage_unreadable", _setup_resolved_slippage_unreadable),
    ("resolved_slippage", _setup_resolved_slippage),
    ("resolved_net_ev", _setup_resolved_net_ev),
    ("day_budget", _setup_day_budget),
    ("recon", _setup_recon),
    ("region_concurrent", _setup_region_concurrent),
    ("region_exposure", _setup_region_exposure),
    ("orders_per_day_unreadable", _setup_orders_per_day_unreadable),
    ("orders_per_day", _setup_orders_per_day),
]


@pytest.mark.parametrize("code,setup", SITES, ids=[s[0] for s in SITES])
def test_each_refusal_site_records_exactly_one_refused_row(live, monkeypatch, code, setup):
    decision = setup(monkeypatch)

    executor.open_position(decision)

    assert len(live) == 1, f"{code}: expected one row, got {live}"
    row = live[0]
    assert row["kind"] == "entry" and row["outcome"] == "refused"
    assert row["station_icao"] == "WSSS" and row["bucket_c"] == 32 and row["side"] == "YES"
    assert row["detail"].startswith(f"{code}: "), row["detail"]
    assert row["order_id"] is None


def test_an_unapproved_decision_records_nothing(live):
    d = _decision()
    d.approved = False
    executor.open_position(d)
    assert live == []


def test_paper_refusals_are_not_written_to_the_live_audit(live, monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    executor.open_position(_decision(price=None))
    assert live == []


def test_a_recording_failure_never_raises(live, monkeypatch, capsys):
    def _boom(**kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(storage, "record_live_order_attempt", _boom)
    executor.open_position(_decision(price=None))
    assert "could not record the refusal" in capsys.readouterr().out


def test_refused_rows_do_not_count_toward_the_daily_order_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    storage.migrate()
    storage.record_live_order_attempt(kind="entry", station_icao="WSSS", outcome="filled", detail="ok")
    storage.record_live_order_attempt(kind="entry", station_icao="WSSS", outcome="killed", detail="fok")
    storage.record_live_order_attempt(kind="entry", station_icao="WSSS", outcome="refused", detail="drift: x")

    assert storage.count_live_order_attempts("entry", "2000-01-01") == 2
    assert storage.count_live_order_attempts("entry", "2000-01-01", station_icaos=["WSSS"]) == 2
    assert len(storage.load_live_order_attempts()) == 3
