"""
watchdog.py -- alert when the daemon has stopped writing (gap audit
2026-09-24, gap 6). A dead process cannot alert about itself, so this runs
from cron, outside it. READ-ONLY: it never calls storage.set_writable.

Install on the box (not installed by this commit), in ubuntu's crontab,
with NTFY_TOPIC set to the same topic as the daemon's unit:

  */30 * * * * cd ~/weather-forecast/weather-forecast && NTFY_TOPIC=<topic> ~/weather-forecast/.venv/bin/python watchdog.py >> ~/watchdog.log 2>&1

THE SIGNAL is storage.latest_cycle_write_ts(): forecasts.fetched_at moves on
every pipeline.run, including the hourly collection in monitor windows, so
a live daemon writes roughly hourly. STALE_AFTER = 3h leaves two missed
collections of slack.

THE NIGHTLY CLOSED WINDOW. Each station is closed 22:45-04:00 LOCAL
(config.SCHEDULE_WINDOWS), 5h15m of legitimate silence. Across today's
offsets (UTC-7 .. UTC+9) some group is always open, so the rule always
applies; but silence is only alarming if at least one group was open for
the WHOLE stale span, so a narrower station set cannot false-alarm nightly.

Exit status 1 when it alerted, so cron's log shows it. It alerts on every
run while stale (every 30 min); there is no dedupe state to go wrong.
"""
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import alerts
import scheduler
import storage

STALE_AFTER = timedelta(hours=3)


def _offsets():
    return list(scheduler.stations_by_utc_offset())


def _some_group_open_throughout(now: datetime, span: timedelta) -> bool:
    """Was any station group in a non-closed window for every minute of span?"""
    minutes = int(span.total_seconds() // 60)
    for offset in _offsets():
        open_all = True
        for back in range(minutes + 1):
            local = now + timedelta(hours=offset) - timedelta(minutes=back)
            window = scheduler.determine_window(local.hour, local.minute)
            if window is None or window["mode"] == "closed":
                open_all = False
                break
        if open_all:
            return True
    return False


def check(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    try:
        latest = storage.latest_cycle_write_ts()
    except Exception as exc:  # noqa: BLE001 - an unreadable DB is itself the alarm
        msg = f"cannot read the trading DB: {type(exc).__name__}: {exc}"
        print(f"[watchdog] {now.isoformat()} ALERT {msg}")
        alerts.send("polyweather watchdog: DB unreadable", msg, priority="high")
        return 1

    age = now - datetime.fromisoformat(latest) if latest else None
    if age is not None and age <= STALE_AFTER:
        print(f"[watchdog] {now.isoformat()} ok -- last cycle write {latest}")
        return 0
    if not _some_group_open_throughout(now, STALE_AFTER):
        print(f"[watchdog] {now.isoformat()} quiet but every station group was closed -- ok")
        return 0

    msg = (f"no cycle write for {age} (last {latest})" if latest else "no cycle write ever recorded")
    msg += " -- the daemon may be dead or stuck. Check: systemctl status polyweather"
    print(f"[watchdog] {now.isoformat()} ALERT {msg}")
    alerts.send("polyweather watchdog: daemon silent", msg, priority="high")
    return 1


if __name__ == "__main__":
    sys.exit(check())
