"""
tests/test_scheduler_groups.py

Regression tests for the multi-timezone scheduler grouping added in the
2026-08-05 station expansion (design doc C3): the registry now spans
UTC+5 (Karachi) through UTC+9 (Japan/Korea), so scheduler.py groups
stations by utc_offset_hours and evaluates config.SCHEDULE_WINDOWS
against each group's OWN local clock rather than one shared "local" time.

Covers:
  - scheduler.stations_by_utc_offset() groups exactly per the registry's
    utc_offset_hours values.
  - determine_window() stays pure (hour/minute in, window out -- no
    station or clock lookups) and produces genuinely different windows
    for two groups at the same real instant.
"""

from datetime import datetime, timezone

import config
import scheduler


class _FrozenDatetime:
    """Stand-in for scheduler's datetime: .now(tz) returns a fixed instant."""

    def __init__(self, frozen: datetime):
        self._frozen = frozen

    def now(self, tz=None):
        return self._frozen if tz is None else self._frozen.astimezone(tz)


def test_stations_group_by_utc_offset_matching_the_registry():
    groups = scheduler.stations_by_utc_offset()

    assert set(groups) == {5, 8, 9}

    expected = {5: [], 8: [], 9: []}
    for icao, st in config.STATIONS.items():
        expected[st.utc_offset_hours].append(icao)

    # Order within a group: scheduler iterates config.STATIONS in its own
    # insertion order, same as the expected dict built the same way here.
    assert {k: sorted(v) for k, v in groups.items()} == {k: sorted(v) for k, v in expected.items()}


def test_every_registered_station_appears_in_exactly_one_group():
    groups = scheduler.stations_by_utc_offset()
    all_icaos = [icao for icaos in groups.values() for icao in icaos]
    assert sorted(all_icaos) == sorted(config.STATIONS)
    assert len(all_icaos) == len(set(all_icaos))


def test_unknown_station_is_skipped_not_raised():
    groups = scheduler.stations_by_utc_offset(["WSSS", "NOT-A-REAL-ICAO"])
    all_icaos = [icao for icaos in groups.values() for icao in icaos]
    assert all_icaos == ["WSSS"]


def test_determine_window_is_pure_hour_minute_in_window_out():
    # Same (hour, minute) in, same window out -- no station, no clock,
    # no hidden state. Calling it twice must be idempotent.
    a = scheduler.determine_window(5, 30)
    b = scheduler.determine_window(5, 30)
    assert a == b
    assert a["mode"] == "primary"


def test_utc9_primary_window_is_utc5_closed_window_at_the_same_instant():
    # When Tokyo/Seoul/Busan (UTC+9) are at 05:30 local -- deep in their
    # primary entry window -- the same real instant is 01:30 local in
    # Karachi (UTC+5): 09:00 - (9 - 5) = 05:30 - 4h = 01:30, inside the
    # explicit 00:00-04:00 "closed" floor. A single shared clock would
    # either run Karachi four hours early or delay Tokyo's primary window
    # by the same margin -- grouping by offset is what keeps both honest.
    utc9_hour, utc9_minute = 5, 30
    offset_delta = 9 - 5
    utc5_total_minutes = (utc9_hour * 60 + utc9_minute - offset_delta * 60) % (24 * 60)
    utc5_hour, utc5_minute = divmod(utc5_total_minutes, 60)

    utc9_window = scheduler.determine_window(utc9_hour, utc9_minute)
    utc5_window = scheduler.determine_window(utc5_hour, utc5_minute)

    assert utc9_window["mode"] == "primary"
    assert utc5_window["mode"] == "closed"
    assert utc9_window != utc5_window


def test_local_now_reflects_the_passed_offset(monkeypatch):
    # scheduler.local_now() takes the group's own offset -- two different
    # offsets read at the SAME frozen instant must disagree by exactly
    # their offset delta, mod 24h. Frozen (not read twice off the real
    # wall clock) so the assertion can't flake across a minute boundary.
    frozen = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "datetime", _FrozenDatetime(frozen))

    hour9, minute9 = scheduler.local_now(9)
    hour5, minute5 = scheduler.local_now(5)
    total9 = hour9 * 60 + minute9
    total5 = hour5 * 60 + minute5
    assert (total9 - total5) % (24 * 60) == 4 * 60
