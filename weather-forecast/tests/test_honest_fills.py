"""
tests/test_honest_fills.py

Honest fills (plan P6/P7, 2026-09-24 gap audit):

  1. an entry takes its ask, depth and slippage from ONE /book snapshot and
     refuses books that are crossed, empty on the ask side, stale, or whose
     ask has moved against the priced one.
"""
import time
from datetime import date

import pytest

import config
import entry_manager
from clients import market_client
from clients.market_client import get_entry_book as real_get_entry_book  # before conftest's shim
from models import EVResult


def _book(asks, bids=(), ts=None):
    book = {
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
    }
    if ts is not None:
        book["timestamp"] = str(int(ts * 1000))
    return book


# Trimmed from a real /book captured 2026-09-24 (KBKF 72F NO): asks come back
# best-LAST, bids best-last, prices and sizes as strings, ms timestamp.
REAL_SHAPE = {
    "market": "0x29be57d3", "asset_id": "114445568125",
    "timestamp": "1790265943981", "hash": "25d0fa90",
    "bids": [{"price": "0.01", "size": "3181.31"}, {"price": "0.68", "size": "20"},
             {"price": "0.69", "size": "5"}],
    "asks": [{"price": "0.99", "size": "167"}, {"price": "0.8", "size": "54.99"},
             {"price": "0.76", "size": "25"}, {"price": "0.75", "size": "5"}],
    "min_order_size": "5", "tick_size": "0.01", "neg_risk": True,
}


@pytest.fixture
def one_book(monkeypatch):
    """Patch get_order_book to serve `holder['book']` and count the fetches."""
    holder = {"book": None, "calls": 0}

    def _fetch(token_id, timeout=10):
        holder["calls"] += 1
        return holder["book"]

    monkeypatch.setattr(market_client, "get_order_book", _fetch)
    monkeypatch.setattr(market_client, "get_entry_book", real_get_entry_book)
    return holder


def test_real_shape_parses_to_best_ask_depth_and_walk(one_book):
    one_book["book"] = dict(REAL_SHAPE, timestamp=str(int(time.time() * 1000)))
    snap = market_client.get_entry_book("tok")
    assert snap.refusal is None
    assert snap.best_ask == pytest.approx(0.75)
    # within 10% of 0.75 (<= 0.825): 5@0.75 + 25@0.76 + 54.99@0.80
    assert snap.depth_usd() == pytest.approx(5 * 0.75 + 25 * 0.76 + 54.99 * 0.80)
    # $10 walks 5@0.75 ($3.75) then $6.25 at 0.76
    shares = 5 + 6.25 / 0.76
    assert snap.slippage(10.0) == pytest.approx((10.0 / shares - 0.75) / 0.75)
    assert one_book["calls"] == 1


def test_book_timestamp_is_read_in_seconds():
    assert market_client.book_timestamp(REAL_SHAPE) == pytest.approx(1790265943.981)
    assert market_client.book_timestamp({}) is None


@pytest.mark.parametrize("book,code", [
    (None, "no_book"),
    (_book(asks=[], bids=[(0.40, 10)]), "empty_ask"),
    (_book(asks=[(0.40, 10)], bids=[(0.40, 10)]), "crossed"),
    (_book(asks=[(0.40, 10)], bids=[(0.45, 10)]), "crossed"),
])
def test_unusable_books_are_refused_with_a_code(one_book, book, code):
    one_book["book"] = book
    snap = market_client.get_entry_book("tok")
    assert snap.refusal == code
    assert snap.depth_usd() is None


def test_a_book_older_than_the_threshold_is_stale(one_book):
    one_book["book"] = _book(asks=[(0.40, 100)], bids=[(0.38, 100)],
                             ts=time.time() - config.BOOK_MAX_AGE_S - 5)
    assert market_client.get_entry_book("tok").refusal == "stale"


def test_a_fresh_book_passes_and_no_timestamp_falls_back_to_fetch_time(one_book):
    one_book["book"] = _book(asks=[(0.40, 100)], bids=[(0.38, 100)], ts=time.time() - 60)
    assert market_client.get_entry_book("tok").refusal is None
    one_book["book"] = _book(asks=[(0.40, 100)], bids=[(0.38, 100)])  # no timestamp
    assert market_client.get_entry_book("tok").refusal is None
    # ...and the fetch-time fallback does age it
    assert market_client.book_quality_problem(
        _book(asks=[(0.40, 100)]), fetched_at=0.0, now=config.BOOK_MAX_AGE_S + 1
    ) == "stale"


