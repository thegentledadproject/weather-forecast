"""
tests/test_bid_depth_capture.py

BID-SIDE depth: capture (market_client, price_store, ev_engine,
position_manager) and the exit fill it enables (backtest.fill_model).

WHY IT EXISTS. Every depth figure this system had was ASK-side --
market_client.get_available_depth_usd() sums the asks, because it was built
to size an ENTRY, which buys. An EXIT sells into the BIDS, and no bid-side
depth had ever been recorded: 26% of live_snapshot rows carry depth_usd,
exit-path rows carry none by design, clob_history carries none.

That gap is what stopped the per-position loss cap being decided on 2026-09-02.
Simulating the cap against the recorded bid series brackets it only by
assumption -- fill at the trigger (+$42 over holding) versus fill at the
lowest quote of the day (-$74) -- and the honest middle, filling at the FIRST
bid that crosses the trigger, is +$42/+$9/+$28/+$22 across the four candidate
rules. What none of those can price is SIZE: they all assume the whole order
clears at one quote. Bid depth is the missing input.

THE ONE TEST THAT MATTERS MOST is test_reads_the_bid_side_not_the_ask. The
ask-side function already exists and has the same shape; a bid-depth function
that quietly summed asks would produce plausible numbers that are wrong in
exactly the direction that flatters an exit.

Capture is FAIL-SOFT everywhere, and that is not incidental: this runs inside
the loop carrying every open position's exit decision. A book fetch that
fails, or a store that rejects the column, must cost a NULL and nothing else.
"""

import sqlite3

import pytest

from clients import market_client
from backtest import fill_model as fill_model_mod
from backtest import price_store


def _book(bids, asks):
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    }


# --- market_client.get_bid_depth_usd ------------------------------------

def test_sums_bids_within_the_impact_band(monkeypatch):
    # top bid 0.50, 10% band -> anything at or above 0.45 counts.
    monkeypatch.setattr(market_client, "get_order_book", lambda *a, **k: _book(
        bids=[(0.50, 100), (0.46, 100), (0.40, 999)], asks=[(0.51, 100)]))
    depth = market_client.get_bid_depth_usd("tok", max_price_impact_pct=0.10)
    assert depth == pytest.approx(0.50 * 100 + 0.46 * 100)


def test_reads_the_bid_side_not_the_ask(monkeypatch):
    """
    A book with a deep ask side and a thin bid side must report the THIN
    number. The pre-existing depth figure in this system is the ask one; a
    bid function that summed asks would overstate every modelled exit.
    """
    monkeypatch.setattr(market_client, "get_order_book", lambda *a, **k: _book(
        bids=[(0.50, 10)], asks=[(0.51, 10000)]))
    assert market_client.get_bid_depth_usd("tok") == pytest.approx(5.0)


def test_no_book_or_no_bids_is_unknown_not_zero(monkeypatch):
    """
    Same contract as the ask-side function: None means "not captured", and
    callers must not read it as an empty book. price_store stores NULL.
    """
    monkeypatch.setattr(market_client, "get_order_book", lambda *a, **k: None)
    assert market_client.get_bid_depth_usd("tok") is None
    monkeypatch.setattr(market_client, "get_order_book", lambda *a, **k: _book(bids=[], asks=[(0.5, 10)]))
    assert market_client.get_bid_depth_usd("tok") is None


def test_a_malformed_book_returns_none_rather_than_raising(monkeypatch):
    monkeypatch.setattr(market_client, "get_order_book", lambda *a, **k: {"bids": [{"nope": 1}]})
    assert market_client.get_bid_depth_usd("tok") is None


# --- price_store ---------------------------------------------------------

def test_bid_depth_round_trips(tmp_path):
    db = tmp_path / "m.sqlite3"
    price_store.save_snapshot(token_id="t", ts=1000, price=0.4, depth_usd=None,
                              source="live_exit_check", fidelity_min=13,
                              db_path=str(db), bid_depth_usd=12.5)
    got = sqlite3.connect(db).execute(
        "SELECT bid_depth_usd FROM price_snapshots WHERE token_id='t'").fetchone()[0]
    assert got == 12.5


