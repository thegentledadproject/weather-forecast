"""
tests/test_collection_window.py

The collection cycle: fetch and record, decide nothing.

WHY IT EXISTS. Forecasts were fetched ONLY inside the 3-hour entry
window, so the entire forecast record was censored to the hours the
system happened to be trading -- every "first seen 05:01" was the
schedule observing itself, and nobody could tell whether 05:00 was the
right start time. Verified 2026-08-29 in the live `forecasts` table:
each station's fetch hours were exactly its own window and nothing else.
Price history had the same hole, since the exit path only captures
tokens the book already holds.

`pre_poll` was the other half of the same defect: it polled each
station's official source every 2 minutes from 04:45, stored NOTHING
(storage.save_forecast was reachable only from pipeline.run), and gated
nothing (determine_window is purely clock-based). It is replaced here.

The load-bearing constraint is that collection DECIDES nothing: no
entries surfaced, no exits taken. An exit taken at 04:30 would be a
trading change smuggled in as a data change -- the 04:00-05:00 hour has
never been able to fire a stop, and this must not be where that starts.
"""

import config
import scheduler


class _Spy:
    """Records every call, so a test can assert on what did NOT happen."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _EVRun:
    veto_reason = None
    ev_results = []
    token_map = {}


def _window(mode, start_min, end_min, interval):
    return {
        "start_minute": start_min,
        "end_minute": end_min,
        "interval_min": interval,
        "mode": mode,
        "min_net_ev": None,
        "description": f"test {mode} window",
    }


class TestTheScheduleItself:
    def test_no_pre_poll_window_survives(self):
        modes = {w[5] for w in config.SCHEDULE_WINDOWS}
        assert "pre_poll" not in modes, (
            "pre_poll polls the official source, stores nothing and gates "
            "nothing -- collection replaced it"
        )

    def test_the_hour_before_entries_collects(self):
        for hour, minute in ((4, 0), (4, 30), (4, 59)):
            window = scheduler.determine_window(hour, minute)
            assert window["mode"] == "collection", (
                f"{hour:02d}:{minute:02d} local is not a collection window"
            )

    def test_collection_surfaces_no_ev_bar(self):
        # A window with a min_net_ev is an ENTRY window to every consumer
        # that reads the table -- the replay's tick generator and the
        # dashboard both key off it.
        for w in config.SCHEDULE_WINDOWS:
            if w[5] == "collection":
                assert w[6] is None

    def test_entries_still_open_at_five(self):
        # The guard on the change above: replacing pre_poll must not move
        # the entry window it sits in front of.
        assert scheduler.determine_window(5, 0)["mode"] == "primary"


class TestACollectionCycleRecordsButDecidesNothing:
    def _install(self, monkeypatch):
        import entry_manager
        import ev_engine
        import executor

        spies = {
            "pipeline_run": _Spy({"estimate": object()}),
            "priced": _Spy(_EVRun()),
            "snapshot": _Spy(),
            "entries": _Spy([]),
            "opened": _Spy(),
            "exits": _Spy(),
        }
        monkeypatch.setattr(scheduler.pipeline, "run", spies["pipeline_run"])
        monkeypatch.setattr(ev_engine, "run_for_station_with_map", spies["priced"])
        monkeypatch.setattr(ev_engine, "save_ev_snapshot", spies["snapshot"])
        monkeypatch.setattr(entry_manager, "decide_portfolio_entries", spies["entries"])
        monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.0, 0, 0.0))
        monkeypatch.setattr(executor, "open_position", spies["opened"])
        monkeypatch.setattr(scheduler, "_run_exit_check", spies["exits"])
        monkeypatch.setattr(scheduler, "_ingest_resolution_observations", lambda icaos: None)
        return spies

    def test_it_fetches_and_stores_forecasts(self, monkeypatch):
        spies = self._install(monkeypatch)

        scheduler.run_cycle(_window("collection", 240, 300, 30), station_icaos=["WSSS"])

        assert len(spies["pipeline_run"].calls) == 1, (
            "a collection cycle that never runs the pipeline records no "
            "forecasts -- the censored-record defect is back"
        )

    def test_it_captures_the_book(self, monkeypatch):
        spies = self._install(monkeypatch)

        scheduler.run_cycle(_window("collection", 240, 300, 30), station_icaos=["WSSS"])

        assert len(spies["priced"].calls) == 1, (
            "no market discovery ran, so no price snapshot was written for "
            "a station-day with no open position"
        )
        assert len(spies["snapshot"].calls) == 1

    def test_it_surfaces_no_entries(self, monkeypatch):
        spies = self._install(monkeypatch)

        scheduler.run_cycle(_window("collection", 240, 300, 30), station_icaos=["WSSS"])

        assert spies["entries"].calls == []
        assert spies["opened"].calls == [], "a collection cycle opened a position"

    def test_it_takes_no_exits(self, monkeypatch):
        spies = self._install(monkeypatch)

        scheduler.run_cycle(_window("collection", 240, 300, 30), station_icaos=["WSSS"])

        assert spies["exits"].calls == [], (
            "collection ran an exit check -- 04:00-05:00 has never been able "
            "to fire a stop, and this is not the change that should start it"
        )


class TestTheHourlyThrottle:
    def setup_method(self):
        scheduler._last_collection_ts.clear()

    def test_a_station_never_collected_is_due(self):
        assert scheduler._collection_due("WSSS", now_ts=1_000.0) is True

    def test_a_station_collected_minutes_ago_is_not_due(self):
        scheduler._last_collection_ts["WSSS"] = 1_000.0
        just_after = 1_000.0 + 15 * 60
        assert scheduler._collection_due("WSSS", now_ts=just_after) is False

    def test_a_station_becomes_due_again_after_the_interval(self):
        scheduler._last_collection_ts["WSSS"] = 1_000.0
        an_hour_later = 1_000.0 + config.COLLECTION_INTERVAL_MIN * 60
        assert scheduler._collection_due("WSSS", now_ts=an_hour_later) is True

    def test_the_throttle_is_per_station(self):
        scheduler._last_collection_ts["WSSS"] = 1_000.0
        assert scheduler._collection_due("WMKK", now_ts=1_000.0) is True


class TestMonitorWindowsCollectOnTheThrottle:
    def setup_method(self):
        scheduler._last_collection_ts.clear()

    def _install(self, monkeypatch, due):
        spies = {"collected": _Spy(), "exits": _Spy()}
        monkeypatch.setattr(scheduler, "_run_collection_cycle", spies["collected"])
        monkeypatch.setattr(scheduler, "_run_exit_check", spies["exits"])
        monkeypatch.setattr(scheduler, "_ingest_resolution_observations", lambda icaos: None)
        monkeypatch.setattr(scheduler, "_collection_due", lambda icao, now_ts: due)
        return spies

    def test_a_due_station_collects_alongside_its_exit_check(self, monkeypatch):
        spies = self._install(monkeypatch, due=True)

        scheduler.run_cycle(_window("monitor_only", 600, 720, 15), station_icaos=["WSSS"])

        assert len(spies["collected"].calls) == 1
        assert len(spies["exits"].calls) == 1, (
            "collection displaced the exit check -- the monitoring cadence "
            "must be untouched by it"
        )

    def test_a_throttled_station_still_gets_its_exit_check(self, monkeypatch):
        spies = self._install(monkeypatch, due=False)

        scheduler.run_cycle(_window("monitor_only", 600, 720, 15), station_icaos=["WSSS"])

        assert spies["collected"].calls == []
        assert len(spies["exits"].calls) == 1

    def test_risk_windows_collect_too(self, monkeypatch):
        spies = self._install(monkeypatch, due=True)
        monkeypatch.setattr(scheduler, "_check_same_day_signal", lambda icao: None)

        scheduler.run_cycle(_window("risk_only", 720, 960, 15), station_icaos=["WSSS"])

        assert len(spies["collected"].calls) == 1


class TestTheReplayAgrees:
    def test_replay_neither_enters_nor_exits_on_a_collection_tick(self):
        from backtest import engine

        assert "collection" not in engine.ENTRY_MODES
        assert "collection" not in engine.EXIT_CHECK_MODES

    def test_replay_still_generates_collection_ticks(self):
        from datetime import date

        from backtest import simclock

        ticks = simclock.generate_ticks(date(2026, 8, 30), utc_offset_hours=8)
        collection = [t for t in ticks if t.mode == "collection"]

        assert collection, (
            "the replay skips the collection window entirely, so a replayed "
            "day sees fewer wake-ups than the daemon has"
        )
        assert all(t.min_net_ev is None for t in collection)
