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


def _fetch_daily_max(url: str, station: StationConfig, source_label: str, timeout: int = 10) -> Optional[PointForecast]:
    """Shared logic for ECMWF/GFS single-model daily-max fetch, for a given station."""
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
        today_str = date.today().isoformat()

        if today_str not in dates:
            return None
        idx = dates.index(today_str)

        return PointForecast(
            station_icao=station.icao,
            source=source_label,
            target_date=date.today(),
            max_temp_c=float(maxes[idx]),
            fetched_at=_now_iso(),
        )
    except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
        print(f"[openmeteo_client] {source_label} fetch failed for {station.icao}: {exc}")
        return None


def get_ecmwf_forecast(station: StationConfig) -> Optional[PointForecast]:
    """Fetch today's ECMWF IFS HRES daily-max forecast at the station's coordinates."""
    return _fetch_daily_max(config.OPEN_METEO_ECMWF_URL, station, "open_meteo_ecmwf")


def get_gfs_forecast(station: StationConfig) -> Optional[PointForecast]:
    """Fetch today's GFS daily-max forecast at the station's coordinates (cross-check)."""
    return _fetch_daily_max(config.OPEN_METEO_GFS_URL, station, "open_meteo_gfs")


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