def test_the_column_is_added_to_a_pre_existing_database(tmp_path):
    """
    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists --
    the same trap ask_price documented in 2026-08-10. The deployed
    market_data.sqlite3 is 177MB and predates this column.
    """
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE price_snapshots (token_id TEXT NOT NULL, ts INTEGER NOT NULL, "
        "price REAL NOT NULL, depth_usd REAL, source TEXT NOT NULL, "
        "fidelity_min INTEGER NOT NULL, ask_price REAL, PRIMARY KEY (token_id, ts, source))")
    conn.commit()
    conn.close()

    price_store.save_snapshot(token_id="t", ts=1, price=0.2, depth_usd=None,
                              source="live_exit_check", fidelity_min=13,
                              db_path=str(db), bid_depth_usd=3.0)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(price_snapshots)")}
    assert "bid_depth_usd" in cols


def test_omitting_bid_depth_stores_null(tmp_path):
    """Every existing caller keeps working and records 'not captured'."""
    db = tmp_path / "m.sqlite3"
    price_store.save_snapshot(token_id="t", ts=2, price=0.3, depth_usd=None,
                              source="clob_history", fidelity_min=5, db_path=str(db))
    got = sqlite3.connect(db).execute(
        "SELECT bid_depth_usd FROM price_snapshots WHERE token_id='t'").fetchone()[0]
    assert got is None


# --- the exit fill -------------------------------------------------------

def test_a_small_sale_realises_about_the_bid():
    fm = fill_model_mod.FillModel(depth_regime="strict", fee_rate_pct=None, station_icao="WSSS")
    px = fm.exit_fill_price(quote_price=0.50, size_usd=1.0, bid_depth_usd=10000.0)
    # NOT exactly BASE_SPREAD_PCT: utilisation is 1/10000, so the impact term
    # is a real (tiny) 0.0000005. Asserting equality here would be asserting
    # that slippage() ignores size, which is the opposite of the point.
    assert px == pytest.approx(0.50 * (1 - fill_model_mod.BASE_SPREAD_PCT), abs=1e-3)
    assert px < 0.50 * (1 - fill_model_mod.BASE_SPREAD_PCT)


def test_a_sale_that_eats_the_book_realises_materially_less():
    fm = fill_model_mod.FillModel(depth_regime="strict", fee_rate_pct=None, station_icao="WSSS")
    thin = fm.exit_fill_price(quote_price=0.50, size_usd=100.0, bid_depth_usd=100.0)
    deep = fm.exit_fill_price(quote_price=0.50, size_usd=100.0, bid_depth_usd=100000.0)
    assert thin < deep < 0.50


def test_the_exit_fill_is_below_the_quote_where_the_entry_fill_is_above_it():
    """
    Direction is the whole point: a buyer pays MORE than the quote, a seller
    receives LESS. Sharing slippage() between them makes an inverted sign the
    likeliest bug, so it is pinned.
    """
    fm = fill_model_mod.FillModel(depth_regime="strict", fee_rate_pct=None, station_icao="WSSS")
    assert fm.exit_fill_price(0.50, 10.0, 500.0) < 0.50 < fm.entry_fill_price(0.50, 10.0, 500.0)


def test_unknown_bid_depth_costs_the_live_fallback_not_a_guess():
    fm = fill_model_mod.FillModel(depth_regime="strict", fee_rate_pct=None, station_icao="WSSS")
    px = fm.exit_fill_price(quote_price=0.50, size_usd=10.0, bid_depth_usd=None)
    assert px == pytest.approx(0.50 * (1 - fill_model_mod.FALLBACK_SLIPPAGE_PCT))


def test_the_exit_fill_never_goes_below_zero():
    fm = fill_model_mod.FillModel(depth_regime="strict", fee_rate_pct=None, station_icao="WSSS")
    assert fm.exit_fill_price(quote_price=0.01, size_usd=10_000.0, bid_depth_usd=0.01) >= 0.0


# --- the capture wiring --------------------------------------------------

