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
METAR timestamps are UTC; the market's day is local to EACH station --
the registry now spans UTC+5 (Karachi) through UTC+9 (Japan/Korea), so
the local-day window is computed per station, not off one global offset.
Local day D at offset H spans UTC [D-1 24-H:00, D 24-H:00). daily_max_temp_c()
buckets strictly by that window, and refuses to answer for a day with too
few reports -- a half-covered day would silently understate the maximum,
which is worse than no answer.

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

# Per-station throttle for ingest_missing_recent(): one sweep per local
# day per STATION is enough, the readings are immutable history. This
# used to be a single module-global date, which is wrong once the
# registry spans multiple timezones: the first timezone group to run in
# a process (e.g. Tokyo/Seoul/Busan at UTC+9) would set the global date
# and silently starve every other group's ingest for the rest of the
# process, since their own local "today" never matches the date the
# first group already consumed. Keyed per ICAO and compared against
# that STATION's own local_today() instead.
_last_ingest_by_station: Dict[str, date] = {}


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


def _local_day_window_utc(local_day: date, utc_offset_hours: int) -> Tuple[int, int]:
    """
    Unix [start, end) of a local calendar day at the given fixed UTC
    offset. Required param, not a global default -- the registry spans
    UTC+5 through UTC+9, and silently reusing one station's offset for
    another would mislabel which METARs fall inside "today".
    """
    start = datetime(
        local_day.year, local_day.month, local_day.day, tzinfo=timezone.utc
    ) - timedelta(hours=utc_offset_hours)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def daily_max_temp_c(
    metars: List[Tuple[int, float]],
    local_day: date,
    utc_offset_hours: int = config.LOCAL_UTC_OFFSET_HOURS,
    min_reports: int = MIN_REPORTS_PER_DAY,
) -> Optional[float]:
    """
    Maximum temperature among reports inside local_day's UTC window at
    the station's own utc_offset_hours, or None if coverage is too thin
    to trust. Pure -- callers fetch. Defaults to the legacy UTC+8 offset
    so existing station-agnostic callers/tests keep working unchanged.
    """
    start, end = _local_day_window_utc(local_day, utc_offset_hours)
    temps = [t for ts, t in metars if start <= ts < end]
    if len(temps) < min_reports:
        return None
    return max(temps)


def ingest_missing_recent(station_icaos: List[str], days_back: int = 3) -> int:
    """
    Save daily-max observations for any COMPLETED local day in the last
    `days_back` days that doesn't have one yet, for each station's OWN
    local calendar (see the per-station throttle note above) and per
    that station's metar_ingest_mode (StationConfig):
      - "resolution" -> saved as config.RESOLUTION_GRADE_OBSERVATION_SOURCE
        ("metar_daily_max"), i.e. settlement-grade truth.
      - "proxy"      -> saved as "metar_daily_max_proxy" (rank 1, usable
        for calibration, never picked as settlement truth). Karachi: the
        market names "Masroor Airbase" but the linked page is OPKC --
        unconfirmed identity, so it doesn't get to claim resolution grade.
      - "skip"       -> nothing ingested at all. Hong Kong: the airport
        reading is a biased proxy for the HKO settlement station and
        would corrupt the observation blend rather than help it.
    One API call per station covers the whole span. Every failure is
    contained per-station (this runs inside live trading cycles and must
    never break one -- one station's bad data or a source outage can't
    take down ingest for the rest of the registry). Returns rows saved.
    """
    saved = 0
    for icao in station_icaos:
        try:
            station = config.get_station(icao)
            today = config.local_today(station)

            if _last_ingest_by_station.get(icao) == today:
                continue
            _last_ingest_by_station[icao] = today

            if station.metar_ingest_mode == "skip":
                print(f"[metar_client] {icao}: metar_ingest_mode='skip' -- not ingesting METAR "
                      f"(settlement source for this station is not the airport record).")
                continue

            source = (
                config.RESOLUTION_GRADE_OBSERVATION_SOURCE
                if station.metar_ingest_mode == "resolution"
                else "metar_daily_max_proxy"
            )

            days = [today - timedelta(days=i) for i in range(1, days_back + 1)]
            # Dedup must match the SOURCE this sweep would actually save --
            # otherwise a proxy-mode station would see a prior resolution-
            # grade row (or vice versa) as "already covered" and skip a day
            # it hasn't actually ingested under its own source string yet.
            existing = {
                o.target_date
                for o in storage.load_observations_since(icao, min(days))
                if o.source == source
            }
            missing = [d for d in days if d not in existing]
            if not missing:
                continue

            # The LIVE offset, not the static winter field. station.utc_offset_hours
            # is STANDARD time by design (see StationConfig.iana_timezone); building
            # a local-day window on it shifts the window an hour for every
            # DST-observing station for most of the year, which attributes
            # 23:00-00:00 local on D-1 to day D. That is a whole observation in the
            # wrong day, and the daily MAX is what settles the market.
            #
            # config.local_day_bounds_utc() already resolves the offset AT the
            # day being bounded (not at "now") -- reuse it here for the same
            # reason its own docstring gives: `missing` can span up to
            # `days_back` historical days, and a DST transition inside that
            # span means no single offset is correct for all of them.
            window_start = config.local_day_bounds_utc(station, min(missing))[0].timestamp()
            hours = int((datetime.now(timezone.utc).timestamp() - window_start) / 3600) + 2
            metars = fetch_metars(icao, hours=hours)
            for d in missing:
                # Per-day offset, anchored at day d's own UTC midnight -- NOT
                # the single `window_start` offset above and NOT "now". A
                # station is only ~3 days behind "now" here, but the days in
                # `missing` can straddle a DST transition, and each day's
                # attribution must use the offset that was in effect ON that
                # day (same reasoning as config.local_day_bounds_utc).
                day_offset = config.current_utc_offset_hours(
                    station, at=datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                )
                max_c = daily_max_temp_c(metars, d, day_offset)
                if max_c is None:
                    print(f"[metar_client] {icao} {d}: insufficient METAR coverage, not saving a daily max.")
                    continue
                storage.save_observation(ObservedReading(
                    station_icao=icao,
                    target_date=d,
                    max_temp_c=max_c,
                    source=source,
                ))
                print(f"[metar_client] {icao} {d}: daily max {max_c:.1f}°C saved (source={source}).")
                saved += 1
        except Exception as exc:  # noqa: BLE001 - must never break a trading cycle
            print(f"[metar_client] ingest failed for {icao} (continuing): {exc}")
    return saved
