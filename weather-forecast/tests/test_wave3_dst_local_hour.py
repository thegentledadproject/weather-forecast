"""
Wave 3 item 3d. EGLC is UTC+0 in the registry and UTC+1 (BST) in September.
config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL = 10 must fire at 10:00 BST = 09:00Z,
not at 10:00Z = 11:00 BST -- live (position_manager._local_hour_for,
risk_manager.evaluate_exit with no hour supplied) AND in the replay
(backtest/simclock via engine.run's per-day offset). Verified against the
current code first: a live EGLC position at entry 0.40 and price 0.52 is a
take_profit at local hour 10 and a hold at 9 (the tightened take is 25% of
the risk unit, the loose one 50%).
"""
from datetime import date, datetime, timezone

import pytest

import config
import position_manager
import risk_manager
from backtest import simclock
from models import Position

SEPT_09Z = datetime(2026, 9, 15, 9, 0, 0, tzinfo=timezone.utc)   # 10:00 BST
DEC_09Z = datetime(2026, 12, 15, 9, 0, 0, tzinfo=timezone.utc)   # 09:00 GMT


class _FakeDateTime:
    fixed = SEPT_09Z

    @classmethod
    def now(cls, tz=None):
        return cls.fixed if tz is None else cls.fixed.astimezone(tz)


def _eglc(**kw):
    d = dict(position_id="EGLC:x", station_icao="EGLC", target_date=date(2026, 9, 15), bucket_c=25,
             side="YES", entry_price=0.40, size_usd=10.0, entry_time="2026-09-15T05:00:00+00:00",
             status="open", high_water_mark=0.40, is_paper=False, execution_mode="live", entry_bid=0.38)
    d.update(kw)
    return Position(**d)


@pytest.fixture
def at_0900z(monkeypatch):
    monkeypatch.setattr(_FakeDateTime, "fixed", SEPT_09Z)
    monkeypatch.setattr(position_manager, "datetime", _FakeDateTime)
    monkeypatch.setattr(risk_manager, "datetime", _FakeDateTime)
    monkeypatch.setattr(config, "_now_utc", lambda: SEPT_09Z)


# --- live -------------------------------------------------------------------

def test_egl_local_hour_is_10_at_0900z_in_september(at_0900z):
    assert position_manager._local_hour_for(_eglc()) == 10


def test_flag_off_restores_the_static_registry_hour(at_0900z, monkeypatch):
    monkeypatch.setattr(config, "DST_AWARE_LOCAL_HOUR", False)
    assert position_manager._local_hour_for(_eglc()) == 9


def test_in_december_both_say_9(monkeypatch):
    monkeypatch.setattr(_FakeDateTime, "fixed", DEC_09Z)
    monkeypatch.setattr(position_manager, "datetime", _FakeDateTime)
    assert position_manager._local_hour_for(_eglc(target_date=date(2026, 12, 15))) == 9


def test_a_non_dst_station_is_unchanged(at_0900z):
    assert position_manager._local_hour_for(_eglc(station_icao="WSSS")) == 17


def test_a_negative_offset_dst_station_in_september(at_0900z):
    """KLGA (America/New_York) is EDT = UTC-4 in September -- fix round 1:
    a negative offset exercised at the position_manager site, not just
    EGLC's positive one."""
    assert position_manager._local_hour_for(_eglc(station_icao="KLGA")) == 5


def test_the_tightening_fires_at_10_true_local_when_no_hour_is_passed(at_0900z):
    """evaluate_exit with local_hour=None resolves the POSITION's station."""
    assert risk_manager.evaluate_exit(_eglc(), 0.52).reason == "take_profit"
    assert risk_manager.evaluate_exit(_eglc(), 0.52, local_hour=10).reason == "take_profit"
    assert risk_manager.evaluate_exit(_eglc(), 0.52, local_hour=9).reason == "hold"


