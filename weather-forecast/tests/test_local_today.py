"""
tests/test_local_today.py

Regression tests for the UTC target-date bug: the deployment box runs on
UTC, where date.today() is still YESTERDAY for the first eight hours of
the SGT trading day -- including the entire 05:00-08:00 primary entry
window. Live cycles in that window fetched forecasts labeled with the
previous day and calibrated for a market that had already ended
(discovered 2026-08-02 via the backtest's per-entry-hour Brier audit:
zero same-day forecasts were ever visible to a 05:00 entry).

Two layers of protection:
  1. config.local_today() does the UTC+8 date arithmetic correctly,
     pinned exactly at the window boundaries that were silently wrong.
  2. No trading-path module may call date.today() at all -- the same
     grep-the-source guardrail style as the backtest's gate census,
     because this bug pattern (someone reaching for the "obvious"
     stdlib call) will otherwise creep back in.
"""

import ast
import re
from datetime import date, datetime, timezone
from pathlib import Path

import config

PKG = Path(__file__).resolve().parent.parent


class _FrozenDatetime:
    """Stand-in for config's datetime: .now(tz) returns a fixed instant."""

    def __init__(self, frozen: datetime):
        self._frozen = frozen

    def now(self, tz=None):
        return self._frozen if tz is None else self._frozen.astimezone(tz)


def _local_today_at(monkeypatch, utc_iso: str) -> date:
    frozen = datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
    monkeypatch.setattr(config, "datetime", _FrozenDatetime(frozen))
    return config.local_today()


def test_primary_window_is_local_day_not_utc_day(monkeypatch):
    # 21:30 UTC Aug 1 == 05:30 SGT Aug 2: the heart of the primary entry
    # window, and exactly where date.today() said "Aug 1".
    assert _local_today_at(monkeypatch, "2026-08-01T21:30:00") == date(2026, 8, 2)


def test_boundaries_around_sgt_midnight(monkeypatch):
    # 15:59 UTC == 23:59 SGT same evening; 16:00 UTC == 00:00 SGT next day.
    assert _local_today_at(monkeypatch, "2026-08-01T15:59:59") == date(2026, 8, 1)
    assert _local_today_at(monkeypatch, "2026-08-01T16:00:00") == date(2026, 8, 2)


def test_afternoon_agrees_with_utc(monkeypatch):
    # 08:00-15:59 UTC the two calendars agree -- the bug was invisible here,
    # which is why a full clean paper day could pass without tripping it.
    assert _local_today_at(monkeypatch, "2026-08-01T09:00:00") == date(2026, 8, 1)


# Modules whose notion of "today" is the trading day. models/storage are
# excluded (no clock); backtest/ has its own SimClock discipline and an
# equivalent determinism guard in its own test files.
TRADING_PATH_FILES = [
    "scheduler.py",
    "pipeline.py",
    "position_manager.py",
    "entry_manager.py",
    "executor.py",
    "ev_engine.py",
    "market_discovery.py",
    "calibration.py",
    "clients/openmeteo_client.py",
    "clients/climate_monitor_client.py",
    "clients/official/nea.py",
    "clients/official/met_malaysia.py",
]


def test_no_module_calls_date_today():
    offenders = []
    for rel in TRADING_PATH_FILES:
        src = (PKG / rel).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "today"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "date.today() (or *.today()) found in trading-path modules -- on a "
        f"UTC host this is yesterday during the morning entry window: {offenders}"
    )


def test_local_today_matches_scheduler_local_now_offset():
    # The two clock helpers must agree on the offset, or windows and
    # target dates drift apart again in a subtler way.
    assert config.LOCAL_UTC_OFFSET_HOURS == 8
    src = (PKG / "scheduler.py").read_text(encoding="utf-8")
    m = re.search(r"def local_now\(tz_offset_hours: int = (\d+)\)", src)
    assert m and int(m.group(1)) == config.LOCAL_UTC_OFFSET_HOURS
