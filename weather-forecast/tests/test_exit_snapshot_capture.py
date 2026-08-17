"""
tests/test_exit_snapshot_capture.py

Regression cover for the 2026-08-17 exit-path snapshot capture.

THE BUG. Price history was captured only by ev_engine.run_for_station(),
which scheduler.run_cycle() calls in the "primary"/"secondary" windows --
05:00-10:00 local. The daemon keeps watching open positions until 22:45 in
monitor_only/risk_only windows and fires real exits there, but recorded no
price, so backtest replays were structurally blind to them. Measured across
all 13 stations: 5 of 24 UTC hours covered, ~20 snapshots per token per day
against 288 for a true 5-minute series.

It was not a symmetric loss. At RCSS all 6 stop-losses landed inside the
recorded window and 7 of 11 take-profits landed outside it, because
EDGE_DECAY_TIGHTEN_HOUR_LOCAL halves the profit-take threshold at 10:00 --
the same hour recording stopped. The replay saw the losses and missed the
gains, reporting -6.08% against a live ledger of +10.6%.

What is pinned here:

1. A monitor-window exit cycle WRITES a snapshot (the fix itself).
2. It is tagged with the WINDOW'S cadence, not the 5-minute default --
   get_price_at() derives its staleness limit from that number.
3. Prices that FAILED confirmation are never written. The series must not
   be seeded with the phantom quotes the confirmation logic exists to
   reject.
4. capture_fidelity_min=None disables capture rather than guessing.
5. Capture failure can never break an exit cycle.
"""
from datetime import date, timedelta

import position_manager
import storage
from clients import market_client
from models import Position

TARGET_DATE = date.today() + timedelta(days=2)

MONITOR_WINDOW_INTERVAL_MIN = 15  # config.SCHEDULE_WINDOWS, 10:00-12:00 local


def _pos(position_id="p1", token_id="tok-yes", entry_price=0.30) -> Position:
    return Position(
        position_id=position_id,
        station_icao="RCSS",
        target_date=TARGET_DATE,
        bucket_c=36,
        side="YES",
        entry_price=entry_price,
        size_usd=10.0,
        entry_time="2026-08-17T05:00:00",
        status="open",
        token_id=token_id,
    )


class _CaptureSpy:
    """Stands in for ev_engine.capture_exit_snapshot, recording its calls."""

    def __init__(self, raises: bool = False):
        self.calls = []
        self.raises = raises

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("price store unavailable")


def _install(monkeypatch, positions, price, spy):
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: positions)
    monkeypatch.setattr(
        market_client, "get_current_price_for_side", lambda token_id, side: price,
    )
    # Patched at the position_manager seam rather than on ev_engine: the
    # helper imports ev_engine lazily, so patching the module attribute is
    # what the call actually resolves through.
    monkeypatch.setattr(position_manager, "_capture_exit_price", _adapt(spy))


def _adapt(spy):
    """Bridge the private helper's positional signature to the spy's kwargs."""
    def _inner(position, token_id, bid_price, fidelity_min):
        if fidelity_min is None:
            return
        spy(
            station_icao=position.station_icao,
            target_date=position.target_date,
            bucket_c=position.bucket_c,
            side=position.side,
            token_id=token_id,
            bid_price=bid_price,
            fidelity_min=fidelity_min,
        )
    return _inner


class TestMonitorWindowCyclesRecordPrices:
    def test_exit_cycle_captures_the_price_it_acted_on(self, monkeypatch):
        """The fix. A monitor-window cycle must leave a row behind."""
        spy = _CaptureSpy()
        _install(monkeypatch, [_pos()], 0.33, spy)

        position_manager.check_and_exit_positions(
            capture_fidelity_min=MONITOR_WINDOW_INTERVAL_MIN,
        )

        assert len(spy.calls) == 1, (
            "a monitor-window exit cycle recorded no price -- this is the "
            "2026-08-17 coverage gap reopening"
        )
        assert spy.calls[0]["bid_price"] == 0.33
        assert spy.calls[0]["token_id"] == "tok-yes"
        assert spy.calls[0]["bucket_c"] == 36

    def test_capture_is_tagged_with_the_windows_own_cadence(self, monkeypatch):
        """
        Not DEFAULT_SNAPSHOT_FIDELITY_MIN. get_price_at() derives staleness
        from this field, so tagging a 15-minute row as 5-minute tells every
        future replay that a normal monitor cadence is a data gap.
        """
        spy = _CaptureSpy()
        _install(monkeypatch, [_pos()], 0.33, spy)

        position_manager.check_and_exit_positions(
            capture_fidelity_min=MONITOR_WINDOW_INTERVAL_MIN,
        )

        assert spy.calls[0]["fidelity_min"] == MONITOR_WINDOW_INTERVAL_MIN

    def test_unknown_cadence_captures_nothing(self, monkeypatch):
        """
        Operator scripts and tests fire at no fixed cadence, so they have no
        honest fidelity to declare. Recording nothing beats inventing one.
        """
        spy = _CaptureSpy()
        _install(monkeypatch, [_pos()], 0.33, spy)

        position_manager.check_and_exit_positions(capture_fidelity_min=None)

        assert spy.calls == []


