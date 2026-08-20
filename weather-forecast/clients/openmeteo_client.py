"""
clients/openmeteo_client.py

PURPOSE
-------
Tier 1 data source (per framework doc): direct ECMWF IFS / GFS model
output via Open-Meteo's free API. Station-agnostic by design -- every
function takes a StationConfig and queries that station's exact
lat/lon, so it works unchanged for WSSS, WMKK, or any future station.
Fails soft (returns None) so the pipeline can fall back to Tier 2.

DEPENDENCIES
------------
requests   (pip install requests)
config.py, models.py (local)
"""

from datetime import datetime, date, timezone
from typing import Optional, List

import requests

import config
from models import StationConfig, PointForecast


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_daily_max_series(
    url: str, station: StationConfig, source_label: str, timeout: int = 10
) -> List[PointForecast]:
    """
    Shared logic for the ECMWF/GFS single-model daily-max fetch: ONE
    PointForecast per forecast day the API returned, from local_today
    forward, in date order.

    THE EXTRA DAYS WERE ALWAYS BEING PAID FOR. `forecast_days: 3` has
    been in this request from the start and the response was indexed for
    today alone, so tomorrow and the day after were fetched and thrown
    away on every cycle at every station. The cost of keeping them is one
    extra row per source per cycle; the cost of discarding them was that
    every stored forecast sat in a 14-19h lead band, which made "does
    forecast lead time affect the error spread?" unanswerable from this
    database -- see spread_audit.py, which ran on 2026-08-21 and could
    only report that there was no lead to vary.

    PAST DATES ARE DROPPED, not stored. Open-Meteo's window is built from
    the coordinates' own timezone and can open a day before
    config.local_today(station) at the boundary; such a row is an
    OBSERVATION wearing a forecast's source name, and storing it would put
    a value with perfect hindsight into the bias sample.

    The caller is responsible for not BLENDING the day-ahead rows --
    pipeline.gather_forecasts() stores the series and returns only
    today's, and storage._forecast_means_in_local_day() keeps them out of
    the measured bias by the same local-day rule.
    """
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "forecast_days": 3,
    }
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()

        dates = payload["daily"]["time"]
        maxes = payload["daily"]["temperature_2m_max"]
        # timezone="auto" makes Open-Meteo return dates already in the
        # STATION's own local calendar (per its lat/lon) -- indexing
        # with the global UTC+8 local_today() would silently mismatch
        # for +9 (Japan/Korea) and +5 (Karachi) stations for part of
        # each day, losing the Tier-1 forecast exactly when it's needed.
        today = config.local_today(station)
        fetched_at = _now_iso()

        series = []
        for date_str, value in zip(dates, maxes):
            if value is None:
                continue
            target_date = date.fromisoformat(date_str)
            if target_date < today:
                continue
            series.append(
                PointForecast(
                    station_icao=station.icao,
                    source=source_label,
                    target_date=target_date,
                    max_temp_c=float(value),
                    fetched_at=fetched_at,
                )
            )
        return series
    except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
        print(f"[openmeteo_client] {source_label} fetch failed for {station.icao}: {exc}")
        return []


def get_ecmwf_forecast_series(station: StationConfig) -> List[PointForecast]:
    """ECMWF IFS HRES daily maxima at the station's coordinates, today forward."""
    return _fetch_daily_max_series(
        config.OPEN_METEO_ECMWF_URL, station, "open_meteo_ecmwf"
    )


def get_gfs_forecast_series(station: StationConfig) -> List[PointForecast]:
    """GFS daily maxima at the station's coordinates, today forward (cross-check)."""
    return _fetch_daily_max_series(
        config.OPEN_METEO_GFS_URL, station, "open_meteo_gfs"
    )


def get_ensemble_spread(station: StationConfig, timeout: int = 10) -> Optional[List[float]]:
    """
    Fetch ECMWF ensemble member daily-max values for today, at the
    station's coordinates. Returns a list of per-member max temps
    (degrees C), or None on failure.
    """
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "forecast_days": 1,
        # Model id on the ensemble host is plain "ecmwf_ifs025" -- the
        # "_ensemble" suffix 404s (confirmed against the live API 2026-08-02).
        "models": "ecmwf_ifs025",
    }
    try:
        resp = requests.get(config.OPEN_METEO_ENSEMBLE_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        daily = payload.get("daily", {})
        members = [
            v[0] for k, v in daily.items()
            if k.startswith("temperature_2m_max") and v
        ]
        return members if members else None
    except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
        print(f"[openmeteo_client] get_ensemble_spread failed for {station.icao}: {exc}")
        return None
