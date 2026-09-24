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