class TestOnlyBelievedPricesAreRecorded:
    def test_a_price_that_fails_confirmation_is_never_captured(self, monkeypatch):
        """
        The feed disagreeing with itself is exactly the quote this module
        refuses to act on. It must not enter the historical series either.
        """
        spy = _CaptureSpy()
        position = _pos(entry_price=0.30)
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [position])
        monkeypatch.setattr(position_manager, "_capture_exit_price", _adapt(spy))

        # Seed a baseline, then jump far enough to demand confirmation and
        # have the re-fetch disagree well past CONFIRMATION_TOLERANCE.
        position_manager._last_observed_price[position.position_id] = 0.30
        prices = iter([0.90, 0.40])
        monkeypatch.setattr(
            market_client,
            "get_current_price_for_side",
            lambda token_id, side: next(prices),
        )

        decisions = position_manager.check_and_exit_positions(
            capture_fidelity_min=MONITOR_WINDOW_INTERVAL_MIN,
        )

        assert decisions == [], "an unconfirmed price should produce no decision"
        assert spy.calls == [], (
            "a price the exit logic refused to believe was written to the "
            "price store -- replays would later trade on it"
        )

    def test_unpriceable_position_captures_nothing(self, monkeypatch):
        spy = _CaptureSpy()
        _install(monkeypatch, [_pos()], None, spy)

        position_manager.check_and_exit_positions(
            capture_fidelity_min=MONITOR_WINDOW_INTERVAL_MIN,
        )

        assert spy.calls == []


class TestEntryWindowsDoNotDoubleCapture:
    """
    The exit path must stay OUT of primary/secondary windows.

    ev_engine.run_for_station() already captures both sides of every bucket
    there, with ask_price and periodic depth. The exit check runs seconds
    later, so an exit-path row written in that window is the NEWER row for
    that token -- and get_price_at() returns the newest row at or before an
    instant. A replay pricing an entry on any later tick would find
    ask_price NULL and fall back to the bid, overstating raw edge by the
    spread. That is exactly what the 2026-08-10 entry-pricing fix removed,
    and it would come back silently, as a data artifact rather than a code
    change.
    """

    def test_full_cycle_exit_check_passes_no_fidelity(self, monkeypatch):
        import scheduler

        seen = {}

        def _spy(station_icao, interval_min=None):
            seen["interval_min"] = interval_min

        monkeypatch.setattr(scheduler, "_run_exit_check", _spy)
        # pipeline.run() must SUCCEED here: on failure _run_full_cycle
        # returns early and never reaches the exit check, which would make
        # this assertion pass without testing anything.
        monkeypatch.setattr(scheduler.pipeline, "run", lambda station_icao: None)
        monkeypatch.setattr(scheduler.pipeline, "print_summary", lambda r: None)

        # Fail the EV leg fast. It is wrapped in its own try/except and the
        # exit check still runs after it, but left alone it calls calibrate()
        # -- which fetches real forecasts over the network and then writes
        # data/ev_latest_RCSS.json. A wiring test must not do either.
        import calibration

        def _no_ev(*a, **kw):
            raise RuntimeError("EV leg stubbed out -- this test only checks wiring")

        monkeypatch.setattr(calibration, "calibrate", _no_ev)

        scheduler._run_full_cycle("RCSS", min_net_ev=0.15)

        # Without this the assertion below passes when the exit check was
        # never reached at all, which is the failure mode most likely to be
        # introduced by an early return above it.
        assert "interval_min" in seen, "_run_full_cycle never reached the exit check"
        assert seen.get("interval_min") is None, (
            "an entry-window cycle asked the exit path to capture -- its "
            "ask-less row will shadow the entry-path row and push replay "
            "entries back onto bid-fallback pricing"
        )

    def test_monitor_window_exit_check_does_pass_the_cadence(self, monkeypatch):
        """The other half: monitor windows must still capture."""
        import scheduler

        seen = {}
        monkeypatch.setattr(
            scheduler, "_run_exit_check",
            lambda station_icao, interval_min=None: seen.update(interval_min=interval_min),
        )
        monkeypatch.setattr(scheduler, "_ingest_resolution_observations", lambda icaos: None)

        window = {
            "start_minute": 600, "end_minute": 720, "interval_min": 15,
            "mode": "monitor_only", "min_net_ev": None, "description": "test",
        }
        scheduler.run_cycle(window, station_icaos=["RCSS"])

        assert seen.get("interval_min") == 15


