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

A third layer was added for the 2026-08-05 station expansion:
config.local_today() now ALSO accepts a station (StationConfig or ICAO
str), and every station-context caller on the trading path must pass
its own station -- the registry spans UTC+5 (Karachi) through UTC+9
(Japan/Korea), so the zero-arg legacy UTC+8 default is silently wrong
for eleven of the thirteen registered stations. test_no_module_calls_date_today
below therefore also flags a bare `config.local_today()` call (no
arguments at all) in a trading-path module. An EXPLICIT `None` argument
(or any other argument -- a station variable, a fallback expression) is
not a violation: position_manager.py's fallback path passes
`_station_for(position)`, which legitimately evaluates to None for an
orphaned/unregistered station and is a deliberate, visible choice, not
someone forgetting to pass a station at all.
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
#
# wwis.py/hko.py (generic + HK official-source adapters) and
# snapshot_collector.py were added alongside the 2026-08-05 station
# expansion; generate_dashboard.py lives one level up, in deploy/ next to
# (not inside) the weather-forecast/ package -- see PKG below.
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
    "clients/official/wwis.py",
    "clients/official/hko.py",
    "backtest/snapshot_collector.py",
    "../deploy/generate_dashboard.py",
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


def test_no_module_calls_zero_arg_local_today():
    """
    config.local_today() with NO arguments at all defaults to the legacy
    UTC+8 offset -- silently wrong for the 11 non-Singapore/KL stations
    now in the registry (UTC+9 Japan/Korea, UTC+5 Karachi). Every
    station-context call on the trading path must pass its station
    (a StationConfig, an ICAO string, or an explicit fallback expression
    that MAY evaluate to None -- see position_manager.py's
    `config.local_today(_station_for(position))`). Only the bare,
    argument-free call is the violation this guards against.
    """
    offenders = []
    for rel in TRADING_PATH_FILES:
        src = (PKG / rel).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "local_today"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "config"
                and not node.args
                and not node.keywords
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "zero-arg config.local_today() found in trading-path modules -- this "
        "silently keeps the legacy UTC+8 default for stations that are not "
        f"UTC+8, mislabeling their target date for hours every day: {offenders}"
    )


def test_local_today_matches_scheduler_local_now_offset():
    # The two clock helpers must agree on the offset, or windows and
    # target dates drift apart again in a subtler way.
    assert config.LOCAL_UTC_OFFSET_HOURS == 8
    src = (PKG / "scheduler.py").read_text(encoding="utf-8")
    m = re.search(r"def local_now\(tz_offset_hours: int = (\d+)\)", src)
    assert m and int(m.group(1)) == config.LOCAL_UTC_OFFSET_HOURS
