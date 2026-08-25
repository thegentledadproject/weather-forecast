"""
clients/official/wwis.py

PURPOSE
-------
Generic official-source adapter for any station whose national met
service is republished by the WMO's World Weather Information Service
(worldweather.wmo.int) -- the common case across the 2026-08-05 Asia
expansion: Tokyo, Seoul/Incheon, Busan, Manila, Shanghai, Beijing,
Guangzhou, Shenzhen, and Karachi all resolve through WWIS.

Generalizes clients/official/met_malaysia.py's WWIS plumbing to any
StationConfig via station.wwis_city_name, instead of one hardcoded
Malaysia-only client. met_malaysia.py is DELIBERATELY left unchanged:
its stored rows use the source string "wwis_met_malaysia", and renaming
or replacing it would orphan WMKK's existing history against the new
"wwis" source string (see design doc E3). WMKK keeps using
METMalaysiaClient; every station registered against THIS client uses
official_client_key="wwis".

CITY LIST CACHING
------------------
met_malaysia.py re-downloads the full WWIS city list on every cache
miss (i.e. once per distinct city name never seen before in that
process). With ten WWIS-backed stations in the registry that would be
up to ten redundant ~hundred-KB downloads of the same static file in
one process. This client instead downloads the list exactly ONCE per
process -- a class-level cache shared by every WWISClient instance
(the registry only ever constructs one, but the cache is class-level
regardless so that invariant isn't load-bearing) -- and resolves every
station's city id against that single cached list.

DAY SELECTION
-------------
WWIS's forecastDay list is NOT reliably "today first" across timezones:
a station's local day and WWIS's own day-labeling can disagree near a
date boundary, and blindly taking forecastDay[0] would silently poison
calibration at the official forecast's 40% blend weight. This client
instead matches each entry's own "forecastDate" field (YYYY-MM-DD)
against the station's config.local_today(station), and returns None --
not a guess -- if no entry matches.

DEPENDENCIES
------------
requests   (pip install requests)
models.py, clients/official/base.py, config.py (local)
"""

from datetime import datetime, timezone
from typing import ClassVar, Dict, Optional

import requests

import config
from models import StationConfig, PointForecast
from clients.official.base import OfficialClient

WWIS_CITY_LIST_URL = "https://worldweather.wmo.int/en/json/full_city_list.txt"
WWIS_CITY_FORECAST_URL_TEMPLATE = "https://worldweather.wmo.int/en/json/{city_id}_en.json"