class TestCaptureIsNeverAGate:
    def test_a_failing_capture_does_not_break_the_exit_cycle(self, monkeypatch):
        """
        This runs inside the loop carrying every open position's stop-loss.
        A broken price store must cost history, never monitoring.
        """
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [_pos()])
        monkeypatch.setattr(
            market_client, "get_current_price_for_side", lambda token_id, side: 0.33,
        )

        def _boom(**kwargs):
            raise RuntimeError("price store unavailable")

        import ev_engine
        monkeypatch.setattr(ev_engine, "capture_exit_snapshot", _boom)

        decisions = position_manager.check_and_exit_positions(
            capture_fidelity_min=MONITOR_WINDOW_INTERVAL_MIN,
        )

        assert [d.position_id for d in decisions] == ["p1"], (
            "a failing snapshot capture stopped the position from being "
            "evaluated -- capture must never be a gate"
        )


class TestExitRowsCountAsLiveCoverage:
    """
    Adding EXIT_SNAPSHOT_SOURCE created a second live source while
    coverage_stats() still counted only the first, so genuinely live rows
    landed in pct_live_snapshot's denominator and never its numerator.

    That number is printed as "From live order books" directly above the
    P&L, under a docstring telling the reader to check it before believing
    the result -- and it would have drifted further wrong every day, since
    entry windows cover 3 local hours against ~14.75 of monitor/risk ones.
    """

    def _store(self, tmp_path):
        import backtest.price_store as price_store
        return price_store, str(tmp_path / "market_test.sqlite3")

    def test_both_live_sources_count_toward_pct_live(self, tmp_path):
        price_store, db = self._store(tmp_path)

        # One entry-path row (ask + depth) and three exit-path rows, which
        # is the shape a real day now produces: monitor windows outnumber
        # entry windows, so exit rows dominate.
        price_store.save_snapshot(
            token_id="tok", ts=1000, price=0.40, ask_price=0.42, depth_usd=100.0,
            source=price_store.LIVE_SNAPSHOT_SOURCE, fidelity_min=5, db_path=db,
        )
        for i, ts in enumerate((2000, 3000, 4000)):
            price_store.save_snapshot(
                token_id="tok", ts=ts, price=0.41, ask_price=None, depth_usd=None,
                source=price_store.EXIT_SNAPSHOT_SOURCE, fidelity_min=15, db_path=db,
            )

        stats = price_store.coverage_stats(["tok"], 0, 9999, db_path=db)

        assert stats["n_ticks"] == 4
        assert stats["pct_live_snapshot"] == 1.0, (
            f"every row is a real order-book read but coverage reported "
            f"{stats['pct_live_snapshot']:.0%} live -- exit-path rows are "
            f"being dropped from the numerator again"
        )

    def test_depth_still_separates_the_two_paths(self, tmp_path):
        """
        Merging the live count must not hide the entry/exit split. It stays
        legible in pct_with_depth, because exit rows carry depth NULL by
        design -- high "live" plus low "with depth" is what a
        monitor-dominated window should look like.
        """
        price_store, db = self._store(tmp_path)

        price_store.save_snapshot(
            token_id="tok", ts=1000, price=0.40, ask_price=0.42, depth_usd=100.0,
            source=price_store.LIVE_SNAPSHOT_SOURCE, fidelity_min=5, db_path=db,
        )
        price_store.save_snapshot(
            token_id="tok", ts=2000, price=0.41, ask_price=None, depth_usd=None,
            source=price_store.EXIT_SNAPSHOT_SOURCE, fidelity_min=15, db_path=db,
        )

        stats = price_store.coverage_stats(["tok"], 0, 9999, db_path=db)

        assert stats["pct_live_snapshot"] == 1.0
        assert stats["pct_with_depth"] == 0.5

    def test_reconstructed_sources_are_still_excluded(self, tmp_path):
        """
        The fix widens "live" to every real book read -- not to everything.
        A reconstructed series must still be excluded, or the field stops
        meaning anything at all.
        """
        price_store, db = self._store(tmp_path)

        price_store.save_snapshot(
            token_id="tok", ts=1000, price=0.40, ask_price=None, depth_usd=None,
            source=price_store.EXIT_SNAPSHOT_SOURCE, fidelity_min=15, db_path=db,
        )
        price_store.save_snapshot(
            token_id="tok", ts=2000, price=0.41, ask_price=None, depth_usd=None,
            source="clob_prices_history", fidelity_min=60, db_path=db,
        )

        stats = price_store.coverage_stats(["tok"], 0, 9999, db_path=db)

        assert stats["pct_live_snapshot"] == 0.5