def test_an_ask_worse_than_the_priced_one_is_refused_a_better_one_is_not(one_book):
    one_book["book"] = _book(asks=[(0.42, 100)], ts=time.time())
    assert market_client.get_entry_book("tok", decided_ask=0.40).refusal == "ask_moved"
    assert market_client.get_entry_book("tok", decided_ask=0.42).refusal is None
    assert market_client.get_entry_book("tok", decided_ask=0.45).refusal is None


def test_the_entry_book_fallback_matches_the_literal_the_backtest_reads():
    from backtest import fill_model
    assert market_client._FALLBACK_SLIPPAGE_PCT == fill_model.FALLBACK_SLIPPAGE_PCT


def _ev(price=0.30, model_prob=0.432):
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 8, 20), bucket_c=32, side="YES",
        model_prob=model_prob, market_price=price, raw_edge=model_prob - price,
        estimated_slippage_pct=0.0, fee_rate_pct=0.0,
        net_ev_per_dollar=(model_prob - price) / price,
    )


def _no_legacy_calls(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("entry path must not fetch a second book")
    monkeypatch.setattr(market_client, "get_available_depth_usd", _boom)
    monkeypatch.setattr(market_client, "estimate_slippage", _boom)


def test_evaluate_entry_sizes_off_one_snapshot(one_book, monkeypatch):
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)
    _no_legacy_calls(monkeypatch)
    one_book["book"] = _book(asks=[(0.30, 100_000)], bids=[(0.29, 100)], ts=time.time())

    decision = entry_manager.evaluate_entry(_ev(), token_id="tok", min_net_ev=-9.0)

    assert one_book["calls"] == 1
    assert decision.available_depth_usd == pytest.approx(30_000.0)
    assert decision.slippage_at_size_pct == pytest.approx(0.0)


def test_evaluate_entry_refuses_a_crossed_book_under_the_depth_rule(one_book, monkeypatch):
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)
    _no_legacy_calls(monkeypatch)
    one_book["book"] = _book(asks=[(0.30, 1000)], bids=[(0.31, 100)], ts=time.time())

    decision = entry_manager.evaluate_entry(_ev(), token_id="tok", min_net_ev=-9.0)

    assert not decision.approved
    assert decision.rule_id == "depth"
    assert "crossed" in decision.reason


def test_evaluate_entry_refuses_when_the_ask_moved_up(one_book, monkeypatch):
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)
    _no_legacy_calls(monkeypatch)
    one_book["book"] = _book(asks=[(0.33, 1000)], bids=[(0.29, 100)], ts=time.time())

    decision = entry_manager.evaluate_entry(_ev(price=0.30), token_id="tok", min_net_ev=-9.0)

    assert not decision.approved
    assert "ask_moved" in decision.reason


def test_exit_side_depth_warns_on_a_crossed_book_but_still_answers(one_book, capsys):
    one_book["book"] = _book(asks=[(0.30, 10)], bids=[(0.31, 100)], ts=time.time())
    assert market_client.get_bid_depth_usd("tok") == pytest.approx(31.0)
    assert "crossed" in capsys.readouterr().out


def test_executor_refuses_a_crossed_book_at_submission_with_a_code(one_book):
    import executor
    from types import SimpleNamespace
    one_book["book"] = _book(asks=[(0.30, 1000)], bids=[(0.31, 100)], ts=time.time())
    spec = SimpleNamespace(notional_usd=1.0, pad_cost_pct=0.0)
    decision = SimpleNamespace(recommended_size_usd=1.0, token_id="tok", entry_price=0.30,
                               available_depth_usd=300.0, slippage_at_size_pct=0.0,
                               net_ev_at_size=0.2, min_net_ev=0.1)
    out = {}
    ok, note = executor._resolved_size_ok(spec, decision, out=out)
    assert not ok
    assert out["refusal_code"] == "resolved_book_crossed"


# ---------------------------------------------------------------------------
# 2. A paper fill is booked at the VWAP of the walked book, not the top ask
# ---------------------------------------------------------------------------

def _paper_decision(price=0.30, slip=0.02, size=10.0):
    from models import EntryDecision
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 3), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=size, available_depth_usd=1000.0,
        slippage_at_size_pct=slip, net_ev_at_size=0.30,
        approved=True, reason="test", station_maturity="mature",
        entry_price=price, token_id="TOK",
    )