def test_capture_exit_snapshot_passes_bid_depth_through(monkeypatch):
    import ev_engine
    from datetime import date

    # Patched on the real module, not via sys.modules: ev_engine does
    # `import backtest.price_store as price_store` INSIDE the function, which
    # resolves through the package attribute and would walk straight past a
    # sys.modules entry.
    seen = {}
    monkeypatch.setattr(price_store, "upsert_token", lambda **k: None)
    monkeypatch.setattr(price_store, "save_snapshot", lambda **k: seen.update(k))
    monkeypatch.setattr(ev_engine, "ENABLE_SNAPSHOT_CAPTURE", True)

    ev_engine.capture_exit_snapshot(
        station_icao="WSSS", target_date=date(2026, 9, 2), bucket_c=33, side="YES",
        token_id="tok", bid_price=0.42, fidelity_min=13, bid_depth_usd=88.0,
    )
    assert seen["bid_depth_usd"] == 88.0
    assert seen["depth_usd"] is None, "the ask-side column stays NULL on the exit path"


def test_the_exit_path_fetches_bid_depth_when_enabled(monkeypatch):
    import config
    import position_manager
    from datetime import date
    from models import Position

    monkeypatch.setattr(config, "CAPTURE_EXIT_BID_DEPTH", True)
    calls = []
    monkeypatch.setattr(market_client, "get_bid_depth_usd",
                        lambda token_id, **k: calls.append(token_id) or 55.0)
    captured = {}
    import ev_engine
    monkeypatch.setattr(ev_engine, "capture_exit_snapshot", lambda **k: captured.update(k))

    position_manager._capture_exit_price(
        Position(position_id="p", station_icao="WSSS", target_date=date(2026, 9, 2),
                 bucket_c=33, side="YES", entry_price=0.4, size_usd=5.0,
                 entry_time="2026-09-02T00:00:00+00:00", status="open", high_water_mark=0.4),
        token_id="tok", bid_price=0.38, fidelity_min=13,
    )
    assert calls == ["tok"]
    assert captured["bid_depth_usd"] == 55.0


def test_a_failing_depth_fetch_still_records_the_price(monkeypatch):
    """
    THE FAIL-SOFT CONTRACT. This runs inside the loop carrying every open
    position's exit decision. A book fetch that raises must cost a NULL in
    one column and nothing else -- not the price row, and certainly not the
    exit check.
    """
    import config
    import position_manager
    import ev_engine
    from datetime import date
    from models import Position

    monkeypatch.setattr(config, "CAPTURE_EXIT_BID_DEPTH", True)

    def boom(*a, **k):
        raise RuntimeError("book unavailable")

    monkeypatch.setattr(market_client, "get_bid_depth_usd", boom)
    captured = {}
    monkeypatch.setattr(ev_engine, "capture_exit_snapshot", lambda **k: captured.update(k))

    position_manager._capture_exit_price(
        Position(position_id="p", station_icao="WSSS", target_date=date(2026, 9, 2),
                 bucket_c=33, side="YES", entry_price=0.4, size_usd=5.0,
                 entry_time="2026-09-02T00:00:00+00:00", status="open", high_water_mark=0.4),
        token_id="tok", bid_price=0.38, fidelity_min=13,
    )
    assert captured["bid_price"] == 0.38, "the price row must still be written"
    assert captured["bid_depth_usd"] is None


def test_the_toggle_off_fetches_no_book(monkeypatch):
    """One extra order-book call per open position per cycle is a real cost
    (~2,200/day against ~24,500 quote fetches). It must be switchable off
    without touching code."""
    import config
    import position_manager
    import ev_engine
    from datetime import date
    from models import Position

    monkeypatch.setattr(config, "CAPTURE_EXIT_BID_DEPTH", False)
    calls = []
    monkeypatch.setattr(market_client, "get_bid_depth_usd", lambda *a, **k: calls.append(1))
    captured = {}
    monkeypatch.setattr(ev_engine, "capture_exit_snapshot", lambda **k: captured.update(k))

    position_manager._capture_exit_price(
        Position(position_id="p", station_icao="WSSS", target_date=date(2026, 9, 2),
                 bucket_c=33, side="YES", entry_price=0.4, size_usd=5.0,
                 entry_time="2026-09-02T00:00:00+00:00", status="open", high_water_mark=0.4),
        token_id="tok", bid_price=0.38, fidelity_min=13,
    )
    assert calls == []
    assert captured["bid_depth_usd"] is None
