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
    # Discovery SURVIVES settlement -- only the order book is unseeded --
    # so by default these tests present an event whose bounds match config.
    _event_bounds(monkeypatch, None)


def _event_bounds(monkeypatch, bounds, station="RKSI"):
    """
    Present a live event with the given bucket bounds. None means "same as
    config", the no-drift case.
    """
    if bounds is None:
        st = config.get_station(station)
        bounds = (st.bucket_min_c, st.bucket_max_c)
    monkeypatch.setattr(
        position_manager, "_event_bounds", lambda position, station: bounds,
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


class TestBoundsDrift:
    """
    bucket_for_temp() CLAMPS into whatever bounds it is given, so the bounds
    decide the answer for any reading at or past an edge. config.STATIONS'
    bounds are a seasonal cross-check that drifts; the event's are truth.

    Measured 2026-08-14: 10 of 13 stations had drifted (RJTT by 4C,
    RKPK/ZBAA by 5C) and 15 readings from the previous fortnight settle into
    a different bucket under config's bounds than under the live event's.
    """

    def test_a_winner_at_the_stale_lower_clamp_is_not_written_off(self, monkeypatch):
        """
        The real ZBAA case, 2026-08-12. Live event 25-35 settles 27.0C as
        bucket 27. config's stale 30-40 clamps it to 30 -- so a WINNING 27C
        position gets settled as a loser. Silently, by this function.
        """
        station = config.get_station("RKSI")
        monkeypatch.setattr(station, "bucket_min_c", 30, raising=False)
        monkeypatch.setattr(station, "bucket_max_c", 40, raising=False)

        _dead_book(monkeypatch)
        _event_bounds(monkeypatch, (25, 35))
        _observations(monkeypatch, [_reading(27.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        position_manager._close_resolved_without_price(_pos(bucket_c=27), "tok")

        assert closed[0][1] == 1.0, (
            "settled on config's stale lower clamp -- a winning position paid 0.0"
        )

    def test_a_reading_past_the_stale_upper_clamp_settles_on_the_event(self, monkeypatch):
        """Live 22-32 folds 34.0C into the 32 catch-all; config's 26-36 says 34."""
        station = config.get_station("RKSI")
        monkeypatch.setattr(station, "bucket_min_c", 26, raising=False)
        monkeypatch.setattr(station, "bucket_max_c", 36, raising=False)

        _dead_book(monkeypatch)
        _event_bounds(monkeypatch, (22, 32))
        _observations(monkeypatch, [_reading(34.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        position_manager._close_resolved_without_price(_pos(bucket_c=32), "tok")

        assert closed[0][1] == 1.0, "32C is the live event's top catch-all and won"

    def test_undiscoverable_bounds_refuse_rather_than_fall_back_to_config(self, monkeypatch):
        """
        Falling back to config here would be the whole bug: it looks like a
        safe default and silently settles on numbers known to drift.
        """
        _dead_book(monkeypatch)
        monkeypatch.setattr(position_manager, "_event_bounds", lambda position, station: None)
        _observations(monkeypatch, [_reading(31.0)])
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
        closed = _capture_closes(monkeypatch)

        assert position_manager._close_resolved_without_price(_pos(), "tok") is None
        assert closed == []

    def test_a_malformed_token_map_yields_no_bounds(self, monkeypatch):
        """
        derive_bucket_bounds() rejects a non-contiguous or short map, so a
        partial discovery cannot quietly narrow the bounds.
        """
        station = config.get_station("RKSI")
        monkeypatch.setattr(
            position_manager.market_discovery, "discover_token_map",
            lambda st, d, lo=None, hi=None: {29: {}, 31: {}},  # gappy, 2 of 11
        )
        assert position_manager._event_bounds(_pos(), station) is None


def test_the_mechanism_itself_the_clamp_decides_the_winner():
    """
    Independent of position_manager: the same reading settles into two
    different buckets purely on which bounds bucket_for_temp() is handed.
    This is why the bounds must come from the event and not from a
    cross-check constant that drifts.

    Both cases are real, measured 2026-08-14 against live events.
    """
    from backtest import resolution

    # ZBAA 2026-08-12, 27.0C. Live event 25-35 vs config's stale 30-40.
    assert resolution.bucket_for_temp(27.0, 25, 35, "half_up") == 27
    assert resolution.bucket_for_temp(27.0, 30, 40, "half_up") == 30

    # RJTT 2026-08-08, 34.0C. Live event 22-32 vs config's stale 26-36.
    assert resolution.bucket_for_temp(34.0, 22, 32, "half_up") == 32
    assert resolution.bucket_for_temp(34.0, 26, 36, "half_up") == 34


class TestClosesFromTheRecordedMarketSettlement:
    """
    SECOND TIER, added 2026-08-27. VHHH settles on HKO's CLMMAXT extract,
    which publishes a month at a time weeks late, so a VHHH position whose
    book is unseeded before a resolved price is read has no settlement
    reading for ~six weeks. Three such positions ($14.02) sat open for up
    to six days with the answer already in storage.settled_buckets.

    The tier above still wins where it exists; these only fire when it is
    absent. THE REFUSAL STILL MUST NOT REGRESS -- when neither tier can
    say who won, the position stays open.
    """

    def _no_reading(self, monkeypatch):
        _observations(monkeypatch, [])

    def _settled(self, monkeypatch, record):
        monkeypatch.setattr(
            storage, "load_settled_buckets",
            lambda icao: {} if record is None else {SETTLED_DAY: record},
        )

    def test_a_losing_bucket_settles_at_zero(self, monkeypatch):
        # VHHH 2026-08-21: market settled bucket 31, position held 32 YES.
        _dead_book(monkeypatch)
        self._no_reading(monkeypatch)
        self._settled(monkeypatch, (31, 27, 37))
        closed = _capture_closes(monkeypatch)

        d = position_manager._close_from_settlement_source(
            _pos(bucket_c=32, side="YES"), gamma_closed=True)
        assert d is not None and d.current_price == 0.0
        assert closed[0][2:] == ("closed_resolution", "market_resolved")

    def test_the_winning_bucket_settles_at_par(self, monkeypatch):
        _dead_book(monkeypatch)
        self._no_reading(monkeypatch)
        self._settled(monkeypatch, (31, 27, 37))
        closed = _capture_closes(monkeypatch)

        d = position_manager._close_from_settlement_source(
            _pos(bucket_c=31, side="YES"), gamma_closed=True)
        assert d is not None and d.current_price == 1.0

    def test_a_NO_leg_inverts(self, monkeypatch):
        _dead_book(monkeypatch)
        self._no_reading(monkeypatch)
        self._settled(monkeypatch, (31, 27, 37))
        _capture_closes(monkeypatch)

        d = position_manager._close_from_settlement_source(
            _pos(bucket_c=32, side="NO"), gamma_closed=True)
        assert d is not None and d.current_price == 1.0

    def test_the_observation_record_still_wins_where_it_exists(self, monkeypatch):
        # Preference by authority, not by convenience: the reading is the
        # record the market resolves against, so a station that has one
        # must not start settling off the exchange instead.
        _dead_book(monkeypatch)
        _observations(monkeypatch, [_reading(31.0)])
        self._settled(monkeypatch, (35, 27, 37))   # would disagree, and must not be consulted
        _capture_closes(monkeypatch)

        d = position_manager._close_from_settlement_source(
            _pos(bucket_c=31, side="YES"), gamma_closed=True)
        assert d is not None and d.current_price == 1.0   # 31.0C -> bucket 31, not 35

    def test_no_reading_and_no_settlement_still_refuses(self, monkeypatch):
        _dead_book(monkeypatch)
        self._no_reading(monkeypatch)
        self._settled(monkeypatch, None)
        closed = _capture_closes(monkeypatch)

        assert position_manager._close_from_settlement_source(
            _pos(), gamma_closed=True) is None
        assert closed == []

    def test_a_bucket_outside_the_recorded_event_refuses(self, monkeypatch):
        # A recorded settlement describes ONE event. A position whose
        # bucket falls outside that window is not the same market, and its
        # payout cannot be read across.
        _dead_book(monkeypatch)
        self._no_reading(monkeypatch)
        self._settled(monkeypatch, (31, 27, 37))
        closed = _capture_closes(monkeypatch)

        assert position_manager._close_from_settlement_source(
            _pos(bucket_c=40), gamma_closed=True) is None
        assert closed == []

    def test_a_storage_failure_settles_nothing(self, monkeypatch):
        def _boom(icao):
            raise sqlite_error("database is locked")

        _dead_book(monkeypatch)
        self._no_reading(monkeypatch)
        monkeypatch.setattr(storage, "load_settled_buckets", _boom)
        closed = _capture_closes(monkeypatch)

        assert position_manager._close_from_settlement_source(
            _pos(), gamma_closed=True) is None
        assert closed == []

    def test_gamma_saying_OPEN_is_still_never_overridden(self, monkeypatch):
        # The new tier must not become a way around the one guard that
        # keeps a tradeable market from being settled on stale evidence.
        _dead_book(monkeypatch)
        self._no_reading(monkeypatch)
        self._settled(monkeypatch, (31, 27, 37))
        monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: False)
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [_pos()])
        closed = _capture_closes(monkeypatch)

        for _ in range(position_manager.UNMONITORABLE_CYCLES_WARN + 1):
            position_manager.check_and_exit_positions()

        assert closed == []