def test_paper_entry_is_booked_at_the_walked_vwap_and_fee_follows_it(monkeypatch):
    import executor
    import risk_manager
    import storage
    monkeypatch.setitem(executor.EXECUTION_MODE, "WSSS", "paper")
    captured = []
    monkeypatch.setattr(storage, "open_position", captured.append)

    executor.open_position(_paper_decision(price=0.30, slip=0.02, size=10.0))

    (pos,) = captured
    assert pos.entry_price == pytest.approx(0.306)          # 0.30 x (1 + 2%)
    assert pos.size_usd == pytest.approx(10.0)               # the stake is unchanged
    # storage charges the entry fee on the price it is handed -- the VWAP
    assert storage._entry_fee_for(pos) == pytest.approx(risk_manager.taker_fee_per_share(0.306))


def test_paper_entry_with_no_slippage_is_the_ask(monkeypatch):
    import executor
    import storage
    monkeypatch.setitem(executor.EXECUTION_MODE, "WSSS", "paper")
    captured = []
    monkeypatch.setattr(storage, "open_position", captured.append)

    executor.open_position(_paper_decision(slip=0.0))

    assert captured[0].entry_price == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# 3. Live fill accounting fails safe: reconcile from the exchange, else UNKNOWN
# ---------------------------------------------------------------------------

from clients import wallet_client  # noqa: E402


class _Lib:
    class Side:
        BUY, SELL = "BUY", "SELL"

    class OrderType:
        FOK = "FOK"

    class AssetType:
        CONDITIONAL = "CONDITIONAL"

    class MarketOrderArgs:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class BalanceAllowanceParams:
        def __init__(self, **kw):
            self.__dict__.update(kw)


class _Client:
    """post_order returns `response` (or raises it); balances are served in order."""

    def __init__(self, response, balances, sign_error=None):
        self.response, self.balances, self.sign_error = response, list(balances), sign_error

    def create_market_order(self, args):
        if self.sign_error:
            raise self.sign_error
        return {"signed": True}

    def post_order(self, signed, order_type):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def get_balance_allowance(self, params):
        b = self.balances.pop(0)
        if isinstance(b, Exception):
            raise b
        return {"balance": str(int(b * wallet_client.BALANCE_BASE_UNITS))}


def _wire(monkeypatch, client):
    monkeypatch.setattr(wallet_client, "_clob", lambda: _Lib)
    monkeypatch.setattr(wallet_client, "get_client", lambda: client)
    monkeypatch.setattr(wallet_client, "_wait_for_balance", lambda c: True)
    monkeypatch.setattr(wallet_client, "live_trading_enabled", lambda: True)


def _buy_spec(shares=5.0, limit=0.30):
    return wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=limit,
        size_shares=shares, notional_usd=round(shares * limit, 2),
        expected_price=limit, tick_size="0.01",
    )


def test_exception_after_post_with_a_balance_increase_is_a_fill(monkeypatch):
    _wire(monkeypatch, _Client(TimeoutError("read timed out"), balances=[0.0, 5.0]))
    r = wallet_client.submit_order(_buy_spec(), live=True)
    assert r.filled and not r.unknown
    assert r.fill_shares == pytest.approx(5.0)
    assert r.fill_price == pytest.approx(0.30)          # worst-case bound, alerted by executor


def test_exception_after_post_with_no_balance_change_is_unknown_not_no_fill(monkeypatch):
    # A lagging balance cannot prove the FOK was killed.
    _wire(monkeypatch, _Client(TimeoutError("read timed out"), balances=[0.0, 0.0]))
    r = wallet_client.submit_order(_buy_spec(), live=True)
    assert r.unknown and not r.filled


def test_exception_after_post_with_an_unreadable_balance_is_unknown(monkeypatch):
    _wire(monkeypatch, _Client(TimeoutError("x"), balances=[0.0, RuntimeError("403")]))
    r = wallet_client.submit_order(_buy_spec(), live=True)
    assert r.unknown and not r.filled


def test_a_signing_failure_never_reached_the_exchange(monkeypatch):
    _wire(monkeypatch, _Client({}, balances=[0.0], sign_error=ValueError("bad key")))
    r = wallet_client.submit_order(_buy_spec(), live=True)
    assert not r.submitted and not r.filled and not r.unknown


