"""
clients/official/nea.py

PURPOSE
-------
Adapter for Singapore's National Environment Agency (NEA) official
data.gov.sg APIs. Implements OfficialClient for any Singapore station
registered in config.STATIONS (currently WSSS/Changi). This is the
Tier 2 (verified-accessible) source referenced throughout the
framework doc.

DEPENDENCIES
------------
requests   (pip install requests)
models.py, clients/official/base.py (local)
"""

from datetime import datetime, date, timezone
from typing import Optional

import requests


import config
from models import StationConfig, PointForecast
from clients.official.base import OfficialClient

NEA_24HR_FORECAST_URL = "https://api.data.gov.sg/v1/environment/24-hour-weather-forecast"
NEA_2HR_NOWCAST_URL = "https://api.data.gov.sg/v1/environment/2-hour-weather-forecast"
NEA_AIR_TEMPERATURE_URL = "https://api.data.gov.sg/v1/environment/air-temperature"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NEAClient(OfficialClient):
    """Singapore official-source adapter. Works for any Singapore StationConfig."""

    def get_24hr_forecast(self, station: StationConfig, timeout: int = 10) -> Optional[PointForecast]:
        try:
            resp = requests.get(NEA_24HR_FORECAST_URL, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            record = payload["items"][0]
            general = record.get("general", {})
            temp = general.get("temperature", {})
            forecast_text = general.get("forecast", "")
            max_c = temp.get("high")

            return PointForecast(
                station_icao=station.icao,
                source="nea_24hr",
                target_date=config.local_today(station),
                max_temp_c=float(max_c) if max_c is not None else None,
                fetched_at=_now_iso(),
                raw_note=forecast_text,
            )
        except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
            print(f"[NEAClient] get_24hr_forecast failed: {exc}")
            return None

    def get_same_day_signal(self, station: StationConfig, timeout: int = 10) -> str:
        try:
            resp = requests.get(NEA_2HR_NOWCAST_URL, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            forecasts = payload["items"][0]["forecasts"]
            # Match on the station's display name area keyword (e.g. "Changi").
            keyword = station.display_name.split()[-2].lower() if len(station.display_name.split()) > 1 else station.display_name.lower()
            for entry in forecasts:
                if keyword in entry.get("area", "").lower():
                    return f"{entry.get('area')} 2hr nowcast: {entry.get('forecast', 'n/a')}"
            return f"'{keyword}' not found in current nowcast areas."
        except (requests.RequestException, KeyError, IndexError) as exc:
            print(f"[NEAClient] get_same_day_signal failed: {exc}")
            return "No live nowcast available."

    def get_live_reading(self, station: StationConfig, timeout: int = 10) -> Optional[float]:
        try:
            resp = requests.get(NEA_AIR_TEMPERATURE_URL, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            stations = payload["metadata"]["stations"]
            readings = payload["items"][0]["readings"]

            keyword = station.display_name.split()[-2].lower() if len(station.display_name.split()) > 1 else station.display_name.lower()
            target_id = None
            for st in stations:
                if keyword in st.get("name", "").lower():
                    target_id = st["id"]
                    break
            if target_id is None:
                return None

            for r in readings:
                if r["station_id"] == target_id:
                    return float(r["value"])
            return None
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            print(f"[NEAClient] get_live_reading failed: {exc}")
            return None
