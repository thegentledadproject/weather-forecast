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
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional

import scheduler

from backtest import settings

# Fixed-offset local timezone for every registered station (see
# settings.LOCAL_UTC_OFFSET_HOURS for why a fixed offset, not a tz database).
LOCAL_TZ = timezone(timedelta(hours=settings.LOCAL_UTC_OFFSET_HOURS))

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
    no entries at all (pre_poll, monitor_only, risk_only). None here
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

    def __init__(self, ts: int):
        self.ts = int(ts)

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
        """Current simulated time as a tz-aware datetime at the station-local fixed offset."""
        return self.utc_datetime().astimezone(LOCAL_TZ)

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
        return f"SimClock(ts={self.ts}, local={self.local_datetime().isoformat()})"


def local_minute_to_ts(day: date, minute_of_day: int) -> int:
    """Convert a LOCAL (date, minute-of-day) to unix UTC seconds using the fixed station offset."""
    hour, minute = divmod(minute_of_day, 60)
    local_dt = datetime.combine(day, time(hour=hour, minute=minute), tzinfo=LOCAL_TZ)
    return int(local_dt.timestamp())


def generate_ticks(day: date) -> List[Tick]:
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
                ts=local_minute_to_ts(day, minute_of_day),
                mode=window["mode"],
                min_net_ev=window["min_net_ev"],
                interval_min=interval,
            )
        )
        # Advance by this window's interval, but never PAST the window's
        # own end -- exactly what scheduler.seconds_until_next_boundary()
        # does with min(interval_min, minutes_left_in_window). Without the
        # clamp, a short window (e.g. the 2-minute 04:45-05:00 pre-poll)
        # overshoots its boundary and drags every later tick of the day a
        # minute or two off the times the live daemon actually wakes at.
        minutes_left_in_window = window["end_minute"] - minute_of_day
        minute_of_day += min(interval, max(minutes_left_in_window, 1))

    return ticks