def test_a_matched_response_missing_amounts_is_reconciled_by_balance(monkeypatch):
    _wire(monkeypatch, _Client({"success": True, "status": "matched", "size_matched": "5"},
                               balances=[1.0, 6.0]))
    r = wallet_client.submit_order(_buy_spec(), live=True)
    assert r.filled and not r.unknown
    assert r.fill_shares == pytest.approx(5.0)


def test_a_matched_response_missing_amounts_and_balance_is_unknown(monkeypatch):
    _wire(monkeypatch, _Client({"success": True, "status": "matched", "size_matched": "5"},
                               balances=[RuntimeError("403"), RuntimeError("403")]))
    r = wallet_client.submit_order(_buy_spec(), live=True)
    assert r.unknown and not r.filled


def test_a_partial_fill_books_what_matched_not_what_was_asked(monkeypatch):
    _wire(monkeypatch, _Client({"success": True, "status": "matched",
                                "takingAmount": "3.0", "makingAmount": "0.87"},
                               balances=[0.0]))
    r = wallet_client.submit_order(_buy_spec(shares=5.0), live=True)
    assert r.filled and not r.unknown
    assert r.fill_shares == pytest.approx(3.0)
    assert r.fill_price == pytest.approx(0.29)


# --- executor: an UNKNOWN attempt is recorded, alerted, and blocks the token --

@pytest.fixture
def live_wsss(monkeypatch, tmp_db):
    import executor
    monkeypatch.setattr(
        executor, "EXECUTION_MODE",
        {icao: ("manual_review" if icao != "WSSS" else "live") for icao in config.STATIONS},
    )
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(wallet_client, "_book_constraints", lambda token_id: ("0.01", 5.0))
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions, **_: wallet_client.Reconciliation(ok=True, checked=True, reason="stubbed"),
    )
    alerts_sent = []
    import alerts
    monkeypatch.setattr(alerts, "send", lambda t, m, priority="default": alerts_sent.append((t, m)) or True)
    return alerts_sent


def _live_decision():
    from models import EntryDecision
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 3), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=1.50, available_depth_usd=1000.0,
        slippage_at_size_pct=0.0, net_ev_at_size=0.30, min_net_ev=0.0,
        approved=True, reason="test", station_maturity="mature",
        entry_price=0.30, token_id="TOK",
    )


def _submit_returns(monkeypatch, **kw):
    calls = []

    def _submit(spec, live):
        calls.append(spec)
        return wallet_client.OrderResult(submitted=True, simulated=False, spec=spec, **kw)

    monkeypatch.setattr(wallet_client, "submit_order", _submit)
    return calls


def test_an_unknown_live_entry_books_nothing_records_unknown_alerts_and_blocks(monkeypatch, live_wsss):
    import executor
    import storage
    calls = _submit_returns(monkeypatch, filled=False, unknown=True, error="TimeoutError")

    executor.open_position(_live_decision())

    assert storage.load_open_positions() == []
    rows = storage.load_live_order_attempts()
    assert [r["outcome"] for r in rows] == ["unknown"]
    assert any("UNKNOWN" in t for t, _ in live_wsss)

    # the next entry on the same token is refused before it reaches the exchange
    executor.open_position(_live_decision())
    assert len(calls) == 1
    assert storage.load_live_order_attempts()[0]["detail"].startswith("unknown_order_unreconciled")

    # an operator's 'reconciled' row lifts the block
    storage.record_live_order_attempt(kind="entry", station_icao="WSSS", outcome="reconciled",
                                      target_date=date(2026, 9, 3), bucket_c=32, side="YES",
                                      detail="operator: checked exchange, no fill")
    executor.open_position(_live_decision())
    assert len(calls) == 2


def test_a_fill_with_missing_details_is_never_booked_at_the_limit(monkeypatch, live_wsss):
    import executor
    import storage
    _submit_returns(monkeypatch, filled=True, fill_price=None, fill_shares=None, order_id="0x1")

    executor.open_position(_live_decision())

    assert storage.load_open_positions() == []
    assert storage.load_live_order_attempts()[0]["outcome"] == "unknown"


def test_a_partial_live_fill_is_booked_at_its_real_size(monkeypatch, live_wsss):
    import executor
    import storage
    _submit_returns(monkeypatch, filled=True, fill_price=0.29, fill_shares=3.0, order_id="0x1")

    executor.open_position(_live_decision())

    (pos,) = storage.load_open_positions()
    assert pos.size_shares == pytest.approx(3.0)
    assert pos.entry_price == pytest.approx(0.29)
    assert pos.size_usd == pytest.approx(0.87)


