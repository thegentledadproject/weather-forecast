"""
clients/official/hko.py

PURPOSE
-------
Adapter for the Hong Kong Observatory (station VHHH in this codebase's
registry, though VHHH is really used as a key/reference here -- the
market settles on the Observatory's own settlement station, not the
Chek Lap Kok airport; see config.py's VHHH entry for why). Uses HKO's
public "Open Data API" (data.weather.gov.hk), which serves clean JSON
with no key required. All endpoints below were verified live against
the real API on 2026-08-05.

Three live-data endpoints (weather.php, dataType=...):
  - fnd      -- 9-day forecast, including forecastMaxtemp per day.
  - rhrread  -- current/latest readings across HKO's station network,
                including temperature.data keyed by place name.
  - flw      -- "Local Weather Forecast" bulletin text (forecastDesc +
                generalSituation).

Plus one CLIMATE dataset (opendata.php, dataType=CLMMAXT) for
settlement-grade history: HKO's published "Daily Maximum Temperature"
record. This dataset LAGS the current date -- confirmed live
2026-08-05 that BOTH July and the in-progress August 2026 returned
zero rows, with data published only through 2026-06-30 -- so
ingest_missing_recent() below treats an empty month as a normal,
expected gap and fails soft rather than erroring.

DEPENDENCIES
------------
requests   (pip install requests)
models.py, clients/official/base.py, config.py, storage.py (local)
"""

from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

import config
import storage
from models import StationConfig, PointForecast, ObservedReading
from clients.official.base import OfficialClient

HKO_WEATHER_API_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
HKO_CLIMATE_API_URL = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"

# The place name HKO's own APIs use for its principal settlement
# station, as returned live in both rhrread's temperature.data and
# (implicitly) the climate record's "HKO" station code.
OBSERVATORY_PLACE_NAME = "Hong Kong Observatory"

# How much of the flw bulletin's free text to keep -- these can run to
# several paragraphs (tropical cyclone updates etc.); a same-day signal
# is meant to be a short human-readable string, not the full bulletin.
_SIGNAL_MAX_CHARS = 400

# Per-station throttle for ingest_missing_recent(), same shape and
# reasoning as clients/metar_client.py's: one sweep per local day per
# station is enough, readings are immutable history.
_last_ingest_by_station: Dict[str, date] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HKOClient(OfficialClient):
    """Hong Kong Observatory official-source adapter."""

    def get_24hr_forecast(self, station: StationConfig, timeout: int = 10) -> Optional[PointForecast]:
        try:
            resp = requests.get(
                HKO_WEATHER_API_URL, params={"dataType": "fnd", "lang": "en"}, timeout=timeout
            )
            resp.raise_for_status()
            payload = resp.json()

            forecasts = payload["weatherForecast"]
            target_str = config.local_today(station).strftime("%Y%m%d")

            entry = None
            for day in forecasts:
                if day.get("forecastDate") == target_str:
                    entry = day
                    break
            if entry is None:
                available = [d.get("forecastDate") for d in forecasts]
                print(f"[HKOClient] {station.icao}: no fnd entry matched {target_str}; available: {available}")
                return None

            max_temp = entry.get("forecastMaxtemp") or {}
            max_c = max_temp.get("value")

            return PointForecast(
                station_icao=station.icao,
                source="hko_fnd",
                target_date=config.local_today(station),
                max_temp_c=float(max_c) if max_c is not None else None,
                fetched_at=_now_iso(),
                raw_note=entry.get("forecastWeather", ""),
            )
        except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
            print(f"[HKOClient] {station.icao}: get_24hr_forecast failed: {exc}")
            return None

    def get_live_reading(self, station: StationConfig, timeout: int = 10) -> Optional[float]:
        try:
            resp = requests.get(
                HKO_WEATHER_API_URL, params={"dataType": "rhrread", "lang": "en"}, timeout=timeout
            )
            resp.raise_for_status()
            payload = resp.json()

            readings = payload["temperature"]["data"]
            for r in readings:
                if r.get("place") == OBSERVATORY_PLACE_NAME:
                    return float(r["value"])
            print(f"[HKOClient] {station.icao}: '{OBSERVATORY_PLACE_NAME}' not present in rhrread temperature.data")
            return None
        except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
            print(f"[HKOClient] {station.icao}: get_live_reading failed: {exc}")
            return None

    def get_same_day_signal(self, station: StationConfig, timeout: int = 10) -> str:
        try:
            resp = requests.get(
                HKO_WEATHER_API_URL, params={"dataType": "flw", "lang": "en"}, timeout=timeout
            )
            resp.raise_for_status()
            payload = resp.json()

            desc = payload.get("forecastDesc", "")
            general = payload.get("generalSituation", "")
            text = desc
            if general:
                text = f"{desc} | General situation: {general}" if desc else general
            if not text:
                return "HKO flw bulletin returned no forecastDesc/generalSituation text."
            if len(text) > _SIGNAL_MAX_CHARS:
                text = text[:_SIGNAL_MAX_CHARS].rstrip() + "..."
            return text
        except (requests.RequestException, KeyError, ValueError) as exc:
            print(f"[HKOClient] {station.icao}: get_same_day_signal failed: {exc}")
            return "No live HKO same-day signal available."


