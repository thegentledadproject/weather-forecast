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

from datetime import datetime, date, time, timezone, timedelta
from typing import Optional

import requests

import config
from models import StationConfig, PointForecast
from clients.official.base import OfficialClient

# data.gov.sg v2 real-time API. Keyless is fine at our volume (6 calls per
# 10 s per IP); an x-api-key header raises that, add one if we ever hit 429.
NEA_24HR_FORECAST_URL = "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast"
NEA_2HR_NOWCAST_URL = "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast"
NEA_AIR_TEMPERATURE_URL = "https://api-open.data.gov.sg/v2/real-time/api/air-temperature"

SGT = timezone(timedelta(hours=8))  # Singapore: UTC+8, no DST


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The daytime high falls in the local afternoon, so a 24-hour forecast is
# stamped with the SGT day whose whole 12:00-18:00 its valid period covers.
# The 00:00/06:00/12:00 issues are today; the 18:00 (evening) issue is
# TOMORROW -- the 20:00 SGT day-ahead recorder sees that one. Never stamp
# local_today blindly: that filed tomorrow's high as today's nea_24hr row.
# Same rule as met_malaysia.py's forecastDate match.
AFTERNOON_LOCAL = (time(12), time(18))


def _sgt(stamp: str) -> datetime:
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return t.replace(tzinfo=SGT) if t.tzinfo is None else t.astimezone(SGT)


def _target_date(valid_period) -> Optional[date]:
    """The SGT day whose afternoon the period covers, or None if it covers none."""
    start, end = _sgt(valid_period["start"]), _sgt(valid_period["end"])
    day = start.date()
    while datetime.combine(day, AFTERNOON_LOCAL[1], SGT) <= end:
        if datetime.combine(day, AFTERNOON_LOCAL[0], SGT) >= start:
            return day
        day += timedelta(days=1)
    return None


class NEAClient(OfficialClient):
    """Singapore official-source adapter. Works for any Singapore StationConfig."""

    def get_24hr_forecast(self, station: StationConfig, timeout: int = 10) -> Optional[PointForecast]:
        try:
            resp = requests.get(NEA_24HR_FORECAST_URL, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            # v2 shape: data -> records -> [{general: {validPeriod, temperature, forecast}}].
            # records[0]["date"] is the ISSUE date, not the forecast day.
            general = payload["data"]["records"][0]["general"]
            target = _target_date(general["validPeriod"])
            if target is None:
                print(f"[NEAClient] {station.icao}: validPeriod {general['validPeriod']} covers no afternoon")
                return None
            max_c = general["temperature"].get("high")

            # Source string stays "nea_24hr" so stored history stays joined
            # (backtest/engine.py maps "nea" -> "nea_24hr").
            return PointForecast(
                station_icao=station.icao,
                source="nea_24hr",
                target_date=target,
                max_temp_c=float(max_c) if max_c not in (None, "") else None,
                fetched_at=_now_iso(),
                raw_note=general.get("forecast", {}).get("text", ""),
            )
        except (requests.RequestException, KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
            print(f"[NEAClient] get_24hr_forecast failed: {exc}")
            return None

    def get_same_day_signal(self, station: StationConfig, timeout: int = 10) -> str:
        try:
            resp = requests.get(NEA_2HR_NOWCAST_URL, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            forecasts = payload["data"]["items"][0]["forecasts"]
            # Match on the station's display name area keyword (e.g. "Changi").
            keyword = station.display_name.split()[-2].lower() if len(station.display_name.split()) > 1 else station.display_name.lower()
            for entry in forecasts:
                if keyword in entry.get("area", "").lower():
                    return f"{entry.get('area')} 2hr nowcast: {entry.get('forecast', 'n/a')}"
            return f"'{keyword}' not found in current nowcast areas."
        except (requests.RequestException, KeyError, IndexError, TypeError) as exc:
            print(f"[NEAClient] get_same_day_signal failed: {exc}")
            return "No live nowcast available."

    def get_live_reading(self, station: StationConfig, timeout: int = 10) -> Optional[float]:
        # No callers. NEA street-station air temperature (Changi Met Station
        # S24), NOT the airport METAR that settles the market -- don't use
        # it as a settlement proxy.
        try:
            resp = requests.get(NEA_AIR_TEMPERATURE_URL, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            stations = payload["data"]["stations"]
            readings = payload["data"]["readings"][0]["data"]

            keyword = station.display_name.split()[-2].lower() if len(station.display_name.split()) > 1 else station.display_name.lower()
            target_id = None
            for st in stations:
                if keyword in st.get("name", "").lower():
                    target_id = st["id"]
                    break
            if target_id is None:
                return None

            for r in readings:
                if r["stationId"] == target_id:
                    return float(r["value"])
            return None
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as exc:
            print(f"[NEAClient] get_live_reading failed: {exc}")
            return None
