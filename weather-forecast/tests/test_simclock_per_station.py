"""
The simulated clock is per-station (design C5).

It used to be ONE fixed UTC+8 timezone shared by every simulated instant,
which drives three things at once: the tick schedule
(generate_ticks -> scheduler.determine_window), edge-decay tightening
(risk_manager.evaluate_exit's local_hour) and observation visibility
(resolution.observation_visible's local-date comparison). A station outside
UTC+8 replayed its entire trading thesis at the wrong local hour -- RJTT
(+9) an hour off, OPKC (+5) three hours off.

The engine refused those stations rather than produce a mistimed result
that looked complete. Correct, but it left 6 of 13 registered stations
unbacktestable, including every UTC+9 one, which is why the P&L attempt on
2026-08-10 had only one usable window.

The invariant these pin: a replay wakes at the same LOCAL clock times
whatever the offset, and at different UTC instants.
"""

from datetime import date, timedelta

import pytest

import config
from backtest import engine, settings, simclock


DAY = date(2026, 8, 10)


def test_local_minute_to_ts_shifts_with_the_offset():
    at_8 = simclock.local_minute_to_ts(DAY, 5 * 60, 8)
    at_9 = simclock.local_minute_to_ts(DAY, 5 * 60, 9)
    at_5 = simclock.local_minute_to_ts(DAY, 5 * 60, 5)

    # 05:00 in a further-east zone happens EARLIER in absolute time.
    assert at_8 - at_9 == 3600
    assert at_5 - at_8 == 3 * 3600


def test_omitting_the_offset_keeps_the_legacy_behaviour():
    """The synthetic test scenario is built on the zero-arg form."""
    assert (simclock.local_minute_to_ts(DAY, 5 * 60)
            == simclock.local_minute_to_ts(DAY, 5 * 60, settings.LOCAL_UTC_OFFSET_HOURS))


def test_clock_reports_the_hour_of_its_own_station():
    ts = simclock.local_minute_to_ts(DAY, 5 * 60, 9)

    assert simclock.SimClock(ts, utc_offset_hours=9).local_hour() == 5
    # The same instant read on the old shared clock is the bug: 04:00.
    assert simclock.SimClock(ts, utc_offset_hours=8).local_hour() == 4


def test_local_date_rolls_over_at_the_station_s_own_midnight():
    """
    Observation visibility and target_date both key on the local DATE, so a
    clock an hour out flips days at the wrong instant.
    """
    ts = simclock.local_minute_to_ts(DAY, 23 * 60 + 30, 9)   # 23:30 local at +9

    assert simclock.SimClock(ts, utc_offset_hours=9).local_date() == DAY
    assert simclock.SimClock(ts, utc_offset_hours=8).local_date() == DAY
    # ...and half an hour later the +9 station is on the next day while
    # the +8 reading still is not.
    ts += 1800
    assert simclock.SimClock(ts, utc_offset_hours=9).local_date() == DAY + timedelta(days=1)
    assert simclock.SimClock(ts, utc_offset_hours=8).local_date() == DAY


@pytest.mark.parametrize("offset", [5, 8, 9])
def test_the_schedule_replays_at_the_same_local_times_everywhere(offset):
    """
    THE invariant. config.SCHEDULE_WINDOWS is expressed in local hours, so
    every station must wake at the same local clock times -- at different
    UTC instants. Before the fix, a +9 station's ticks landed at UTC
    instants corresponding to +8 local hours, i.e. it ran the 05:00-08:00
    edge window at 06:00-09:00 its own time, after the market had moved.
    """
    ticks = simclock.generate_ticks(DAY, offset)
    assert ticks

    local_times = [
        simclock.SimClock(t.ts, utc_offset_hours=offset).local_datetime().strftime("%H:%M")
        for t in ticks
    ]
    baseline = [
        simclock.SimClock(t.ts, utc_offset_hours=8).local_datetime().strftime("%H:%M")
        for t in simclock.generate_ticks(DAY, 8)
    ]
    assert local_times == baseline
    assert [t.mode for t in ticks] == [t.mode for t in simclock.generate_ticks(DAY, 8)]


def test_different_offsets_produce_different_utc_instants():
    """The complement: same local schedule, genuinely different absolute times."""
    a = [t.ts for t in simclock.generate_ticks(DAY, 8)]
    b = [t.ts for t in simclock.generate_ticks(DAY, 9)]

    assert a != b
    assert all(x - y == 3600 for x, y in zip(a, b))


def test_ticks_stay_strictly_increasing_at_every_offset():
    for offset in (5, 8, 9):
        ts = [t.ts for t in simclock.generate_ticks(DAY, offset)]
        assert ts == sorted(ts)
        assert len(set(ts)) == len(ts)


# --------------------------------------------------------------------------
# The engine no longer refuses these stations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("icao", ["RJTT", "RKSI", "OPKC"])
def test_engine_accepts_stations_outside_utc_plus_8(icao, tmp_path, monkeypatch):
    """
    The guard these used to hit raised ValueError mentioning "LOCAL_TZ is
    fixed". With no market data seeded the run is empty, which is fine --
    what matters is that it RUNS rather than refusing.
    """
    import contextlib
    import io

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    assert config.get_station(icao).utc_offset_hours != settings.LOCAL_UTC_OFFSET_HOURS

    with contextlib.redirect_stdout(io.StringIO()):
        run = engine.run(
            station_icao=icao, start_date=DAY, end_date=DAY,
            depth_regime="strict", fee_rate_pct=0.0, bankroll_mode="static",
            market_db_path=str(tmp_path / "market.sqlite3"),
        )

    assert run.station_icao == icao


def test_run_records_the_offset_it_actually_used(tmp_path, monkeypatch):
    """
    A result carrying only the DEFAULT offset would be unreadable after the
    fact: two runs of different stations would look identically configured.
    """
    import contextlib
    import io

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    with contextlib.redirect_stdout(io.StringIO()):
        run = engine.run(
            station_icao="RJTT", start_date=DAY, end_date=DAY,
            depth_regime="strict", fee_rate_pct=0.0, bankroll_mode="static",
            market_db_path=str(tmp_path / "market.sqlite3"),
        )

    assert run.manifest["backtest_settings"]["sim_utc_offset_hours"] == 9
    assert run.manifest["backtest_settings"]["LOCAL_UTC_OFFSET_HOURS"] == 8


def test_every_registered_station_is_now_backtestable():
    """
    Structural: the point of the fix was coverage. Nothing in the registry
    should be excluded by its timezone any more.
    """
    offsets = {icao: config.get_station(icao).utc_offset_hours for icao in config.STATIONS}
    assert len(set(offsets.values())) > 1, "registry no longer spans timezones -- test is stale"
    for icao, offset in offsets.items():
        ticks = simclock.generate_ticks(DAY, offset)
        assert ticks, f"{icao} (UTC+{offset}) produced no ticks"
