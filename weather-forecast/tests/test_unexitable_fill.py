"""
build_exit_order() refuses below the market's share minimum -- loudly, and
correctly. But the trap springs at ENTRY, not at exit: WSSS on 2026-08-20
requested 5.00 shares, was filled 4.891, and had no working stop for the whole
life of the position. Nobody found out until the first stop attempt.

_shares_at_worst_fill() now prevents new occurrences at order-CONSTRUCTION
time, which is the right place for a guard that can refuse. It cannot help
here: the exchange decides what it fills, and a partial fill is a real holding
whatever its size. Nothing inspected the ACTUAL fill.

So the position is still recorded -- an unrecorded real holding is strictly
worse than a flagged one -- and the flag says the exit path cannot sell it.
"""
from datetime import date

import pytest

import config
import executor
import storage
from clients import market_client, wallet_client
from models import EntryDecision


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Keep the live entry path off the network; none of that is under test."""
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions, **_: wallet_client.Reconciliation(ok=True, checked=True, reason="stubbed"),
    )


@pytest.fixture
def captured(monkeypatch):
    opened = []
    monkeypatch.setattr(storage, "open_position", lambda p: opened.append(p))
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "record_live_order_attempt", lambda **kw: None)
    return opened


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(
        executor, "EXECUTION_MODE",
        {icao: ("manual_review" if icao != "WSSS" else "live") for icao in config.STATIONS},
    )


def _fills(monkeypatch, shares, min_order_size=5.0):
    monkeypatch.setattr(
        wallet_client, "_book_constraints", lambda token_id: ("0.01", min_order_size)
    )
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=True, simulated=False, spec=spec,
            order_id="0xabc", fill_price=0.30, fill_shares=shares,
        ),
    )


def _decision():
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 3), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=1.50, available_depth_usd=1000.0,
        slippage_at_size_pct=0.01, net_ev_at_size=0.30,
        approved=True, reason="test", station_maturity="mature",
        entry_price=0.30, token_id="TOK",
    )


def test_a_short_fill_flags_the_position_as_unexitable(monkeypatch, live, captured, capsys):
    """The acceptance case: 4.891 shares against a 5-share minimum."""
    _fills(monkeypatch, shares=4.891, min_order_size=5.0)

    executor.open_position(_decision())

    assert len(captured) == 1, "the shares exist -- the position must still be recorded"
    assert captured[0].exit_blocked_reason is not None
    assert "4.891" in captured[0].exit_blocked_reason
    assert "[ACTION NEEDED]" in capsys.readouterr().out


def test_a_full_fill_leaves_the_flag_clear(monkeypatch, live, captured, capsys):
    _fills(monkeypatch, shares=5.00, min_order_size=5.0)

    executor.open_position(_decision())

    assert len(captured) == 1
    assert captured[0].exit_blocked_reason is None
    assert "[ACTION NEEDED]" not in capsys.readouterr().out


def test_the_flag_survives_a_round_trip_through_storage(tmp_path, monkeypatch):
    """
    A flag held only in memory is worthless: the daemon restarts, and the
    position it cannot sell outlives the process that noticed.
    """
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    from models import Position

    storage.open_position(Position(
        position_id="p1", station_icao="WSSS", target_date=date(2026, 9, 3),
        bucket_c=32, side="YES", entry_price=0.30, size_usd=1.47,
        entry_time="2026-09-03T00:00:00+00:00", status="open", token_id="TOK",
        is_paper=False, size_shares=4.891, execution_mode="live",
        exit_blocked_reason="filled 4.891 shares, under the 5-share market minimum",
    ))

    loaded = storage.load_open_positions(is_paper=False)

    assert len(loaded) == 1
    assert "4.891" in loaded[0].exit_blocked_reason


def test_an_ordinary_position_round_trips_with_no_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    from models import Position

    storage.open_position(Position(
        position_id="p2", station_icao="WSSS", target_date=date(2026, 9, 3),
        bucket_c=32, side="YES", entry_price=0.30, size_usd=1.50,
        entry_time="2026-09-03T00:00:00+00:00", status="open", token_id="TOK",
        is_paper=False, size_shares=5.0, execution_mode="live",
    ))

    assert storage.load_open_positions(is_paper=False)[0].exit_blocked_reason is None


def test_the_cycle_log_reports_a_flagged_position(monkeypatch, capsys):
    """
    Reported when the position is CHECKED, not when a stop first tries to
    sell. The whole defect was that the two were the same moment.
    """
    import position_manager
    from clients import market_client as mc
    from models import Position

    position_manager._exit_blocked_seen.clear()
    monkeypatch.setattr(position_manager, "_token_id_for", lambda p: "TOK")
    monkeypatch.setattr(mc, "get_current_price_for_side", lambda **kw: None)
    monkeypatch.setattr(position_manager, "_note_price_failure", lambda p: 0)

    pos = Position(
        position_id="p1", station_icao="WSSS", target_date=date(2026, 9, 3),
        bucket_c=32, side="YES", entry_price=0.30, size_usd=1.47,
        entry_time="2026-09-03T00:00:00+00:00", status="open", token_id="TOK",
        is_paper=False, size_shares=4.891, execution_mode="live",
        exit_blocked_reason="filled 4.891 shares, under the 5-share market minimum",
    )

    position_manager._check_one_position(pos)
    first = capsys.readouterr().out
    position_manager._check_one_position(pos)
    second = capsys.readouterr().out

    assert "4.891" in first
    assert "cannot be sold" in first.lower()
    # Once per position per process, matching market_client's ghost-book
    # convention -- a flag that never clears must not print on every cycle.
    assert "4.891" not in second


def test_an_unflagged_position_says_nothing(monkeypatch, capsys):
    import position_manager
    from clients import market_client as mc
    from models import Position

    position_manager._exit_blocked_seen.clear()
    monkeypatch.setattr(position_manager, "_token_id_for", lambda p: "TOK")
    monkeypatch.setattr(mc, "get_current_price_for_side", lambda **kw: None)
    monkeypatch.setattr(position_manager, "_note_price_failure", lambda p: 0)

    position_manager._check_one_position(Position(
        position_id="p2", station_icao="WSSS", target_date=date(2026, 9, 3),
        bucket_c=32, side="YES", entry_price=0.30, size_usd=1.50,
        entry_time="2026-09-03T00:00:00+00:00", status="open", token_id="TOK",
        is_paper=False, size_shares=5.0, execution_mode="live",
    ))

    assert "cannot be sold" not in capsys.readouterr().out.lower()


def test_a_market_with_no_minimum_cannot_flag(monkeypatch, live, captured):
    """
    min_order_size is None on markets that publish no floor. Unknown is not
    the same as breached, and guessing one for the other would flag every
    position on those markets for life.
    """
    _fills(monkeypatch, shares=4.891, min_order_size=None)

    executor.open_position(_decision())

    assert captured[0].exit_blocked_reason is None
