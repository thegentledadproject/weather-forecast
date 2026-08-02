"""
clients/metar_client.py

PURPOSE
-------
Ingest the RESOLUTION-GRADE daily maximum temperature for any station:
the airport METAR record, fetched from aviationweather.gov's public data
API. Polymarket settles these markets on Wunderground's station history,
and Wunderground's history *is* the METAR record for these airports --
so this client closes the resolution-source mismatch that the backtest
build flagged: settling simulated positions (and scoring Brier) against
Open-Meteo model analysis could disagree with the real settlement on
boundary-adjacent days.

Unlike clients/wunderground_client.py (JS-rendered pages, explicitly
best-effort), aviationweather.gov serves clean JSON and is the primary
distribution channel for METARs -- reliable enough to automate.

DAY BOUNDARY
------------
METAR timestamps are UTC; the market's day is local (UTC+8). Local day D
spans UTC [D-1 16:00, D 16:00). daily_max_temp_c() buckets strictly by
that window, and refuses to answer for a day with too few reports --
a half-covered day would silently understate the maximum, which is
worse than no answer.

DEPENDENCIES
------------
requests; config.py, models.py, storage.py (local)
"""

from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

import config
import storage
from models import StationConfig, ObservedReading

API_URL = "https://aviationweather.gov/api/data/metar"

# A tropical airport files METARs at least half-hourly (~48/day). Below
# this many reports in the local-day window, the daily max is not
# trustworthy -- decline rather than under-report.
MIN_REPORTS_PER_DAY = 24

# Module-level throttle for ingest_missing_recent(): one sweep per local
# day per process is enough, the readings are immutable history.
_last_ingest_local_date: Optional[date] = None


def fetch_metars(icao: str, hours: int, timeout: int = 15) -> List[Tuple[int, float]]:
    """
    (obs_unix_ts, temp_c) pairs for the last `hours` hours, oldest first.
    Reports without a temperature are skipped. Returns [] on any failure
    (fail-soft, matching the other clients' stance).
    """
    try:
        resp = requests.get(
            API_URL,
            params={"ids": icao, "format": "json", "hours": hours},
            timeout=timeout,
            headers={"User-Agent": "polyweather/1.0"},
        )
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[metar_client] fetch failed for {icao}: {exc}")
        return []

    out = []
    for row in rows if isinstance(rows, list) else []:
        temp = row.get("temp")
        ts = row.get("obsTime")
        if temp is None or ts is None:
            continue
        try:
            out.append((int(ts), float(temp)))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _local_day_window_utc(local_day: date) -> Tuple[int, int]:
    """Unix [start, end) of a UTC+8 local calendar day."""
    start = datetime(
        local_day.year, local_day.month, local_day.day, tzinfo=timezone.utc
    ) - timedelta(hours=config.LOCAL_UTC_OFFSET_HOURS)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def daily_max_temp_c(
    metars: List[Tuple[int, float]],
    local_day: date,
    min_reports: int = MIN_REPORTS_PER_DAY,
) -> Optional[float]:
    """
    Maximum temperature among reports inside local_day's UTC window, or
    None if coverage is too thin to trust. Pure -- callers fetch.
    """
    start, end = _local_day_window_utc(local_day)
    temps = [t for ts, t in metars if start <= ts < end]
    if len(temps) < min_reports:
        return None
    return max(temps)


def ingest_missing_recent(station_icaos: List[str], days_back: int = 3) -> int:
    """
    Save metar_daily_max observations for any COMPLETED local day in the
    last `days_back` days that doesn't have one yet. One API call per
    station covers the whole span. Self-throttles to one sweep per local
    day per process; every failure is contained (this runs inside live
    trading cycles and must never break one). Returns rows saved.
    """
    global _last_ingest_local_date
    today = config.local_today()
    if _last_ingest_local_date == today:
        return 0
    _last_ingest_local_date = today

    saved = 0
    days = [today - timedelta(days=i) for i in range(1, days_back + 1)]
    for icao in station_icaos:
        try:
            existing = {
                o.target_date
                for o in storage.load_observations_since(icao, min(days))
                if o.source == config.RESOLUTION_GRADE_OBSERVATION_SOURCE
            }
            missing = [d for d in days if d not in existing]
            if not missing:
                continue

            hours = int((datetime.now(timezone.utc).timestamp() - _local_day_window_utc(min(missing))[0]) / 3600) + 2
            metars = fetch_metars(icao, hours=hours)
            for d in missing:
                max_c = daily_max_temp_c(metars, d)
                if max_c is None:
                    print(f"[metar_client] {icao} {d}: insufficient METAR coverage, not saving a daily max.")
                    continue
                storage.save_observation(ObservedReading(
                    station_icao=icao,
                    target_date=d,
                    max_temp_c=max_c,
                    source=config.RESOLUTION_GRADE_OBSERVATION_SOURCE,
                ))
                print(f"[metar_client] {icao} {d}: daily max {max_c:.1f}°C saved (resolution-grade).")
                saved += 1
        except Exception as exc:  # noqa: BLE001 - must never break a trading cycle
            print(f"[metar_client] ingest failed for {icao} (continuing): {exc}")
    return saved
