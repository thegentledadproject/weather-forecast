"""
tests/test_settlement_fallback.py

position_manager closes a resolved position from the station's own
settlement-grade observation when the order book can no longer be read.

THE FAILURE THIS FIXES. Once a Polymarket weather event settles, its book
is unseeded -- every bucket returns "no orderbook" forever. The exit path
correctly refused to guess between 1.0 and 0.0, and then left the position
open, emitting an UNMONITORABLE warning every cycle. Five positions were
cleared by hand between 2026-08-09 and 2026-08-14 (RKSI 33C YES at 51
consecutive failed price reads, WMKK 35C YES at 59) with two more queued
behind them.

The refusal was right; the resignation was the bug. The winning side was
never unknowable -- it just wasn't on the book.

WHAT MUST NOT REGRESS is the refusal itself. Every test below that removes
the settlement observation asserts the position stays OPEN.
"""
from datetime import date, timedelta

import config
import position_manager
import storage
from clients import market_client
from models import ObservedReading, Position

SETTLED_DAY = date.today() - timedelta(days=2)


def _pos(bucket_c=33, side="YES", station="RKSI", target_date=SETTLED_DAY,
         entry_price=0.11, execution_mode="paper") -> Position:
    return Position(
        position_id=f"{station}:{target_date}:{bucket_c}:{side}:x",
        station_icao=station,
        target_date=target_date,
        bucket_c=bucket_c,
        side=side,
        entry_price=entry_price,
        size_usd=7.60,
        entry_time="2026-08-12T20:02:17+00:00",
        status="open",
        token_id="tok",
        execution_mode=execution_mode,
    )


def _reading(temp_c, station="RKSI", target_date=SETTLED_DAY, source=None):
    if source is None:
        source = config.get_station(station).resolution_grade_source
    return ObservedReading(
        station_icao=station,
        target_date=target_date,
        max_temp_c=temp_c,
        source=source,
    )


def _dead_book(monkeypatch):
    """The post-settlement state: no price, ever."""
    monkeypatch.setattr(
        market_client, "get_current_price_for_side", lambda token_id, side: None,
    )


def _observations(monkeypatch, rows):
    monkeypatch.setattr(storage, "load_observations_since", lambda icao, since: rows)


def _capture_closes(monkeypatch):
    closed = []
    monkeypatch.setattr(
        position_manager.executor, "close_position",
        lambda position, decision, status=None, exit_reason=None:
            closed.append((position.position_id, decision.current_price, status, exit_reason)),
    )
    return closed


class TestClosesFromTheObservationRecord:
    def test_a_losing_bucket_settles_at_zero(self, monkeypatch):
        """RKSI 2026-08-13: settled 31.0C, position held 33C YES."""
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        decision = position_manager._close_resolved_without_price(_pos(), "tok")

        assert decision is not None and decision.should_exit
        assert decision.reason == "resolution"
        assert closed[0][1] == 0.0
        assert closed[0][2:] == ("closed_resolution", "market_resolved")

    def test_the_winning_bucket_settles_at_par(self, monkeypatch):
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(33.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        position_manager._close_resolved_without_price(_pos(bucket_c=33), "tok")

        assert closed[0][1] == 1.0

    def test_a_NO_position_pays_on_every_bucket_but_the_winner(self, monkeypatch):
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        position_manager._close_resolved_without_price(_pos(bucket_c=33, side="NO"), "tok")

        assert closed[0][1] == 1.0

    def test_it_never_books_an_intermediate_price(self, monkeypatch):
        """Par or nothing -- 0.5 is not a settlement outcome."""
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.4)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        position_manager._close_resolved_without_price(_pos(bucket_c=31), "tok")

        assert closed[0][1] in (0.0, 1.0)


class TestNeverGuess:
    """The refusal is the safety property. It must survive every path."""

    def test_no_observation_leaves_the_position_open(self, monkeypatch):
        _dead_book(monkeypatch)
        _observations(monkeypatch, [])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        assert position_manager._close_resolved_without_price(_pos(), "tok") is None
        assert closed == []

    def test_an_observation_for_a_DIFFERENT_DAY_is_not_used(self, monkeypatch):
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0, target_date=SETTLED_DAY + timedelta(days=1))])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        assert position_manager._close_resolved_without_price(_pos(), "tok") is None
        assert closed == []

    def test_a_proxy_grade_source_is_not_a_settlement_source(self, monkeypatch):
        """
        A good forecast input is not an authority on settlement. Accepting
        one here would close positions on the wrong number.
        """
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0, source="open_meteo_analysis")])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        assert position_manager._close_resolved_without_price(_pos(), "tok") is None
        assert closed == []

    def test_a_storage_failure_does_not_settle_anything(self, monkeypatch):
        def _boom(icao, since):
            raise sqlite_error("database is locked")

        _dead_book(monkeypatch)
        monkeypatch.setattr(storage, "load_observations_since", _boom)
        closed = _capture_closes(monkeypatch)

        assert position_manager._close_from_settlement_source(_pos(), gamma_closed=True) is None
        assert closed == []


def sqlite_error(msg):
    import sqlite3
    return sqlite3.OperationalError(msg)


class TestTheTriggerConditions:
    def test_gamma_saying_OPEN_is_never_overridden(self, monkeypatch):
        """
        A live market with a broken price feed is a FEED problem. Settling
        it on the weather would close a position that can still trade.
        """
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: False)
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [_pos()])
        closed = _capture_closes(monkeypatch)

        for _ in range(position_manager.UNMONITORABLE_CYCLES_WARN + 1):
            position_manager.check_and_exit_positions()

        assert closed == []

    def test_gamma_unreachable_plus_a_past_date_still_settles(self, monkeypatch):
        """
        The stranding case: both feeds down. The observation record is not,
        and it is the authority the other two were only ever proxies for.
        """
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: None)
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [_pos()])
        closed = _capture_closes(monkeypatch)

        for _ in range(position_manager.UNMONITORABLE_CYCLES_WARN + 1):
            position_manager.check_and_exit_positions()

        assert len(closed) >= 1
        assert closed[0][1] == 0.0

    def test_a_still_running_market_day_is_not_settled_early(self, monkeypatch):
        """
        Gamma unreachable but the market day has NOT passed -- nothing to
        settle yet, whatever happens to be in the observation table.
        """
        today = _pos(target_date=date.today() + timedelta(days=1))
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0, target_date=today.target_date)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: None)
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [today])
        closed = _capture_closes(monkeypatch)

        for _ in range(position_manager.UNMONITORABLE_CYCLES_WARN + 1):
            position_manager.check_and_exit_positions()

        assert closed == []

    def test_it_waits_for_the_unmonitorable_threshold(self, monkeypatch):
        """One failed read is a blip, not a resolution."""
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: None)
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [_pos()])
        closed = _capture_closes(monkeypatch)

        position_manager.check_and_exit_positions()

        assert closed == []


class TestBucketEdgeMode:
    def test_the_station_edge_mode_is_honoured(self, monkeypatch):
        """
        A 'floor' station (HKO reports 0.1C and the market resolves to the
        containing range) must not get half_up: 33.9C is bucket 33, never
        34. Reimplementing the rounding here instead of reusing
        backtest.resolution is exactly how that would break.
        """
        station = config.get_station("RKSI")
        monkeypatch.setattr(station, "bucket_edge_mode", "floor", raising=False)
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(33.9)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        position_manager._close_resolved_without_price(_pos(bucket_c=33), "tok")

        assert closed[0][1] == 1.0, "floor mode should put 33.9C in bucket 33"