def _fetch_clmmaxt_rows(year: int, month: Optional[int], timeout: int = 15) -> Dict[Tuple[int, int, int], float]:
    """
    Fetch HKO's CLMMAXT (Daily Maximum Temperature) rows for one
    (year, month), or a whole year if month is None. Returns
    {(year, month, day): value_c}. Empty dict on ANY failure or an
    empty published dataset -- this climate dataset genuinely lags the
    current date (confirmed live 2026-08-05: both the in-progress
    August and the already-complete July returned zero rows, with data
    published only through 2026-06-30), so an empty result here is a
    normal, expected outcome for recent months, not an error to raise.
    """
    params = {"dataType": "CLMMAXT", "rformat": "json", "station": "HKO", "year": str(year)}
    if month is not None:
        params["month"] = str(month)
    try:
        resp = requests.get(HKO_CLIMATE_API_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        # "fields" (confirmed live): ["Year","Month","Day","Value","data Completeness"].
        # Parse positionally by index rather than trusting field NAMES
        # match exactly (the live payload's field labels are bilingual
        # and slash-separated, not stable keys to match against).
        rows = payload.get("data", [])
        out: Dict[Tuple[int, int, int], float] = {}
        for row in rows:
            try:
                y, m, d, v = int(row[0]), int(row[1]), int(row[2]), float(row[3])
            except (IndexError, TypeError, ValueError):
                continue
            out[(y, m, d)] = v
        return out
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"[HKOClient] CLMMAXT fetch failed (year={year}, month={month}): {exc}")
        return {}


def ingest_missing_recent(station_icaos: List[str], days_back: int = None) -> int:
    """
    Save "hko_daily_max" observations for any COMPLETED local day in
    the last `days_back` days that doesn't have one yet, for stations
    whose resolution_grade_source == "hko_daily_max" (VHHH today).
    Every other station is a silent no-op, so this can be called
    unconditionally alongside metar_client.ingest_missing_recent() in
    the same scheduler pass without an extra station filter at the
    call site.

    Mirrors metar_client.ingest_missing_recent()'s shape: per-station
    throttle (one sweep per local day per station), one save per
    missing day, every failure contained per-station (this runs inside
    live trading cycles and must never break one). Returns rows saved.

    HKO's CLMMAXT climate dataset is NOT live -- it lags the current
    date, sometimes by more than a full month (see _fetch_clmmaxt_rows
    docstring). A day whose month isn't published yet is therefore an
    honest gap: this fails soft, logs clearly, and saves nothing for
    that day rather than guessing or raising.

    THE LOOKBACK MUST BE LONGER THAN THAT LAG, which is why days_back
    defaults to config.HKO_INGEST_LOOKBACK_DAYS (75) and not to
    metar_client's 3. METAR is minutes old; CLMMAXT publishes a whole
    month at a time, weeks in arrears. A 3-day window and a 46-day lag
    never overlap, so the sweep saved nothing for as long as it ran --
    see the constant's note. Most of the span is normally already
    ingested, so the extra reach costs one pass over cached month rows,
    not one request per day.
    """
    if days_back is None:
        days_back = config.HKO_INGEST_LOOKBACK_DAYS
    saved = 0
    for icao in station_icaos:
        try:
            station = config.get_station(icao)
            if station.resolution_grade_source != "hko_daily_max":
                continue

            today = config.local_today(station)
            if _last_ingest_by_station.get(icao) == today:
                continue
            _last_ingest_by_station[icao] = today

            days = [today - timedelta(days=i) for i in range(1, days_back + 1)]
            existing = {
                o.target_date
                for o in storage.load_observations_since(icao, min(days))
                if o.source == "hko_daily_max"
            }
            missing = [d for d in days if d not in existing]
            if not missing:
                continue

            # Cache per (year, month) so a days_back span within one
            # month issues one request pair total, not one per day.
            # Probe order per design doc: month-scoped query first,
            # then fall back to a year-only query (occasionally the
            # two granularities disagree on what's published).
            rows_cache: Dict[Tuple[int, int], Dict[Tuple[int, int, int], float]] = {}

            def rows_for(year: int, month: int) -> Dict[Tuple[int, int, int], float]:
                key = (year, month)
                if key in rows_cache:
                    return rows_cache[key]
                rows = _fetch_clmmaxt_rows(year, month)
                if not rows:
                    year_rows = _fetch_clmmaxt_rows(year, None)
                    rows = {k: v for k, v in year_rows.items() if k[1] == month}
                rows_cache[key] = rows
                return rows

            # Unpublished days are summarised PER MONTH, not per day. At a
            # 3-day lookback one line per gap was fine; at 75 it would print
            # ~46 identical "not published yet" lines every sweep and bury
            # the saves -- and the whole reason this window is long is that
            # most of it is expected to be unpublished or already stored.
            unpublished_by_month: Dict[Tuple[int, int], int] = {}
            for d in missing:
                day_rows = rows_for(d.year, d.month)
                max_c = day_rows.get((d.year, d.month, d.day))
                if max_c is None:
                    key = (d.year, d.month)
                    unpublished_by_month[key] = unpublished_by_month.get(key, 0) + 1
                    continue
                storage.save_observation(ObservedReading(
                    station_icao=icao,
                    target_date=d,
                    max_temp_c=max_c,
                    source="hko_daily_max",
                ))
                print(f"[HKOClient] {icao} {d}: daily max {max_c:.1f}°C saved (source=hko_daily_max).")
                saved += 1

            for (year, month), n in sorted(unpublished_by_month.items()):
                print(
                    f"[HKOClient] {icao}: {n} day(s) in {year}-{month:02d} have no published "
                    f"CLMMAXT daily max yet (HKO's climate dataset lags) -- not saving."
                )
        except Exception as exc:  # noqa: BLE001 - must never break a trading cycle
            print(f"[HKOClient] ingest failed for {icao} (continuing): {exc}")
    return saved
