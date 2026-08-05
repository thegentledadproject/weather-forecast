"""
clients/official/met_malaysia.py

PURPOSE
-------
Adapter for Malaysian stations (e.g. WMKK/Kuala Lumpur). Unlike NEA,
MET Malaysia does not publish a data.gov.sg-equivalent open API with
clean JSON endpoints (confirmed during framework research). Rather
than scrape met.gov.my's HTML district-forecast pages -- fragile and
likely to break -- this adapter uses the WMO's World Weather
Information Service (worldweather.wmo.int), which republishes each
national met service's official forecast in a stable JSON format and
explicitly covers Kuala Lumpur.

This is intentionally a lighter implementation than NEAClient:
  - get_24hr_forecast(): implemented via WWIS's per-city JSON feed.
  - get_same_day_signal(): WWIS doesn't publish area-level nowcast
    text the way NEA does, so this returns an honest "not available"
    string rather than faking a signal. A real Malaysia-specific
    nowcast would need a different source (e.g. met.gov.my radar) --
    left as a documented gap, not silently skipped.
  - get_live_reading(): WWIS publishes forecasts, not live station
    observations, so this also returns None. A live-reading source
    for Malaysia is a good candidate for the next adapter improvement.

This file is the template to copy when adding the NEXT new station's
country/source: implement what the source actually supports, return
None/an honest string for what it doesn't, and let pipeline.py's
existing fallback logic handle the rest.

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

WWIS_CITY_LIST_URL = "https://worldweather.wmo.int/en/json/full_city_list.txt"
WWIS_CITY_FORECAST_URL_TEMPLATE = "https://worldweather.wmo.int/en/json/{city_id}_en.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class METMalaysiaClient(OfficialClient):
    """
    Malaysia official-source adapter, via WMO WWIS. Works for any
    Malaysian StationConfig whose display_name's city is listed in
    WWIS's city index (confirmed available for Kuala Lumpur).
    """

    def __init__(self):
        self._city_id_cache = {}

    def _resolve_city_id(self, city_name: str, timeout: int = 10) -> Optional[str]:
        """Look up WWIS's internal city_id for a given city name. Cached per-process."""
        if city_name in self._city_id_cache:
            return self._city_id_cache[city_name]
        try:
            resp = requests.get(WWIS_CITY_LIST_URL, timeout=timeout)
            resp.raise_for_status()
            # Confirmed live format (2026-08-02): semicolon-separated with
            # every field double-quoted, header row included:
            #   "Country";"City";"CityId"
            #   "Malaysia";"Kuala Lumpur";"82"
            # City is field 1 and the id is field 2 -- an earlier version
            # took field 0 (the COUNTRY, quotes and all) as the id and
            # requested /json/%22Malaysia%22_en.json, a guaranteed 404.
            for line in resp.text.splitlines():
                parts = [p.strip().strip('"') for p in line.split(";")]
                if len(parts) >= 3 and parts[1].lower() == city_name.lower():
                    self._city_id_cache[city_name] = parts[2]
                    return parts[2]
            print(f"[METMalaysiaClient] city '{city_name}' not found in WWIS city list")
            return None
        except requests.RequestException as exc:
            print(f"[METMalaysiaClient] city lookup failed: {exc}")
            return None

    def get_24hr_forecast(self, station: StationConfig, timeout: int = 10) -> Optional[PointForecast]:
        city_name = station.display_name.split(" International")[0].replace(" Airport", "")
        # StationConfig.display_name for WMKK is "Kuala Lumpur International Airport";
        # WWIS lists the city as "Kuala Lumpur", so trim to that.
        city_name = "Kuala Lumpur" if "Kuala Lumpur" in station.display_name else city_name

        city_id = self._resolve_city_id(city_name, timeout=timeout)
        if city_id is None:
            print(f"[METMalaysiaClient] could not resolve WWIS city_id for '{city_name}'")
            return None

        try:
            url = WWIS_CITY_FORECAST_URL_TEMPLATE.format(city_id=city_id)
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            # WWIS JSON shape: city -> forecast -> forecastDay -> [{maxTemp, ...}, ...]
            forecast_days = payload["city"]["forecast"]["forecastDay"]
            today_entry = forecast_days[0]  # first entry is typically "today"
            max_c = today_entry.get("maxTemp")

            return PointForecast(
                station_icao=station.icao,
                source="wwis_met_malaysia",
                target_date=config.local_today(station),
                max_temp_c=float(max_c) if max_c is not None else None,
                fetched_at=_now_iso(),
                raw_note=today_entry.get("weather", ""),
            )
        except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
            print(f"[METMalaysiaClient] get_24hr_forecast failed: {exc}")
            return None

    def get_same_day_signal(self, station: StationConfig) -> str:
        return (
            "Same-day area-level nowcast not implemented for Malaysia yet -- "
            "WWIS publishes daily forecasts, not nowcast text. Consider "
            "met.gov.my radar for a real Step-E signal here."
        )

    def get_live_reading(self, station: StationConfig) -> Optional[float]:
        return None  # WWIS is forecast-only; no live-observation source wired up yet.
