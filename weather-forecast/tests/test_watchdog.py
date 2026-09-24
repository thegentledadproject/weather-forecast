"""watchdog.py: alert when the daemon has stopped writing, not when it is merely closed."""
from datetime import datetime, timedelta, timezone

import pytest

import alerts
import storage
import watchdog
from models import PointForecast
from datetime import date

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(alerts, "send", lambda *a, **kw: out.append(a) or True)
    return out


def _write_forecast(at):
    storage.save_forecast(PointForecast(
        station_icao="WSSS", source="watchdog_test", target_date=date(2026, 9, 24),
        max_temp_c=31.0, fetched_at=at.isoformat()))


def test_latest_write_reads_the_newest_forecast_row():
    _write_forecast(NOW - timedelta(minutes=5))
    assert storage.latest_cycle_write_ts() >= (NOW - timedelta(minutes=5)).isoformat()


def test_fresh_writes_are_silent(monkeypatch, sent):
    monkeypatch.setattr(storage, "latest_cycle_write_ts",
                        lambda: (NOW - timedelta(minutes=50)).isoformat())
    assert watchdog.check(now=NOW) == 0
    assert sent == []


def test_stale_writes_alert(monkeypatch, sent):
    monkeypatch.setattr(storage, "latest_cycle_write_ts",
                        lambda: (NOW - timedelta(hours=4)).isoformat())
    assert watchdog.check(now=NOW) == 1
    assert len(sent) == 1


def test_no_rows_at_all_alerts(monkeypatch, sent):
    monkeypatch.setattr(storage, "latest_cycle_write_ts", lambda: None)
    assert watchdog.check(now=NOW) == 1
    assert len(sent) == 1


def test_an_unreadable_database_alerts(monkeypatch, sent):
    def boom():
        raise OSError("no such file")
    monkeypatch.setattr(storage, "latest_cycle_write_ts", boom)
    assert watchdog.check(now=NOW) == 1
    assert "no such file" in sent[0][1]


def test_quiet_is_expected_when_every_station_was_closed(monkeypatch, sent):
    """One UTC+8 group, 02:00 local: closed since 22:45, so 4h of silence is normal."""
    monkeypatch.setattr(watchdog, "_offsets", lambda: [8])
    at = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)   # 02:00 UTC+8
    monkeypatch.setattr(storage, "latest_cycle_write_ts",
                        lambda: (at - timedelta(hours=4)).isoformat())
    assert watchdog.check(now=at) == 0
    assert sent == []


def test_the_shipped_station_set_is_never_all_closed():
    """With today's offsets some group is always open, so the 3h rule always applies."""
    for minute in range(0, 24 * 60, 15):
        at = datetime(2026, 9, 24, tzinfo=timezone.utc) + timedelta(minutes=minute)
        assert watchdog._some_group_open_throughout(at, watchdog.STALE_AFTER), at


# --- clock drift ---------------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_clock_synced_is_ok(monkeypatch):
    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda *a, **kw: _FakeCompleted(0, "yes\n"))
    status, detail = watchdog._clock_drift_status()
    assert status == "ok"


def test_clock_not_synced_alerts(monkeypatch):
    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda *a, **kw: _FakeCompleted(0, "no\n"))
    status, detail = watchdog._clock_drift_status()
    assert status == "alert"
    assert "NTPSynchronized" in detail


def test_missing_timedatectl_and_chronyc_is_unknown_not_alert(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("no such tool")
    monkeypatch.setattr(watchdog.subprocess, "run", boom)
    status, detail = watchdog._clock_drift_status()
    assert status == "unknown"


def test_timedatectl_timeout_falls_back_to_unknown(monkeypatch):
    import subprocess as sp

    def timeout(*a, **kw):
        raise sp.TimeoutExpired(cmd="timedatectl", timeout=5)
    monkeypatch.setattr(watchdog.subprocess, "run", timeout)
    status, detail = watchdog._clock_drift_status()
    assert status == "unknown"


def test_chrony_offset_beyond_threshold_alerts(monkeypatch):
    def fake_run(cmd, **kw):
        if cmd[0] == "timedatectl":
            return _FakeCompleted(0, "yes\n")
        return _FakeCompleted(0, "System time     : 3.500000000 seconds fast of NTP time\n")
    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    status, detail = watchdog._clock_drift_status()
    assert status == "alert"
    assert "3.5" in detail


def test_chrony_offset_within_threshold_is_ok(monkeypatch):
    def fake_run(cmd, **kw):
        if cmd[0] == "timedatectl":
            return _FakeCompleted(0, "yes\n")
        return _FakeCompleted(0, "System time     : 0.100000000 seconds fast of NTP time\n")
    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    status, detail = watchdog._clock_drift_status()
    assert status == "ok"


def test_check_never_crashes_when_clock_tools_are_missing(monkeypatch, sent):
    def boom(*a, **kw):
        raise FileNotFoundError("no such tool")
    monkeypatch.setattr(watchdog.subprocess, "run", boom)
    monkeypatch.setattr(storage, "latest_cycle_write_ts",
                        lambda: (NOW - timedelta(minutes=5)).isoformat())
    assert watchdog.check(now=NOW) == 0
    assert sent == []


def test_check_alerts_and_returns_nonzero_on_clock_drift(monkeypatch, sent):
    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda *a, **kw: _FakeCompleted(0, "no\n"))
    monkeypatch.setattr(storage, "latest_cycle_write_ts",
                        lambda: (NOW - timedelta(minutes=5)).isoformat())
    assert watchdog.check(now=NOW) == 1
    assert len(sent) == 1