# Stable source string for EVERY station served by this client. A
# per-country suffix (e.g. "wwis_japan") would break the backtest's
# OFFICIAL_SOURCE_BY_CLIENT_KEY map, which keys one fixed source string
# per official_client_key (see design doc E4) -- and would also make
# observation_source_rank/dedup treat the same station's rows from two
# different WWIS fetches as two different sources.
SOURCE = "wwis"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WWISClient(OfficialClient):
    """
    Generic WMO WWIS adapter. Works for any StationConfig whose
    wwis_city_name is set to that city's exact WWIS listing (see
    config.py's per-station comments for cases where the WWIS city is a
    documented PROXY for the actual settlement station, e.g. RKSI's
    "Seoul" listing standing in for the Incheon airport ~50km away).
    Stations with wwis_city_name == "" (absent from WWIS entirely --
    Taiwan, a UN service, and London/EGLC, verified absent from the WWIS
    city list) get an honest None from get_24hr_forecast instead of a
    guess.
    """

    # Class-level, not instance-level: the full city list is a single
    # static file shared by every station this client serves, so it is
    # downloaded at most once per process regardless of how many
    # distinct cities get resolved against it or how many WWISClient
    # instances exist.
    _city_list_cache: ClassVar[Optional[Dict[str, str]]] = None

    def _load_city_list(self, timeout: int = 10) -> Dict[str, str]:
        """
        Download and parse the full WWIS city list exactly once per
        process; every later call (any instance) reuses the cached
        dict. Returns {city_name_lower: city_id}. Empty dict on
        failure -- callers then treat every city as "not found", which
        is the same honest None outcome as a partial/corrupt list.
        """
        if WWISClient._city_list_cache is not None:
            return WWISClient._city_list_cache

        city_ids: Dict[str, str] = {}
        try:
            resp = requests.get(WWIS_CITY_LIST_URL, timeout=timeout)
            resp.raise_for_status()
            # Confirmed live format (2026-08-05): semicolon-separated
            # with every field double-quoted, header row included:
            #   "Country";"City";"CityId"
            #   "Japan";"Tokyo";"183"
            # City is field 1, id is field 2 -- see met_malaysia.py's
            # note on the earlier bug that took field 0 (the quoted
            # COUNTRY) as the id, a guaranteed 404.
            for line in resp.text.splitlines():
                parts = [p.strip().strip('"') for p in line.split(";")]
                if len(parts) >= 3:
                    city_ids[parts[1].lower()] = parts[2]
        except requests.RequestException as exc:
            print(f"[WWISClient] city list download failed: {exc}")

        WWISClient._city_list_cache = city_ids
        return city_ids

    def _resolve_city_id(self, city_name: str, timeout: int = 10) -> Optional[str]:
        city_ids = self._load_city_list(timeout=timeout)
        city_id = city_ids.get(city_name.lower())
        if city_id is None:
            print(f"[WWISClient] city '{city_name}' not found in WWIS city list")
        return city_id

    def get_24hr_forecast(self, station: StationConfig, timeout: int = 10) -> Optional[PointForecast]:
        if not station.wwis_city_name:
            print(
                f"[WWISClient] {station.icao}: wwis_city_name is empty -- this station's city "
                f"is not listed in WWIS (e.g. Taipei -- WWIS is a UN service and does not cover "
                f"Taiwan). Returning None honestly rather than guessing a forecast."
            )
            return None

        city_id = self._resolve_city_id(station.wwis_city_name, timeout=timeout)
        if city_id is None:
            print(f"[WWISClient] {station.icao}: could not resolve WWIS city_id for '{station.wwis_city_name}'")
            return None

        try:
            url = WWIS_CITY_FORECAST_URL_TEMPLATE.format(city_id=city_id)
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            # WWIS JSON shape: city -> forecast -> forecastDay -> [{forecastDate, maxTemp, ...}, ...]
            forecast_days = payload["city"]["forecast"]["forecastDay"]
            target = config.local_today(station)
            target_str = target.isoformat()

            # NEVER take forecast_days[0] blindly -- see module docstring.
            # Match forecastDate explicitly instead.
            entry = None
            for day in forecast_days:
                if day.get("forecastDate") == target_str:
                    entry = day
                    break
            if entry is None:
                available = [d.get("forecastDate") for d in forecast_days]
                print(
                    f"[WWISClient] {station.icao}: no forecastDay entry matched {target_str} "
                    f"(city={station.wwis_city_name}); available dates: {available}"
                )
                return None

            # maxTemp is sometimes missing/empty in the live feed -- treat
            # that as "no number" rather than letting float() raise.
            raw_max = entry.get("maxTemp")
            max_c: Optional[float] = None
            if raw_max not in (None, ""):
                try:
                    max_c = float(raw_max)
                except (TypeError, ValueError):
                    max_c = None

            return PointForecast(
                station_icao=station.icao,
                source=SOURCE,
                target_date=target,
                max_temp_c=max_c,
                fetched_at=_now_iso(),
                raw_note=entry.get("weather", ""),
            )
        except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
            print(f"[WWISClient] {station.icao}: get_24hr_forecast failed: {exc}")
            return None

    def get_same_day_signal(self, station: StationConfig) -> str:
        return (
            "Same-day area-level nowcast not available via WWIS -- it publishes daily "
            "forecasts only, not nowcast/area text. A country-specific radar/nowcast "
            "source would be needed for a real Step-E signal for this station."
        )

    def get_live_reading(self, station: StationConfig) -> Optional[float]:
        return None  # WWIS is forecast-only; no live-observation source wired up here.