def test_before_10_true_local_the_loose_take_still_holds(monkeypatch):
    """08:00Z is 09:00 BST: loose thresholds, hold. On the pre-Wave-3 code
    the dead UTC+8 default read this instant as 16:00 and took profit --
    this is the assertion that separates the station's clock from
    Singapore's, which the 09:00Z case above cannot (17:00 is tightened too)."""
    monkeypatch.setattr(_FakeDateTime, "fixed", datetime(2026, 9, 15, 8, 0, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(position_manager, "datetime", _FakeDateTime)
    monkeypatch.setattr(risk_manager, "datetime", _FakeDateTime)
    assert position_manager._local_hour_for(_eglc()) == 9
    assert risk_manager.evaluate_exit(_eglc(), 0.52).reason == "hold"


def test_flag_off_the_default_hour_is_the_static_one(at_0900z, monkeypatch):
    monkeypatch.setattr(config, "DST_AWARE_LOCAL_HOUR", False)
    assert risk_manager.evaluate_exit(_eglc(), 0.52).reason == "hold"


def test_local_hour_has_no_default_offset_any_more():
    with pytest.raises(TypeError):
        risk_manager._local_hour()
    with pytest.raises(TypeError):
        risk_manager._active_thresholds()


def test_station_offset_now_falls_back_only_for_an_unregistered_station(at_0900z):
    assert risk_manager._station_offset_now("EGLC") == 1
    assert risk_manager._station_offset_now("WSSS") == 8
    assert risk_manager._station_offset_now("XXXX") == config.LOCAL_UTC_OFFSET_HOURS


# --- replay -----------------------------------------------------------------

def test_the_replay_offset_for_a_summer_egl_day_is_bst():
    assert simclock.utc_offset_for("EGLC", date(2026, 9, 15)) == 1
    assert simclock.utc_offset_for("EGLC", date(2026, 12, 15)) == 0
    assert simclock.utc_offset_for("RJTT", date(2026, 9, 15)) == 9


def test_flag_off_the_replay_offset_is_the_registry_int(monkeypatch):
    monkeypatch.setattr(config, "DST_AWARE_LOCAL_HOUR", False)
    assert simclock.utc_offset_for("EGLC", date(2026, 9, 15)) == 0


def test_the_replay_local_hour_matches_live_at_0900z(at_0900z):
    day = date(2026, 9, 15)
    offset = simclock.utc_offset_for("EGLC", day)
    ts = simclock.local_minute_to_ts(day, 10 * 60, offset)
    clock = simclock.SimClock(ts, utc_offset_hours=offset)
    assert clock.utc_datetime() == SEPT_09Z                 # 10:00 local IS 09:00Z
    assert clock.local_hour() == 10 == position_manager._local_hour_for(_eglc())


def test_retune_changes_the_reported_hour_not_the_instant():
    clock = simclock.SimClock(int(SEPT_09Z.timestamp()), utc_offset_hours=1)
    assert clock.local_hour() == 10
    clock.retune(0)
    assert clock.local_hour() == 9 and clock.ts == int(SEPT_09Z.timestamp())


def test_engine_threads_the_per_day_offset(monkeypatch, tmp_path, tmp_db):
    """A one-day EGLC run in September records BST in its manifest and
    generates its first tick at 04:00 BST = 03:00Z."""
    import contextlib
    import io
    from backtest import engine, settings

    with contextlib.redirect_stdout(io.StringIO()):
        run = engine.run(
            station_icao="EGLC", start_date=date(2026, 9, 15), end_date=date(2026, 9, 15),
            depth_regime="strict", fee_rate_pct=0.0, bankroll_mode="static",
            market_db_path=str(tmp_path / "market.sqlite3"),
        )
    assert run.manifest["backtest_settings"]["sim_utc_offset_hours"] == 1
    assert run.manifest["station_config"]["utc_offset_hours"] == 0
    first_tick = simclock.generate_ticks(date(2026, 9, 15), 1)[0]
    assert datetime.fromtimestamp(first_tick.ts, timezone.utc).hour == settings.SIM_DAY_START_HOUR_LOCAL - 1


# --- transition day itself (fix round 1) -------------------------------------
#
# utc_offset_for()/retune() are DAY-ANCHORED (offset resolved once, at UTC
# midnight of that local day) -- right either side of a DST transition, but
# wrong ON the transition day: EGLC 2026-10-25 anchors at +1 (BST still holds
# at 00:00Z) and would replay the WHOLE day at +1 even after the 01:00Z
# instant it actually became +0 (BST -> GMT). SimClock now re-resolves the
# offset PER TICK from self.ts when it carries a station, so the transition
# instant itself reads correctly even though the tick grid and the day's one
# retune() call are still day-anchored.

def test_engine_transition_day_local_hour_is_per_tick(monkeypatch, tmp_path, tmp_db):
    """A three-day EGLC run spanning the 2026-10-25 BST->GMT transition
    still records the START day's (BST) offset in the manifest -- the
    day-anchored value utc_offset_for()/the manifest are SUPPOSED to keep --
    while the clock itself (built the same way engine.run() builds it, with
    a station) reads the correct PER-TICK local hour straight through the
    transition instant."""
    import contextlib
    import io
    from backtest import engine

    with contextlib.redirect_stdout(io.StringIO()):
        run = engine.run(
            station_icao="EGLC", start_date=date(2026, 10, 24), end_date=date(2026, 10, 26),
            depth_regime="strict", fee_rate_pct=0.0, bankroll_mode="static",
            market_db_path=str(tmp_path / "market.sqlite3"),
        )
    assert run.manifest["backtest_settings"]["sim_utc_offset_hours"] == 1  # Oct 24 is still BST

    eglc = config.get_station("EGLC")
    day_offset = simclock.utc_offset_for("EGLC", date(2026, 10, 25))
    assert day_offset == 1  # the (wrong-if-used-all-day) day anchor

    clock = simclock.SimClock(
        simclock.local_minute_to_ts(date(2026, 10, 25), 0, day_offset),
        utc_offset_hours=day_offset, station=eglc,
    )
    # 00:30Z: still BST (+1) -- 00:30 + 1h = 01:30, hour 1.
    clock.advance_to(int(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()))
    assert clock.local_hour() == 1
    # 01:30Z: the transition already happened at 01:00Z -- now GMT (+0),
    # so 01:30 + 0h = 01:30, hour 1 again, NOT 2 (what the stale +1
    # day-anchor would have given: 01:30 + 1h = 02:30).
    clock.advance_to(int(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).timestamp()))
    assert clock.local_hour() == 1


def test_klga_transition_day_local_hour_is_per_tick():
    """KLGA (America/New_York) EDT->EST transition, 2026-11-01: 07:00Z is
    AFTER the 06:00Z transition, so the true offset is -5 (EST), not the
    day-anchored -4 (EDT) -- 07:00 - 5h = 02:00, hour 2."""
    klga = config.get_station("KLGA")
    day_offset = simclock.utc_offset_for("KLGA", date(2026, 11, 1))
    assert day_offset == -4  # the (wrong-if-used-all-day) day anchor

    clock = simclock.SimClock(
        int(datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc).timestamp()),
        utc_offset_hours=day_offset, station=klga,
    )
    assert clock.local_hour() == 1  # 05:00Z, still EDT: 05:00 - 4h = 01:00
    clock.advance_to(int(datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc).timestamp()))
    assert clock.local_hour() == 2  # 07:00Z, now EST: 07:00 - 5h = 02:00


def test_no_station_keeps_the_day_anchored_offset_all_day():
    """Without a station (every pre-fix caller/test), local_hour() must
    still use the stored/retuned offset for the whole day -- the per-tick
    resolution is opt-in via `station`, not a silent behaviour change."""
    clock = simclock.SimClock(
        int(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()), utc_offset_hours=1,
    )
    clock.advance_to(int(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).timestamp()))
    assert clock.local_hour() == 2  # stale +1 anchor: 01:30 + 1h = 02:30
