"""
backtest/simclock.py

PURPOSE
-------
Simulated time for the replay engine. Two pieces:

  - SimClock: the ONLY clock a backtest is allowed to read. Every
    module that would otherwise call datetime.now() gets its time from
    here instead, so a simulated cycle can never accidentally consult
    the real wall clock and leak the present into the past.
  - generate_ticks(): the timestamps a live daemon WOULD have woken at
    on a given local day, derived from scheduler.determine_window() --
    the same pure function the real daemon dispatches on. This matters
    more than it looks: a backtest that scans on a flat interval is
    testing a strategy nobody runs. The live system scans every 10
    minutes in the 05:00-08:00 edge window and every 3 hours in the
    evening, so the backtest must too, or its fill opportunities and
    exit-check frequency are both fiction.

WINDOWS WITH NO INTERVAL
------------------------
config.SCHEDULE_WINDOWS gives "closed" windows interval_min=None. A
live daemon sleeps straight through those (scheduler.run_cycle()
returns immediately on mode "closed"), so no Tick is emitted for them
-- the simulator jumps to the window's end instead. Emitting ticks
there would put decision points in a part of the day the real system
is deliberately silent in.

DEPENDENCIES
------------
dataclasses, datetime, typing (standard library)
scheduler.py (local, for determine_window)
backtest/settings.py (local)
config.py (local, for current_utc_offset_hours / DST_AWARE_LOCAL_HOUR)
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional

import config
import scheduler

from backtest import settings

# Default local offset, used only when a caller does not name one. Every
# function and the SimClock itself now take utc_offset_hours, because the
# registry spans UTC+5 (Karachi) through UTC+9 (Japan/Korea) and a single
# shared clock replayed all of them at Singapore's hours.
#
# Still a FIXED offset rather than a tz database entry: no registered city
# observes DST. That is an assumption which happens to hold for every Asian
# market listed so far, not a general truth -- re-check it before
# registering a station in a DST region (see models.StationConfig).
LOCAL_TZ = timezone(timedelta(hours=settings.LOCAL_UTC_OFFSET_HOURS))


def tz_for(utc_offset_hours: Optional[int] = None) -> timezone:
    """The fixed-offset timezone for a station, defaulting to the legacy UTC+8."""
    if utc_offset_hours is None:
        return LOCAL_TZ
    return timezone(timedelta(hours=utc_offset_hours))


def utc_offset_for(station, day: date) -> int:
    """
    The UTC offset to replay `station` at on LOCAL day `day`. WAVE 3 (3d):
    DST-AWARE -- config.current_utc_offset_hours at UTC midnight of that
    day, the same anchor config.local_day_bounds_utc uses, so a replay's
    local hour on a summer EGLC day is the hour the live daemon saw (and a
    run spanning the October transition keys each half on its own clock;
    engine.run calls this per day and retunes the SimClock). A station
    without an iana_timezone gets its static int, so Asia is unchanged.
    DST_AWARE_LOCAL_HOUR=False restores the static registry int for every
    station, which is what every run before Wave 3 used.
    """
    st = config.get_station(station) if isinstance(station, str) else station
    if not config.DST_AWARE_LOCAL_HOUR:
        return st.utc_offset_hours
    anchor = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return config.current_utc_offset_hours(st, at=anchor)

_MINUTES_PER_DAY = 24 * 60

# How far to jump when determine_window() matches nothing at all. Only
# reachable if SCHEDULE_WINDOWS ever develops a gap; advancing rather than
# emitting keeps tick generation terminating instead of spinning.
_NO_WINDOW_ADVANCE_MIN = 30


@dataclass
class Tick:
    """
    One simulated wake-up of the daemon.

    ts is unix UTC seconds -- the single time representation the whole
    backtest passes around, so nothing has to guess whether a given
    datetime was local or UTC.

    min_net_ev carries the active window's EV bar straight through from
    config.SCHEDULE_WINDOWS, INCLUDING its None for windows that surface
    no entries at all (collection, monitor_only, risk_only). None here
    means "this window does not open positions", which is different from
    a bar of 0.0, and collapsing the two would let the simulator enter
    trades in windows the live system never enters trades in.
    """
    ts: int
    mode: str
    min_net_ev: Optional[float]
    interval_min: int


class SimClock:
    """
    Monotone simulated clock. Constructed at a unix UTC second and
    advanced only forward -- advance_to() raises on any backwards move
    rather than accepting it, because time going backwards in a replay
    is always a bug in the caller's ordering, and one that would show up
    later as an inexplicably profitable backtest.
    """

    def __init__(self, ts: int, utc_offset_hours: Optional[int] = None, station=None):
        self.ts = int(ts)
        # The station's own offset. Everything local this clock reports --
        # the hour risk_manager's edge-decay tightening keys on, the date
        # target_date and observation visibility key on -- is derived from
        # it, so a clock built without one replays a UTC+9 station at
        # Singapore's hours: an hour early for +9, three hours late for +5.
        self.utc_offset_hours = (
            settings.LOCAL_UTC_OFFSET_HOURS if utc_offset_hours is None else int(utc_offset_hours)
        )
        self.tz = tz_for(self.utc_offset_hours)
        # WAVE 3 (3d) fix round 1: the station this clock replays, ICAO or
        # StationConfig, or None. utc_offset_for()/retune() are day-anchored
        # (right either side of a DST transition, wrong ON the transition
        # day itself -- EGLC 2026-10-25 would replay all day at +1 even
        # after the 01:00Z instant it actually became +0). Carrying the
        # station lets local_datetime() re-resolve the offset PER TICK
        # instead, so the transition instant itself reads correctly. None
        # (every pre-fix caller/test) keeps the old day-anchored behaviour.
        self.station = config.get_station(station) if isinstance(station, str) else station
        self._offset_cache_bucket = None
        self._offset_cache_value = None

    def retune(self, utc_offset_hours: int) -> None:
        """
        Change the offset this clock reports LOCAL time at, without moving
        the instant. A multi-day replay does this at each day boundary with
        simclock.utc_offset_for(), so the half of a run after a DST
        transition keys risk_manager's tightening and observation
        visibility on the right local hour. Superseded within a single day
        by the per-tick resolution in local_datetime() when this clock
        carries a station and DST_AWARE_LOCAL_HOUR is on; still the value
        used otherwise (no station, or the flag off).
        """
        self.utc_offset_hours = int(utc_offset_hours)
        self.tz = tz_for(self.utc_offset_hours)

    def _current_offset_hours(self) -> int:
        """
        The UTC offset AT THIS INSTANT (self.ts), so a tick landing after a
        mid-day DST transition reads the new offset even though the day's
        tick grid and the last retune() were both anchored at the day's
        start. Falls back to self.utc_offset_hours (static, or whatever
        retune() last set) when this clock has no station or the flag is
        off -- exactly the behaviour every pre-fix caller relies on.

        Cached per UTC-hour bucket of self.ts: a DST transition only ever
        falls on an hour boundary, so this is at most one ZoneInfo lookup
        per simulated hour, not one per tick.
        """
        if self.station is None or not config.DST_AWARE_LOCAL_HOUR:
            return self.utc_offset_hours
        bucket = self.ts // 3600
        if bucket != self._offset_cache_bucket:
            self._offset_cache_value = config.current_utc_offset_hours(self.station, at=self.utc_datetime())
            self._offset_cache_bucket = bucket
        return self._offset_cache_value

    def advance_to(self, ts: int) -> None:
        """Move the clock to ts. Raises ValueError if that would move time backwards."""
        ts = int(ts)
        if ts < self.ts:
            raise ValueError(
                f"SimClock cannot move backwards: current ts={self.ts} "
                f"({self.now_iso()}), requested ts={ts}. A replay that "
                f"revisits an earlier timestamp is reading the future."
            )
        self.ts = ts

    def utc_datetime(self) -> datetime:
        """Current simulated time as a tz-aware UTC datetime."""
        return datetime.fromtimestamp(self.ts, timezone.utc)

    def local_datetime(self) -> datetime:
        """
        Current simulated time as a tz-aware datetime at THIS station's
        offset. WAVE 3 (3d) fix round 1: resolves via _current_offset_hours()
        (per-tick, cached per UTC hour) rather than the fixed self.tz, so a
        tick after a mid-day DST transition reads the new offset.
        """
        return self.utc_datetime().astimezone(tz_for(self._current_offset_hours()))

    def local_hour(self) -> int:
        """Local hour 0-23 -- what risk_manager._local_hour() would have returned at this instant."""
        return self.local_datetime().hour

    def local_minute(self) -> int:
        return self.local_datetime().minute

    def local_date(self) -> date:
        """The LOCAL calendar date, which is what target_date and the schedule are both keyed on."""
        return self.local_datetime().date()

    def now_iso(self) -> str:
        """
        UTC ISO timestamp in exactly the format executor.py writes for
        entry_time/exit_time (datetime.now(timezone.utc).isoformat(), i.e.
        offset-aware with a '+00:00' suffix). Matching it exactly means
        simulated positions sort and compare against real stored rows
        without any special-casing.
        """
        return self.utc_datetime().isoformat()

    def __repr__(self) -> str:
        return (f"SimClock(ts={self.ts}, utc_offset_hours={self.utc_offset_hours}, "
                f"local={self.local_datetime().isoformat()})")


def local_minute_to_ts(day: date, minute_of_day: int,
                       utc_offset_hours: Optional[int] = None) -> int:
    """
    Convert a LOCAL (date, minute-of-day) to unix UTC seconds at a station's
    own fixed offset. Omitting utc_offset_hours keeps the legacy UTC+8
    behaviour, which is what the synthetic test scenario is built on.
    """
    hour, minute = divmod(minute_of_day, 60)
    local_dt = datetime.combine(day, time(hour=hour, minute=minute),
                                tzinfo=tz_for(utc_offset_hours))
    return int(local_dt.timestamp())


def generate_ticks(day: date, utc_offset_hours: Optional[int] = None) -> List[Tick]:
    """
    Every timestamp a live daemon would have woken at on the given LOCAL
    day, in strictly increasing order.

    Starts at settings.SIM_DAY_START_HOUR_LOCAL:00 local (the real
    system's hard floor -- nothing runs before 04:00 by explicit design,
    see config.SCHEDULE_WINDOWS) and walks forward, asking
    scheduler.determine_window() what is active at each point and
    advancing by that window's own interval. Stops at the end of the
    local day.

    No Tick is emitted for a window the daemon sleeps through (mode
    "closed", or any window with interval_min=None); the walk jumps to
    that window's end instead.
    """
    ticks: List[Tick] = []
    minute_of_day = settings.SIM_DAY_START_HOUR_LOCAL * 60

    while minute_of_day < _MINUTES_PER_DAY:
        hour, minute = divmod(minute_of_day, 60)
        window = scheduler.determine_window(hour, minute)

        if window is None:
            # Fails safe exactly like scheduler.run_forever() does: log
            # nothing, take no decision, try again a little later.
            minute_of_day += _NO_WINDOW_ADVANCE_MIN
            continue

        interval = window["interval_min"]
        if window["mode"] == "closed" or interval is None or interval <= 0:
            end_minute = window["end_minute"]
            # Guard against a malformed window that wouldn't advance us.
            minute_of_day = max(end_minute, minute_of_day + _NO_WINDOW_ADVANCE_MIN)
            continue

        ticks.append(
            Tick(
                ts=local_minute_to_ts(day, minute_of_day, utc_offset_hours),
                mode=window["mode"],
                min_net_ev=window["min_net_ev"],
                interval_min=interval,
            )
        )
        # Advance by this window's interval, but never PAST the window's
        # own end. This walk has always been grid-anchored -- start, then
        # start+interval, clamped at the window end -- and since 2026-08-30
        # scheduler.seconds_until_next_boundary() is too, so live and replay
        # now agree on when the daemon wakes. (Before that the live side
        # answered "one interval from now" AFTER a cycle had run, so a slow
        # group drifted later all day and delivered fewer ticks than every
        # replay assumed.) Without the clamp, a short window overshoots its
        # boundary and drags every later tick of the day off the grid.
        minutes_left_in_window = window["end_minute"] - minute_of_day
        minute_of_day += min(interval, max(minutes_left_in_window, 1))

    return ticks
