"""
Gap 6 of the 2026-09-24 plan gap audit: automatic brakes on the LIVE entry path.

Each brake refuses the live order (paper is untouched), logs why, and alerts
at most once per reason per UTC day. A brake check that errors fails CLOSED.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

import alerts
import cohort_monitor
import config
import executor
import storage
from clients import wallet_client
from models import EntryDecision, Position

TODAY = datetime.now(timezone.utc).date()


def _live_pos(pid, target_date=TODAY, status="open", entry=0.40, exit_price=None,
              exit_time=None, size_usd=2.0, mode="live"):
    return Position(
        position_id=pid, station_icao="WSSS", target_date=target_date, bucket_c=32,
        side="YES", entry_price=entry, size_usd=size_usd,
        entry_time="2026-09-20T00:00:00+00:00", status=status, token_id="TOK",
        is_paper=(mode != "live"), size_shares=size_usd / entry, execution_mode=mode,
        exit_price=exit_price, exit_time=exit_time,
    )


@pytest.fixture
def book(monkeypatch):
    """Stub the three stores the brakes read; tests fill them in."""
    state = {"open": [], "closed": [], "kill": None}
    monkeypatch.setattr(storage, "load_open_positions",
                        lambda station_icao=None, is_paper=None: list(state["open"]))
    monkeypatch.setattr(storage, "load_position_history",
                        lambda icao, limit=100, is_paper=None:
                            [p for p in state["closed"] if p.station_icao == icao])
    monkeypatch.setattr(cohort_monitor, "load_cohort", lambda **kw: ([], {}))
    monkeypatch.setattr(cohort_monitor, "kill_criterion", lambda ws: {"fired": state["kill"]})
    sent = []
    monkeypatch.setattr(alerts, "send", lambda *a, **kw: sent.append(a) or True)
    state["sent"] = sent
    return state


def _recent(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# --------------------------------------------------------------------------
# Each brake, tripping and not
# --------------------------------------------------------------------------

def test_nothing_trips_on_a_clean_book(book):
    assert executor._live_brake() is None


def test_daily_loss_trips_at_the_limit(book, monkeypatch):
    monkeypatch.setattr(config, "LIVE_DAILY_LOSS_LIMIT_USD", 4.0)
    # $5 staked at 0.50, resolved to 0 -> -$5.00 realised, inside 24h.
    book["closed"] = [_live_pos("a", status="closed_resolution", entry=0.50, exit_price=0.0,
                                exit_time=_recent(3), size_usd=5.0)]
    code, why = executor._live_brake()
    assert code == "daily_loss"
    assert "-5.00" in why


def test_daily_loss_ignores_old_and_paper_losses(book, monkeypatch):
    monkeypatch.setattr(config, "LIVE_DAILY_LOSS_LIMIT_USD", 4.0)
    book["closed"] = [
        _live_pos("old", status="closed_resolution", entry=0.5, exit_price=0.0,
                  exit_time=_recent(30), size_usd=5.0),
        _live_pos("paper", status="closed_resolution", entry=0.5, exit_price=0.0,
                  exit_time=_recent(1), size_usd=5.0, mode="paper"),
        _live_pos("small", status="closed_resolution", entry=0.5, exit_price=0.0,
                  exit_time=_recent(1), size_usd=3.0),
    ]
    assert executor._live_brake() is None


def test_kill_criterion_fired_trips(book):
    book["kill"] = True
    assert executor._live_brake()[0] == "kill_criterion"


@pytest.mark.parametrize("fired", [False, None])
def test_kill_criterion_holding_or_no_verdict_does_not_trip(book, fired):
    book["kill"] = fired
    assert executor._live_brake() is None


def test_stranded_live_position_trips(book):
    book["open"] = [_live_pos("s", target_date=TODAY - timedelta(days=3))]
    assert executor._live_brake()[0] == "stranded"


def test_a_position_two_days_past_is_not_yet_stranded(book):
    book["open"] = [_live_pos("s", target_date=TODAY - timedelta(days=2)),
                    _live_pos("p", target_date=TODAY - timedelta(days=9), mode="paper")]
    assert executor._live_brake() is None


def test_a_brake_that_errors_fails_closed(book, monkeypatch):
    def boom(**kw):
        raise RuntimeError("db locked")
    monkeypatch.setattr(storage, "load_open_positions", boom)
    code, why = executor._live_brake()
    assert code == "error"
    assert "db locked" in why


# --------------------------------------------------------------------------
# On the order path
# --------------------------------------------------------------------------

def _decision():
    return EntryDecision(
        station_icao="WSSS", target_date=TODAY, bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1, recommended_size_usd=1.0,
        available_depth_usd=1000.0, slippage_at_size_pct=0.01, net_ev_at_size=0.30,
        approved=True, reason="test", station_maturity="mature", entry_price=0.30,
        token_id="TOK",
    )


@pytest.fixture
def live_path(monkeypatch, book):
    """Everything up to the brake passes; submission is recorded, never sent."""
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    spec = wallet_client.OrderSpec(ok=True, token_id="TOK", side="BUY", limit_price=0.30,
                                   expected_price=0.30, size_shares=5.0, notional_usd=1.5)
    monkeypatch.setattr(wallet_client, "build_entry_order", lambda **kw: spec)
    monkeypatch.setattr(executor, "_price_drift_ok", lambda a, b: (True, ""))
    monkeypatch.setattr(executor, "_resolved_size_ok", lambda s, d, out=None: (True, ""))
    monkeypatch.setattr(executor, "_live_budget_breach", lambda *a, **kw: None)
    refusals, submitted = [], []
    monkeypatch.setattr(executor, "_record_refusal",
                        lambda decision, code, message, **kw: refusals.append(code))
    monkeypatch.setattr(wallet_client, "submit_order",
                        lambda spec, live=False: submitted.append(spec) or
                        wallet_client.OrderResult(submitted=True, filled=False, simulated=False,
                                                  spec=spec, error="stub"))
    monkeypatch.setattr(executor, "_record_attempt", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_brake_alerted", set())
    return refusals, submitted


def test_a_tripped_brake_refuses_the_live_order_and_alerts_once(live_path, book):
    refusals, submitted = live_path
    book["kill"] = True

    executor.open_position(_decision())
    executor.open_position(_decision())

    assert submitted == []
    assert refusals == ["brake_kill_criterion", "brake_kill_criterion"]
    assert len(book["sent"]) == 1, "one alert per reason per UTC day"


def test_an_untripped_brake_lets_the_order_through(live_path, book):
    refusals, submitted = live_path
    executor.open_position(_decision())
    assert refusals == []
    assert len(submitted) == 1


def test_paper_never_consults_the_brakes(monkeypatch, book):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "paper"})
    monkeypatch.setattr(executor, "_live_brake", lambda: pytest.fail("paper consulted a brake"))
    opened = []
    monkeypatch.setattr(storage, "open_position", lambda p: opened.append(p))
    executor.open_position(_decision())
    assert len(opened) == 1


# --------------------------------------------------------------------------
# #16 KILL-SWITCH DRILL (plan section 12): every brake, end to end -- the live
# entry is refused, an exit on the same station still goes out, and the
# operator hears about it exactly once however many entries it refuses.
# --------------------------------------------------------------------------

def _trip(code, book, monkeypatch):
    if code == "daily_loss":
        monkeypatch.setattr(config, "LIVE_DAILY_LOSS_LIMIT_USD", 4.0)
        book["closed"] = [_live_pos("L", status="closed_resolution", entry=0.50, exit_price=0.0,
                                    exit_time=_recent(2), size_usd=5.0)]
    elif code == "kill_criterion":
        book["kill"] = True
    elif code == "stranded":
        book["open"] = [_live_pos("S", target_date=TODAY - timedelta(days=5))]
    elif code == "error":
        def boom(**kw):
            raise RuntimeError("db locked")
        monkeypatch.setattr(storage, "load_open_positions", boom)


@pytest.mark.parametrize("code", ["daily_loss", "kill_criterion", "stranded", "error"])
def test_kill_switch_drill(code, live_path, book, monkeypatch):
    from models import ExitDecision

    refusals, submitted = live_path
    monkeypatch.setattr(executor, "_kill_cache", {})
    _trip(code, book, monkeypatch)

    for _ in range(3):
        executor.open_position(_decision())
    assert submitted == [], f"{code}: a braked live entry reached the exchange"
    assert refusals == [f"brake_{code}"] * 3
    assert [a[0] for a in book["sent"]] == [f"LIVE brake: {code}"], "alert exactly once"

    # The exit still goes out.
    sell = wallet_client.OrderSpec(ok=True, token_id="TOK", side="SELL", limit_price=0.20,
                                   expected_price=0.20, size_shares=5.0, notional_usd=1.0)
    monkeypatch.setattr(wallet_client, "build_exit_order", lambda **kw: sell)
    monkeypatch.setattr(wallet_client, "submit_order",
                        lambda spec, live=False: submitted.append(spec) or
                        wallet_client.OrderResult(submitted=True, filled=True, simulated=False,
                                                  spec=spec, fill_price=0.20, fill_shares=5.0,
                                                  order_id="X"))
    closed = []
    monkeypatch.setattr(storage, "close_position", lambda **kw: closed.append(kw) or True)
    executor.close_position(_live_pos("E"), ExitDecision(
        position_id="E", should_exit=True, reason="stop_loss", current_price=0.20, pnl_pct=-0.5))
    assert [s.side for s in submitted] == ["SELL"]
    assert closed and closed[0]["status"] == "closed_stop_loss"
