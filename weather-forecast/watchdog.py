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

CLOCK DRIFT. Also checks `timedatectl show -p NTPSynchronized --value`,
falling back to `chronyc tracking`'s offset when timedatectl gives no
answer. Alerts if not synchronized or the offset exceeds 2s. Neither tool
exists on a dev box or in a container; that is logged as "unknown", not an
alert -- "can't tell" is not evidence of drift.
"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import alerts
import scheduler
import storage

STALE_AFTER = timedelta(hours=3)
MAX_CLOCK_OFFSET_S = 2.0
_SUBPROCESS_TIMEOUT_S = 5
# Errors a missing/broken tool can raise on a dev box, a container, or a box
# with no chrony/systemd-timesyncd -- none of these mean the clock is bad,
# they mean we cannot tell. Treated as "unknown", never as "alert".
_CANT_TELL = (FileNotFoundError, OSError, subprocess.TimeoutExpired, subprocess.SubprocessError, ValueError, IndexError)


def _clock_drift_status() -> Tuple[str, str]:
    """('ok' | 'alert' | 'unknown', detail). Never raises."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
        )
        synced = out.stdout.strip() if out.returncode == 0 else ""
        if synced == "no":
            return "alert", "timedatectl reports NTPSynchronized=no"
        if synced == "yes":
            offset = _chrony_offset_s()
            if offset is not None and abs(offset) > MAX_CLOCK_OFFSET_S:
                return "alert", f"NTP synchronized but chrony offset {offset:.3f}s exceeds {MAX_CLOCK_OFFSET_S}s"
            return "ok", "NTP synchronized" + (f", offset {offset:.3f}s" if offset is not None else "")
    except _CANT_TELL:
        pass
    # timedatectl gave no usable answer -- fall back to chrony alone.
    offset = _chrony_offset_s()
    if offset is not None:
        if abs(offset) > MAX_CLOCK_OFFSET_S:
            return "alert", f"chrony offset {offset:.3f}s exceeds {MAX_CLOCK_OFFSET_S}s"
        return "ok", f"chrony offset {offset:.3f}s"
    return "unknown", "cannot determine clock sync (no timedatectl/chronyc, or both failed)"


def _chrony_offset_s() -> Optional[float]:
    """Parse `chronyc tracking`'s 'System time' line, or None if unavailable/unparseable."""
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S)
    except _CANT_TELL:
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.strip().startswith("System time"):
            # "System time     : 0.000123456 seconds fast of NTP time"
            try:
                return float(line.split(":", 1)[1].strip().split()[0])
            except _CANT_TELL:
                return None
    return None


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


def _check_clock(now: datetime) -> int:
    """Log the clock-sync status and alert on real drift. Returns 1 if it alerted."""
    status, detail = _clock_drift_status()
    print(f"[watchdog] {now.isoformat()} clock {status} -- {detail}")
    if status == "alert":
        alerts.send("polyweather watchdog: clock drift", detail, priority="high")
        return 1
    return 0


def check(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    clock_alerted = _check_clock(now)
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
        return clock_alerted
    if not _some_group_open_throughout(now, STALE_AFTER):
        print(f"[watchdog] {now.isoformat()} quiet but every station group was closed -- ok")
        return clock_alerted

    msg = (f"no cycle write for {age} (last {latest})" if latest else "no cycle write ever recorded")
    msg += " -- the daemon may be dead or stuck. Check: systemctl status polyweather"
    print(f"[watchdog] {now.isoformat()} ALERT {msg}")
    alerts.send("polyweather watchdog: daemon silent", msg, priority="high")
    return 1


if __name__ == "__main__":
    sys.exit(check())
