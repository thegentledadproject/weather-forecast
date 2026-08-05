"""
tests/test_resolution_source.py

Regression tests for the resolution-source mismatch fix (2026-08-02):
Polymarket settles on Wunderground's station history, which is the
airport METAR record; the backtest was settling (and scoring Brier)
against whatever sat in the observations table -- Open-Meteo model
analysis after the backfill. Covered here:

  - clients/metar_client.py buckets METARs into the correct UTC+8 local
    day (the 16:00Z boundary) and refuses thin coverage.
  - config.observation_source_rank orders METAR > other fetched > seed.
  - storage.dedupe_observations keeps exactly one reading per day, best
    source first -- so calibration's observed mean can never count one
    day twice just because two sources reported it.
  - metar_client.ingest_missing_recent is idempotent per local day and
    skips days that already have a METAR row.
"""

from datetime import date, datetime, timedelta, timezone

import config
import storage
from clients import metar_client
from models import ObservedReading


def _ts(iso_utc: str) -> int:
    return int(datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc).timestamp())


def _day_of_metars(local_day: date, temps, start_hour_local=0, step_min=30):
    """Synthetic half-hourly METARs inside local_day's window."""
    base = _ts(f"{(local_day - timedelta(days=1)).isoformat()}T16:00:00") + start_hour_local * 3600
    return [(base + i * step_min * 60, float(t)) for i, t in enumerate(temps)]


class TestDailyMaxWindowing:
    def test_max_taken_only_inside_local_day(self):
        day = date(2026, 8, 1)
        metars = _day_of_metars(day, [28] * 40 + [31.0] + [28] * 7)
        # A hotter report one second BEFORE the local day begins (15:59:59Z
        # on Jul 31) must not leak in.
        metars.append((_ts("2026-07-31T15:59:59"), 35.0))
        # ...nor one at exactly 16:00:00Z on Aug 1 (start of Aug 2 local).
        metars.append((_ts("2026-08-01T16:00:00"), 36.0))
        assert metar_client.daily_max_temp_c(sorted(metars), day) == 31.0

    def test_thin_coverage_returns_none(self):
        day = date(2026, 8, 1)
        metars = _day_of_metars(day, [30.0] * (metar_client.MIN_REPORTS_PER_DAY - 1))
        assert metar_client.daily_max_temp_c(metars, day) is None


class TestSourceRanking:
    def test_rank_order(self):
        r = config.observation_source_rank
        assert r("metar_daily_max") < r("openmeteo_recent_actual") < r("seed_data")

    def test_dedupe_keeps_best_source_per_day(self):
        day = date(2026, 8, 1)

        def obs(source, temp):
            return ObservedReading("WSSS", day, temp, source)

        deduped = storage.dedupe_observations([
            obs("seed_data", 30.0),
            obs("openmeteo_recent_actual", 31.6),
            obs("metar_daily_max", 32.0),
            ObservedReading("WSSS", date(2026, 7, 31), 31.0, "openmeteo_recent_actual"),
        ])
        assert len(deduped) == 2
        by_day = {o.target_date: o for o in deduped}
        assert by_day[day].source == "metar_daily_max"
        assert by_day[day].max_temp_c == 32.0
        assert by_day[date(2026, 7, 31)].source == "openmeteo_recent_actual"


class TestIngest:
    def test_skips_days_already_ingested_and_throttles(self, monkeypatch):
        monkeypatch.setattr(metar_client, "_last_ingest_by_station", {})
        today = config.local_today()
        yesterday = today - timedelta(days=1)
        two_ago = today - timedelta(days=2)
        three_ago = today - timedelta(days=3)

        # yesterday already has a METAR row; the two older days do not.
        monkeypatch.setattr(storage, "load_observations_since", lambda icao, cutoff: [
            ObservedReading("WSSS", yesterday, 31.0, config.RESOLUTION_GRADE_OBSERVATION_SOURCE),
        ])
        saved = []
        monkeypatch.setattr(storage, "save_observation", lambda o: saved.append(o))

        metars = _day_of_metars(two_ago, [29.0] * 48) + _day_of_metars(three_ago, [30.5] * 48)
        fetches = []
        monkeypatch.setattr(metar_client, "fetch_metars", lambda icao, hours, timeout=15: (fetches.append(hours), metars)[1])

        n = metar_client.ingest_missing_recent(["WSSS"], days_back=3)
        assert n == 2
        assert {(o.target_date, o.max_temp_c) for o in saved} == {(two_ago, 29.0), (three_ago, 30.5)}
        assert all(o.source == config.RESOLUTION_GRADE_OBSERVATION_SOURCE for o in saved)

        # Second sweep in the same process/day: throttled, no fetch, no saves.
        n2 = metar_client.ingest_missing_recent(["WSSS"], days_back=3)
        assert n2 == 0
        assert len(fetches) == 1