# ---------------------------------------------------------------------------
# 4. close_position fails CLOSED on an execution mode it does not know
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_mode", ["auto", "", None, "LIVE"])
def test_close_position_refuses_an_unknown_mode_and_alerts_once(monkeypatch, bad_mode):
    import alerts
    import executor
    import storage
    from models import ExitDecision, Position
    closes, sent = [], []
    monkeypatch.setattr(storage, "close_position", lambda **kw: closes.append(kw) or True)
    monkeypatch.setattr(wallet_client, "submit_order",
                        lambda *a, **k: pytest.fail("must not trade an unknown-mode position"))
    monkeypatch.setattr(alerts, "send", lambda t, m, priority="default": sent.append(t) or True)
    monkeypatch.setattr(executor, "_unknown_mode_alerted", set())
    pos = Position(position_id=f"p-{bad_mode}", station_icao="WSSS", target_date=date(2026, 9, 3),
                   bucket_c=32, side="YES", entry_price=0.3, size_usd=1.0,
                   entry_time="2026-09-03T00:00:00+00:00", status="open", token_id="TOK",
                   execution_mode=bad_mode)
    decision = ExitDecision(position_id=pos.position_id, should_exit=True, reason="stop_loss",
                            current_price=0.2, pnl_pct=-0.33)

    executor.close_position(pos, decision)
    executor.close_position(pos, decision)

    assert closes == []
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# 5. markout_report: read-only, look-back quotes, settlement payoff
# ---------------------------------------------------------------------------

def test_markout_report_reads_moves_and_settlement_without_writing(tmp_path):
    import sqlite3
    import markout_report as mr
    pw, md = tmp_path / "pw.sqlite3", tmp_path / "md.sqlite3"
    c = sqlite3.connect(pw)
    c.execute("CREATE TABLE positions (position_id, station_icao, target_date, bucket_c, side, "
              "entry_price, entry_time, status, exit_price, token_id, execution_mode)")
    c.execute("CREATE TABLE settled_buckets (station_icao, target_date, bucket_c)")
    c.execute("INSERT INTO positions VALUES ('p1','WSSS','2026-09-03',32,'YES',0.31,"
              "'2026-09-03T00:00:00+00:00','closed_resolution',1.0,'TOK','paper')")
    c.execute("INSERT INTO settled_buckets VALUES ('WSSS','2026-09-03',32)")
    c.commit(); c.close()
    t0 = int(datetime_fromiso("2026-09-03T00:00:00+00:00"))
    c = sqlite3.connect(md)
    c.execute("CREATE TABLE price_snapshots (token_id, ts, price, ask_price, source)")
    c.executemany("INSERT INTO price_snapshots VALUES ('TOK', ?, ?, ?, 'live_snapshot')", [
        (t0 - 60, 0.29, 0.31),            # entry: mid 0.30
        (t0 + 14 * 60, 0.31, 0.33),       # at +15m: mid 0.32
        (t0 + 15 * 60 + 1, 0.99, 0.99),   # AFTER +15m -- must not be read for +15m
        (t0 + 3600 - 100, 0.35, 0.37),    # +1h: mid 0.36
    ])
    c.commit(); c.close()

    (row,) = mr.entry_rows(mr._ro(pw), mr._ro(md))

    assert row["mid_15m"] == pytest.approx(0.02)
    assert row["markout_15m"] == pytest.approx(0.32 - 0.31)
    assert row["mid_1h"] == pytest.approx(0.06)
    assert row["mid_6h"] is None                       # nothing within staleness
    assert row["markout_settle"] == pytest.approx(1.0 - 0.31)
    with pytest.raises(sqlite3.OperationalError):
        mr._ro(pw).execute("INSERT INTO settled_buckets VALUES ('X','2026-01-01',1)")


def datetime_fromiso(s):
    from datetime import datetime
    return datetime.fromisoformat(s).timestamp()


def test_reconciled_rows_do_not_count_against_the_daily_order_cap(tmp_db):
    import storage
    storage.record_live_order_attempt(kind="entry", station_icao="WSSS", outcome="reconciled",
                                      target_date=date(2026, 9, 3), bucket_c=32, side="YES")
    assert storage.count_live_order_attempts("entry", "2000-01-01") == 0
