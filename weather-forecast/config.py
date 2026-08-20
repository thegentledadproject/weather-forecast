"""
config.py

PURPOSE
-------
Station registry + shared constants. This is THE file you touch to
add a new station/market. Everything else in the codebase looks up
a StationConfig by ICAO code and stays generic from there.

To add a new station (e.g. WMKK):
  1. Add a STATIONS["WMKK"] = StationConfig(...) entry below.
  2. If its official forecast source doesn't have an adapter yet,
     add one in clients/official/ (see clients/official/base.py for
     the interface) and register it in clients/official/registry.py.
  3. Nothing else needs to change -- pipeline.py, calibration.py,
     probability.py, storage.py are all station-parameterized already.

DEPENDENCIES
------------
None besides models.py (standard library otherwise).
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

from models import StationConfig

# --- Trading-day clock ----------------------------------------------------
# Every station's market day is LOCAL to that station. The deployment box
# runs on UTC, where date.today() is still YESTERDAY for the first hours of
# the local day -- including the entire 05:00-08:00 primary entry window.
# Every forecast fetched in that window was being labeled with the previous
# day's date, and the trading cycle was calibrating for (and discovering the
# market of) a day that had already ended. Any code that needs "today" in
# the trading sense MUST use local_today(), never date.today() -- and any
# code that KNOWS which station it is working for must pass that station,
# because the registry now spans UTC+5 (Karachi) through UTC+9 (Japan/Korea).
# The zero-arg form keeps the legacy UTC+8 behaviour for station-agnostic
# callers and old tests.
LOCAL_UTC_OFFSET_HOURS = 8


def local_today(station: Optional[Union[str, StationConfig]] = None) -> date:
    """
    The current calendar date in a station's market timezone. Accepts a
    StationConfig, an ICAO string, or None (legacy UTC+8 default -- only
    for genuinely station-agnostic contexts).
    """
    if station is None:
        offset = LOCAL_UTC_OFFSET_HOURS
    elif isinstance(station, str):
        offset = get_station(station).utc_offset_hours
    else:
        offset = station.utc_offset_hours
    return (datetime.now(timezone.utc) + timedelta(hours=offset)).date()


# --- Observation source ranking -------------------------------------------
# Most markets settle on Wunderground's station history, which is the
# airport METAR record ("metar_daily_max", ingested by clients/metar_client
# .py) -- but not all: Hong Kong settles on the HK Observatory's climate
# extract ("hko_daily_max"). Settlement-grade truth for THE STATION AT HAND
# must win, so ranking is parameterized by the station's own
# resolution_grade_source: that source first, then any other fetched reading
# (e.g. the Open-Meteo analysis backfill, or a proxy METAR), with the
# hand-maintained seed constants last. Used by resolution picking in the
# backtest AND by dedup before calibration blending -- two rows for one day
# would otherwise double-count it in the observed mean.
RESOLUTION_GRADE_OBSERVATION_SOURCE = "metar_daily_max"


def observation_source_rank(
    source: str,
    resolution_grade_source: str = RESOLUTION_GRADE_OBSERVATION_SOURCE,
) -> tuple:
    """Sort key: lower ranks win. Deterministic across ties via the name."""
    if source == resolution_grade_source:
        return (0, source)
    if source == "seed_data":
        return (2, source)
    return (1, source)

# --- Shared monsoon-phase lookup (reused across Southeast Asian stations) ---
# Coarse, deliberately simple for the MVP. A given station can override
# this per-entry below if its local seasonal pattern differs.
_SEA_MONSOON_PHASE_BY_MONTH = {
    12: "northeast_monsoon", 1: "northeast_monsoon", 2: "northeast_monsoon", 3: "northeast_monsoon",
    4: "inter_monsoon", 5: "inter_monsoon",
    6: "southwest_monsoon", 7: "southwest_monsoon", 8: "southwest_monsoon", 9: "southwest_monsoon",
    10: "inter_monsoon", 11: "inter_monsoon",
}

# --- Station registry -----------------------------------------------------
# Every Asian city with a live Polymarket "highest temperature" market as of
# 2026-08-05, verified against the Gamma API (event date 2026-08-06). Each
# entry's wunderground_slug, polymarket_city_slug, and bucket bounds were
# read from that city's real event; bounds DRIFT seasonally (see the
# bucket_min_c note on models.StationConfig), so the live token map remains
# authoritative on the trading path.
STATIONS = {
    "WSSS": StationConfig(
        icao="WSSS",
        display_name="Singapore Changi Airport",
        country="Singapore",
        lat=1.3644,
        lon=103.9915,
        wunderground_slug="sg/singapore/WSSS",
        long_term_normal_max_c=31.4,  # NEA 1991-2020 climatological reference, July
        official_client_key="nea",
        polymarket_city_slug="singapore",
        monsoon_phase_by_month=_SEA_MONSOON_PHASE_BY_MONTH,
        seed_observations=[
            # (date_iso, max_temp_c) -- from NEA's Jul 2026 fortnightly outlook review.
            # See clients/official/nea.py NEAClient for how this feeds calibration.
            ("2026-07-01", 32.1), ("2026-07-02", 29.9), ("2026-07-03", 31.5),
            ("2026-07-04", 33.1), ("2026-07-05", 33.3), ("2026-07-06", 32.3),
            ("2026-07-07", 32.9), ("2026-07-08", 32.2), ("2026-07-09", 31.4),
            ("2026-07-10", 32.9), ("2026-07-11", 31.5), ("2026-07-12", 30.7),
            ("2026-07-13", 32.0), ("2026-07-14", 32.7),
        ],
        # Was 25/35 ("confirmed" July 2026); the live August event runs 27-37.
        # Polymarket re-centers the window seasonally -- these are cross-checks,
        # the discovered token map decides at trade time.
        bucket_min_c=27,
        bucket_max_c=37,
    ),
    "WMKK": StationConfig(
        icao="WMKK",
        display_name="Kuala Lumpur International Airport",
        country="Malaysia",
        lat=2.7456,
        lon=101.7099,
        wunderground_slug="my/sepang-district/WMKK",
        long_term_normal_max_c=32.8,  # SOURCED: WMO 1991-2020 August mean daily max for
                                       # Kuala Lumpur city. WMKK/Sepang is ~50 km south
                                       # and more rural, so this is the loosest of the
                                       # sourced substitutions -- MET Malaysia's own
                                       # Sepang normals would be the exact figure.
        official_client_key="met_malaysia",
        polymarket_city_slug="kuala-lumpur",
        monsoon_phase_by_month=_SEA_MONSOON_PHASE_BY_MONTH,
        seed_observations=[],  # not yet populated -- see clients/official/met_malaysia.py
        bucket_min_c=27,  # live August 2026 event range (same drift caveat as WSSS)
        bucket_max_c=37,
    ),
    # --- Northeast Asia (UTC+9) -------------------------------------------
    # monsoon_phase_by_month deliberately {} for every station below: the
    # field feeds no calculation (it only annotates CalibratedEstimate), and
    # inventing regional season maps would manufacture authority the data
    # doesn't have. "unknown" is the honest value until someone does the work.
    "RJTT": StationConfig(
        icao="RJTT",
        display_name="Tokyo Haneda Airport",
        country="Japan",
        lat=35.5533,
        lon=139.7811,
        wunderground_slug="jp/tokyo/RJTT",
        long_term_normal_max_c=31.3,  # STILL A PLACEHOLDER (2026-08-09 sourcing pass).
                                       # JMA's 1991-2020 normal for the "Tokyo" station
                                       # (Otemachi) is ~32.6C, but that is an inland
                                       # urban-heat site and Haneda is on the bay --
                                       # substituting it would swap one error for a
                                       # bigger one. Needs JMA's Haneda (47671) normals.
        official_client_key="wwis",
        polymarket_city_slug="tokyo",
        utc_offset_hours=9,
        bucket_min_c=26,  # live 2026-08-06 event: 26 "or below" .. 36 "or higher"
        bucket_max_c=36,
        wwis_city_name="Tokyo",
    ),
    "RKSI": StationConfig(
        icao="RKSI",
        display_name="Incheon International Airport",
        country="South Korea",
        lat=37.4691,
        lon=126.4505,
        wunderground_slug="kr/incheon/RKSI",
        long_term_normal_max_c=29.2,  # SOURCED: KMA 1991-2020 August mean daily max for
                                       # Incheon city. RKSI is ~15 km west on Yeongjong
                                       # Island; both coastal, so a close stand-in.
        official_client_key="wwis",
        polymarket_city_slug="seoul",  # Polymarket titles this "Seoul (Incheon)"
        utc_offset_hours=9,
        bucket_min_c=27,  # live 2026-08-06 event: 27..37
        bucket_max_c=37,
        # WWIS lists "Seoul", not Incheon -- the city forecast is a PROXY for
        # the airport station ~50 km west on the coast. Documented gap, not a
        # silent equivalence; Open-Meteo runs at the airport's own lat/lon.
        wwis_city_name="Seoul",
    ),
    "RKPK": StationConfig(
        icao="RKPK",
        display_name="Busan Gimhae International Airport",
        country="South Korea",
        lat=35.1795,
        lon=128.9382,
        wunderground_slug="kr/busan/RKPK",
        long_term_normal_max_c=30.8,  # PLACEHOLDER KEPT ON PURPOSE (2026-08-09): KMA's
                                       # 1991-2020 Busan normal is 29.5C, but that is the
                                       # coastal city station and RKPK/Gimhae sits inland
                                       # up the Nakdong valley, reliably warmer. Adopting
                                       # 29.5 would bias this station cool. Needs Gimhae.
        official_client_key="wwis",
        polymarket_city_slug="busan",
        utc_offset_hours=9,
        bucket_min_c=30,  # live 2026-08-06 event: 30..40
        bucket_max_c=40,
        wwis_city_name="Busan",
    ),
    # --- Greater China + Southeast Asia (UTC+8) ---------------------------
    "VHHH": StationConfig(
        icao="VHHH",
        # Deliberately the OBSERVATORY, not the airport: this market settles
        # on the HK Observatory's own climate extract ("Absolute Daily Max",
        # 0.1 C precision), NOT on any Wunderground airport page. lat/lon
        # therefore point at the Observatory (urban Tsim Sha Tsui) so every
        # forecast targets the settlement site; the VHHH ICAO is kept only as
        # the registry key and for reference. The airport (Chek Lap Kok,
        # marine-exposed) reads systematically cooler than the Observatory's
        # heat island -- which is why metar_ingest_mode="skip": one biased
        # proxy reading in the 60%-weight observation blend shifts the
        # central estimate by whole buckets.
        display_name="Hong Kong Observatory",
        country="Hong Kong",
        lat=22.3020,
        lon=114.1741,
        wunderground_slug="hk/hong-kong/VHHH",  # proxy page only -- NOT the resolution source
        long_term_normal_max_c=31.3,  # SOURCED: HKO 1991-2020 August mean daily max.
                                       # EXACT for this station -- the market settles on
                                       # the Observatory's own record (resolution_grade_
                                       # source="hko_daily_max"), so this normal and the
                                       # settlement site are the same place.
        official_client_key="hko",
        polymarket_city_slug="hong-kong",
        bucket_min_c=27,  # live 2026-08-06 event: 27..37
        bucket_max_c=37,
        # 0.1 C settlement precision + "range that contains" resolution text
        # means floor semantics, not whole-degree rounding: 33.9 C is bucket
        # 33, never 34.
        bucket_edge_mode="floor",
        resolution_grade_source="hko_daily_max",
        metar_ingest_mode="skip",
    ),
    "RPLL": StationConfig(
        icao="RPLL",
        display_name="Manila Ninoy Aquino International Airport",
        country="Philippines",
        lat=14.5086,
        lon=121.0198,
        wunderground_slug="ph/manila/RPLL",
        long_term_normal_max_c=30.8,  # SOURCED: PAGASA 1991-2020 August mean daily max
                                       # for Ninoy Aquino Intl -- which IS this station
                                       # (RPLL), so no site substitution.
        official_client_key="wwis",
        polymarket_city_slug="manila",
        bucket_min_c=25,  # live 2026-08-06 event: 25..35
        bucket_max_c=35,
        wwis_city_name="Metro Manila",  # WWIS's exact listing (not "Manila")
    ),
    "RCSS": StationConfig(
        icao="RCSS",
        display_name="Taipei Songshan Airport",
        country="Taiwan",
        lat=25.0694,
        lon=121.5525,
        wunderground_slug="tw/taipei/RCSS",
        long_term_normal_max_c=34.2,  # SOURCED: CWB/CWA 1991-2020 August mean daily max
                                       # for Taipei. Songshan sits inside the city, so
                                       # the city normal is a close stand-in.
        official_client_key="wwis",
        polymarket_city_slug="taipei",
        bucket_min_c=28,  # live 2026-08-06 event: 28..38
        bucket_max_c=38,
        # Taiwan is absent from the WMO WWIS index (UN service), so there is
        # no official-source city to name -- the wwis client returns None
        # honestly and calibration runs on Open-Meteo + METAR alone. A CWA
        # (cwa.gov.tw) adapter is the documented next step if Taipei earns
        # real sizing.
        wwis_city_name="",
    ),
    "ZSPD": StationConfig(
        icao="ZSPD",
        display_name="Shanghai Pudong International Airport",
        country="China",
        lat=31.1443,
        lon=121.8083,
        wunderground_slug="cn/shanghai/ZSPD",
        long_term_normal_max_c=32.2,  # PLACEHOLDER KEPT ON PURPOSE (2026-08-09): CMA's
                                       # 1991-2020 Shanghai normal is 32.6C, but that is
                                       # Xujiahui -- a downtown heat-island site. Pudong
                                       # is coastal and runs cooler, so adopting 32.6
                                       # would move this the WRONG way. Needs Pudong.
        official_client_key="wwis",
        polymarket_city_slug="shanghai",
        bucket_min_c=27,  # live 2026-08-06 event: 27..37
        bucket_max_c=37,
        wwis_city_name="Shanghai",
    ),
    "ZBAA": StationConfig(
        icao="ZBAA",
        display_name="Beijing Capital International Airport",
        country="China",
        lat=40.0801,
        lon=116.5846,
        wunderground_slug="cn/beijing/ZBAA",
        long_term_normal_max_c=30.7,  # SOURCED: CMA 1991-2020 August mean daily max for
                                       # Beijing; Capital Airport is ~25 km NE of the
                                       # reference station.
        official_client_key="wwis",
        polymarket_city_slug="beijing",
        bucket_min_c=30,  # live 2026-08-06 event: 30..40
        bucket_max_c=40,
        wwis_city_name="Beijing",
    ),
    "ZGGG": StationConfig(
        icao="ZGGG",
        display_name="Guangzhou Baiyun International Airport",
        country="China",
        lat=23.3924,
        lon=113.2988,
        wunderground_slug="cn/guangzhou/ZGGG",
        long_term_normal_max_c=33.4,  # STILL A PLACEHOLDER (2026-08-09 sourcing pass):
                                       # no authoritative CMA 1991-2020 August normal
                                       # located for Guangzhou. Not guessed.
        official_client_key="wwis",
        polymarket_city_slug="guangzhou",
        bucket_min_c=29,  # live 2026-08-06 event: 29..39
        bucket_max_c=39,
        wwis_city_name="Guangzhou",
    ),
    "ZGSZ": StationConfig(
        icao="ZGSZ",
        display_name="Shenzhen Bao'an International Airport",
        country="China",
        lat=22.6393,
        lon=113.8108,
        wunderground_slug="cn/shenzhen/ZGSZ",
        long_term_normal_max_c=32.2,  # SOURCED: Shenzhen Meteorological Bureau 1991-2020
                                       # August mean daily max; Bao'an Airport is coastal
                                       # within the same city.
        official_client_key="wwis",
        polymarket_city_slug="shenzhen",
        bucket_min_c=28,  # live 2026-08-06 event: 28..38
        bucket_max_c=38,
        wwis_city_name="Shenzhen",
    ),
    # --- South Asia (UTC+5) -----------------------------------------------
    "OPKC": StationConfig(
        icao="OPKC",
        display_name="Karachi Jinnah International Airport",
        country="Pakistan",
        lat=24.9065,
        lon=67.1608,
        wunderground_slug="pk/karachi/OPKC",
        long_term_normal_max_c=32.5,  # SOURCED: 1991-2020 August mean daily max for
                                       # Karachi, whose official station is at Jinnah
                                       # Intl -- this station.
        official_client_key="wwis",
        polymarket_city_slug="karachi",
        utc_offset_hours=5,
        bucket_min_c=27,  # live 2026-08-06 event: 27..37
        bucket_max_c=37,
        wwis_city_name="Karachi",
        # The market's own resolution text names "Masroor Airbase Station"
        # (OPMR, ~15 km west across Karachi's sea-breeze gradient) while
        # linking Wunderground's OPKC page. Until someone confirms which
        # record Wunderground actually displays there, OPKC METAR is ingested
        # as PROXY grade only: usable as a rank-1 calibration input, never as
        # settlement truth -- so the backtest reports resolution-pending
        # instead of settling Karachi P&L on a maybe-wrong station.
        metar_ingest_mode="proxy",
    ),
}


def get_station(icao: str) -> StationConfig:
    """Look up a StationConfig by ICAO code. Raises KeyError with a clear message if unregistered."""
    icao = icao.upper()
    if icao not in STATIONS:
        raise KeyError(
            f"Station '{icao}' is not registered. Known stations: {list(STATIONS)}. "
            f"Add a StationConfig entry in config.STATIONS to support it."
        )
    return STATIONS[icao]


# --- Open-Meteo (Tier 1 model access, best-effort) ----------------------
# Station-agnostic endpoints -- lat/lon passed per-call from StationConfig.
OPEN_METEO_ECMWF_URL = "https://api.open-meteo.com/v1/ecmwf"
OPEN_METEO_GFS_URL = "https://api.open-meteo.com/v1/gfs"
# Ensemble runs live on their own host, NOT api.open-meteo.com -- the main
# host 404s the /v1/ensemble path (confirmed against the live API 2026-08-02).
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# --- Polymarket-style resolution buckets --------------------------------
# Every city's temperature event lists 11 outcomes -- "X or below", nine
# individual degrees, "Y or higher" -- but X/Y are PER-CITY and drift
# seasonally (verified 2026-08-05: Manila 25-35, Beijing 30-40, and
# Singapore itself moved 25-35 -> 27-37 since July). Per-station cross-check
# bounds live on StationConfig.bucket_min_c/max_c; the trading path derives
# the authoritative bounds from each cycle's discovered token map. These two
# globals remain ONLY as legacy defaults for station-agnostic signatures and
# old tests -- do not add new call sites.
BUCKET_MIN_C = 25
BUCKET_MAX_C = 35
# range(min, max+1) must yield exactly this many values for a well-formed
# event; discovery vetoes a station-day whose parsed map violates it.
EXPECTED_BUCKET_COUNT = 11

# --- Local storage --------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "polyweather.sqlite3"

# --- Position risk management --------------------------------------------
# Exit thresholds are intentionally asymmetric: take profit sooner than you
# accept loss, since the edge this system trades on is a morning-only
# phenomenon that decays through the day (see edge analysis) -- letting a
# winning position ride hoping for more upside fights the system's own
# timing thesis. Tune these per your own risk tolerance; these are starting
# defaults, not researched optima.
#
# THE UNIT THESE ARE MEASURED IN (changed 2026-08-09)
# ---------------------------------------------------
# Every threshold below is a fraction of the position's RISK UNIT --
# risk_manager.risk_unit() = min(entry_price, 1 - entry_price) -- not of
# entry price. The old entry-price basis broke down above 0.50 because
# price is capped at 1.00 while the thresholds are not:
#
#   PROFIT_TAKE_PCT 0.50 needed price >= 1.50 x entry, i.e. UNREACHABLE
#   for any entry above 0.667. Same arithmetic killed the tightened take
#   above 0.80 (and, while it existed, both trailing activations). An entry
#   at 0.85 therefore had NO upside exit of any kind -- only the stop-loss
#   and resolution could ever fire -- while carrying a 30% stop against a
#   maximum possible gain of +17.6%. The stop exceeded the entire
#   remaining upside for every entry above 0.769.
#
# min(entry, 1-entry) is the distance to the NEARER boundary, so a
# threshold expressed against it can never demand a price outside [0, 1].
# Below 0.50 the risk unit IS entry price, so every number here keeps its
# old meaning exactly: this reformulation is a no-op for entries at or
# below 0.50 and only changes the 0.50-0.75 band that was mispriced.
# (See MAX_ENTRY_PRICE, which caps entries at 0.75.)
PROFIT_TAKE_PCT = 0.50      # take profit once gain reaches +50% of the risk unit
STOP_LOSS_PCT = 0.30        # cut once loss reaches -30% of the risk unit

# After this local hour, tighten both thresholds (see risk_manager.py) --
# reflects the edge-decay curve: be quicker to lock in gains and quicker to
# cut losses, since there's no more new edge coming to justify holding
# through volatility.
#
# THIS USED TO SAY "once the primary trading window closes", and that phrasing
# was true only while entries also stopped at 10:00. They stop at 08:00 as of
# 2026-08-17 (see SCHEDULE_WINDOWS above), and this hour was deliberately NOT
# moved with them -- so the two are now two hours apart and the tightening is
# no longer keyed to the entry close. Kept at 10:00 because moving it is a
# question about EXITS that needs the capital a stop frees to be modeled;
# stop_loss_audit.py measures the tightening's cost but its held-to-settlement
# column is explicitly an upper bound and cannot answer it.
EDGE_DECAY_TIGHTEN_HOUR_LOCAL = 10  # 10:00 local, per the scanning-schedule analysis
TIGHTENED_PROFIT_TAKE_PCT = 0.25

# THE STOP IS NO LONGER TIGHTENED (2026-08-18). It was 0.15, i.e. half the
# loose distance, and stop_loss_audit.py scored every stop that fired only
# because of it: 23 such stops, realized -50.49 against -21.31 had they been
# held to settlement, and 7 of the 18 with settlement truth would have WON.
# Tightening the stop cost +21.49 USD and bought nothing measurable.
#
# THE TAKE-PROFIT TIGHTENING ABOVE IS DELIBERATELY UNCHANGED. The audit
# scores stops; it has nothing to say about exiting winners earlier, and
# "the stop half was not worth it" is not evidence about the other half.
# Changing both on one measurement would be borrowing the stop's evidence
# to justify a take-profit change nobody has measured.
#
# Written as a reference to STOP_LOSS_PCT, not a repeated 0.30, so the two
# cannot drift apart if the loose distance is ever retuned. That the wider
# 30% distance is itself expensive (+253 USD of the +278 total, see the
# audit) is a SEPARATE and much larger question, and needs the backtest
# sweep rather than a constant edited by hand.
TIGHTENED_STOP_LOSS_PCT = STOP_LOSS_PCT

# --- Lottery-priced positions (risk_manager.py, entry_manager.py) ----------
# Below this entry price, a percentage stop-loss is structurally meaningless:
# 30% of a $0.04 entry is 1.2 cents -- UNDER Polymarket's 1-cent tick -- so
# the smallest possible two-tick wobble on a near-empty book must blow
# through the stop. Live case (2026-08-02/03): WSSS 31°C YES bought 3x at
# $0.04-0.10 on a $14-deep book, stopped out every time within minutes-to-
# hours on 2-cent noise, once just 21 minutes after entry. A ticket like
# this is a hold-to-resolution bet: its max loss is its (Kelly-tiny) stake,
# already fully accepted at entry, and stopping it on price noise forfeits
# exactly the rare winning paths that justify buying it. Positions entered
# below this threshold therefore skip the percentage stop-loss entirely;
# the fixed profit-take and resolution detection still apply.
#
# (The trailing stop was exempted for the same reason from 2026-08-09;
# it was removed outright on 2026-08-17 -- see risk_manager.py's module
# docstring for the measurement.)
LOTTERY_PRICE_THRESHOLD = 0.15

# The take-profit distance for those same lottery-priced entries, as a
# fraction of the risk unit -- which below 0.50 IS the entry price, so 0.50
# means "sell at 1.5x entry" and 4.0 means "sell at 5x entry".
#
# SHIPPED AS A STRICT NO-OP: it defaults to PROFIT_TAKE_PCT, so today's
# behaviour is unchanged. It exists because the carve-out above is
# ONE-SIDED and nothing had measured whether it should be. The stop is
# exempted for lottery entries; the profit-take never was, so a 0.08 entry
# holds through its full 8c of downside while its upside is cut at 0.12 --
# a 4-cent move on a 1-cent tick, which is the same sub-tick argument that
# disqualified the stop. On a ticket bought at 0.08 against model_prob
# 0.267 the hold-to-par payoff is 12.5x, and +50% takes a thin slice of it.
#
# THE 2026-08-19 OBSERVATION THAT PROMPTED THIS, and its limits: WSSS
# 33 YES, entered 0.08, ordered sold SIX times at 0.12-0.13 by this very
# threshold, every one killed (thin book, then an exchange halt), and it
# reached 0.92. The leg made +$10.45 where the designed exit was +$0.55.
# That is ONE trade and it is the luckiest possible sample -- it is a
# reason to measure, not evidence of the right value.
#
# THE FIRST SWEEP SAYS THE ANECDOTE IS BACKWARDS. Over 2026-08-10..17,
# 13 stations, ~21 closed lottery positions per row, the lottery cohort
# scores MONOTONICALLY WORSE as the take widens: 10% -20.9%, 25% -31.1%,
# 50% (live) -38.2%, 100% -46.5%, 200% -65.5%, 400%/none -80.9%. Tighter
# is better everywhere tested, and every row is NEGATIVE.
#
# Do NOT act on that either. The window is 7/8 peak-blind (9-11h of price
# coverage per day), which under-triggers wide takes specifically and so
# biases the result in exactly the direction it came out. See
# backtest/take_sweep.py, which prints that coverage on every run and
# states the bar a recommendation has to clear.
LOTTERY_PROFIT_TAKE_PCT = PROFIT_TAKE_PCT

# The post-EDGE_DECAY_TIGHTEN_HOUR_LOCAL partner, exactly as
# TIGHTENED_PROFIT_TAKE_PCT is for the normal distance. Written as a
# reference so the two cannot drift apart -- and note THE ALIAS GOTCHA
# (backtest/stop_sweep.py): `=` binds the VALUE at import, so anything
# rebinding LOTTERY_PROFIT_TAKE_PCT at runtime must set this one too or it
# silently keeps the old number for half the trading day.
TIGHTENED_LOTTERY_PROFIT_TAKE_PCT = TIGHTENED_PROFIT_TAKE_PCT

# THE MIRROR OF LOTTERY_PRICE_THRESHOLD: entries AT OR ABOVE this price
# also skip the percentage stop-loss.
#
# The lottery carve-out exempts the cheap end because the stop distance
# there is sub-tick. This one exempts the expensive end for a different
# and directly measured reason: up here the stop is simply WRONG most of
# the times it fires.
#
# MEASURED 2026-08-20 on the paper book (2026-08-02..19), scoring every
# stop that fired against what the position was worth AT SETTLEMENT --
# "stop precision", the fraction of stops taken on positions that would
# have LOST anyway. A stop that fires on an eventual winner is a false
# positive. WMKK, the largest single-station book at 37 closed positions:
#
#     entry < 0.30        6 stops    83% precise
#     entry 0.30 - 0.45   3 stops    67% precise
#     entry >= 0.45       9 stops    33% precise
#
# and book-wide over all 11 stations the same fall, monotone:
# 0.15-0.30 78% (n=40), 0.30-0.45 62% (n=21), 0.45-0.60 44% (n=18),
# 0.60+ 29% (n=21). Ten of eleven stations show a gain from dropping the
# stop on the >= 0.45 cut; only RJTT does not (n=3, and RJTT is 0/7 at
# settlement over the whole window).
#
# The mechanism is the risk unit. A NO at 0.55 has a risk unit of 0.45,
# so STOP_LOSS_PCT buys a 13.5c stop distance -- well inside a bucket's
# ordinary intraday swing on a day whose maximum has not been set yet.
# A cheap YES that sags usually IS dying, because the day is drifting
# away from the bucket it needed; an expensive NO that sags is watching
# the temperature approach one bucket out of several it can still miss.
# Settlement is decided by observation, not by the price path, which is
# why this measurement needs no price history and is untouched by the
# price-store coverage gap that caveats every replay number.
#
# WHAT THIS IS WORTH, and it is NOT the headline. WMKK's nine stops at
# entry >= 0.45 realized -72.19 USD against +51.00 held to settlement,
# but "held" is not what removing a stop buys: those positions would
# still have hit their take-profit on the way up. Scoring winners at
# their take-profit target and losers at zero gives +47.64 (at today's
# tightened 0.25 take) to +71.37 (at the loose 0.50) on 176.21 USD of
# stake -- enough to move the WMKK book from -7.1% to roughly +4%.
#
# WHY 0.45 AND NOT 0.50. 0.50 is where the risk unit flips from entry
# price to (1 - entry), and putting the boundary exactly there would
# entangle this carve-out with that flip -- see [[risk-unit-thresholds]]
# and the no-op boundary it documents. 0.45 sits below it, is where the
# measured precision has already fallen under a coin flip, and is a
# band edge in the analysis rather than a fitted optimum. Nothing here
# separates 0.45 from 0.40 or 0.50; the evidence is the ORDERING.
#
# THIS DOES NOT CONTRADICT backtest/stop_sweep.py, whose first run said
# WMKK scores best at the TIGHTEST distance. That sweep varies one stop
# distance uniformly across all entry prices and re-decides its own
# entries over a peak-blind window. It never tested conditioning the
# stop on entry price, which is the only thing changed here.
#
# THE HONEST LIMITS: nine stops over seven days at WMKK, eighteen at
# 0.45-0.60 book-wide. A day-level bootstrap of the held delta on the
# WMKK cut is positive in every resample, but that interval is over the
# upper bound, not over what the change actually buys. Set this to 1.01
# to disable the carve-out entirely and restore the old behaviour.
STOP_EXEMPT_ABOVE_PRICE = 0.45

# Hard ceiling on entry price. Above this, a bought bucket stops behaving
# like a position and starts behaving like a bond with a stop-loss on it:
#
#   - Remaining upside is (1 - price), which at 0.85 is 15c against a
#     stake of 85c. You risk the whole stake to win 17.6%.
#   - MAX_PLAUSIBLE_RAW_EDGE cannot fire at all above price 0.75, because
#     raw edge <= 1 - price by construction (model_prob <= 1.0). The one
#     gate built to catch inverted-quote/wrong-token data errors is
#     structurally blind in exactly this band -- see
#     MAX_PLAUSIBLE_EDGE_HEADROOM_FRACTION, which fixes that too.
#   - Kelly's denominator is (1 - price), so sizing explodes toward the
#     per-trade cap as price -> 1: at 0.95 the SMALLEST legal edge
#     (MIN_ABS_RAW_EDGE) already pins MAX_POSITION_USD, and Kelly stops
#     discriminating on edge quality at all. See MIN_KELLY_DENOMINATOR.
#   - A 0.95 entry needs model_prob >= 0.98 to clear MIN_ABS_RAW_EDGE.
#     Tail probabilities are where a fallback-spread calibration is least
#     trustworthy, and this is where it would be sized largest.
#
# 0.75 is sited by the SECOND bullet alone: it is exactly where
# MAX_PLAUSIBLE_RAW_EDGE regains its teeth, because raw edge <= 1 - price
# makes a flat 0.25 ceiling unreachable above that point.
#
# It is NOT sited by the stop, though this comment used to say it was.
# The claim was that 0.75 is where the stop stops exceeding total
# available upside -- true only on the OLD entry-price basis, where
# 0.30 x entry crosses 1 - entry at 0.769. Measured against the risk unit
# this file actually uses, the stop distance is 0.30 x min(entry,
# 1 - entry) against upside 1 - entry, which is under upside at EVERY
# price: there is no crossover anywhere to site a ceiling at. Anyone
# re-deriving MAX_ENTRY_PRICE from the stop would get a different and
# wrong answer. (Corrected 2026-08-19. The wording also predated the
# stop-regime tightening being dropped -- TIGHTENED_STOP_LOSS_PCT is now
# just STOP_LOSS_PCT, so there is no "normal regime" to qualify.)
#
# Until now nothing bounded the top end at all: the
# only price screens were `market_price >= 1.0` and EV_MIN_PRICE_SCREEN
# at the bottom. Entries this expensive were kept out only ACCIDENTALLY,
# by net EV being a ratio -- (model_prob - price)/price has a ceiling of
# (1-price)/price, so a 15% EV bar is arithmetically impossible above
# 0.87 and a 25% bar above 0.80. That is a side effect of the metric, not
# a risk control, and it evaporates the moment min_net_ev is lowered.
MAX_ENTRY_PRICE = 0.75

# After this many stop-loss exits on the same (station, date, bucket, side),
# entries there are blocked for the rest of the day. The per-bucket open-
# position cap stops STACKING but has no memory of exits, so on 2026-08-03
# the same bucket ran a "stop -> re-buy -> stop" churn loop, paying the
# spread on every lap. One stop-out is the market's answer for the day.
MAX_STOP_OUTS_PER_BUCKET_PER_DAY = 1

# Which closed statuses count toward that cooldown. Trailing-stop exits
# were deliberately excluded on the reasoning that "a trailing stop is a
# winner giving back its peak, not the market rejecting the entry" -- true
# in isolation, but it left the churn loop wide open through the other
# door: a bucket could stop -> re-buy -> trail out -> re-buy all day
# without ever tripping a cooldown, paying the spread and both legs of
# taker fees on every lap. Counted together, one give-back-to-the-peak on
# a bucket is as good a reason to leave it alone for the day as one
# stop-out is. Kept as a tuple rather than hardcoded so the trade-off
# stays visible and reversible.
#
# "closed_trailing_stop" is RETAINED after the trailing stop itself was
# removed (2026-08-17). Nothing writes that status any more, but stored
# positions carry it, and this tuple is matched against position HISTORY --
# dropping it would silently stop those rows tripping the cooldown they
# tripped yesterday.
COOLDOWN_COUNTED_EXIT_STATUSES = ("closed_stop_loss", "closed_trailing_stop")

# Minimum ABSOLUTE edge (model_prob - market_price, in dollars/share) for
# any entry. Percentage EV explodes mechanically as price -> 0, which
# floats sub-tick "edges" to the top of the EV ranking: a claimed edge
# smaller than a few cents on an illiquid book is inside the bid-ask noise,
# not a tradeable disagreement. Complements MAX_PLAUSIBLE_RAW_EDGE (which
# catches absurdly LARGE edges); this catches meaninglessly SMALL ones.
MIN_ABS_RAW_EDGE = 0.03

# Minimum market price for a bucket/side to count as an opportunity AT ALL
# -- anywhere: ev_engine.best_opportunities() (live + backtest screen) and
# the status dashboard's EV table both apply it. Because net EV divides
# raw_edge by price, a near-zero price turns any model disagreement into a
# quadruple-digit "+18,820% EV" artifact; a market at 0.001 late in the day
# is a CONVERGED market the stale morning model disagrees with, not free
# money. Single constant so the trading screen and the report can never
# drift apart on what counts as displayable edge again.
EV_MIN_PRICE_SCREEN = 0.03

# When the edge was computed against a std_dev_c with NO real spread
# signal behind it (CalibratedEstimate.spread_source == "fallback_default"
# -- see calibration.estimate_std_dev()), the probability the edge itself
# rests on is a guess, not a measurement. MIN_ABS_RAW_EDGE alone treats a
# 3.5c edge on a real ensemble spread identically to a 3.5c edge on a flat
# 1.2C default, even though only one of those is backed by real data.
# This multiplier raises the bar for fallback-quality estimates instead of
# silently trusting them at the same threshold as a properly-calibrated one.
LOW_CONFIDENCE_EDGE_MULTIPLIER = 2.0  # fallback_default estimates need 2x the normal minimum edge

# --- Exit-side price plausibility (position_manager.py) -------------------
# A price at either extreme of the book is far more often a RESOLVED market
# (or a broken quote) than a live one worth stop-lossing out of: a resolved
# bucket prints ~1.00 on the winning side and ~0.00 on the losing side.
# Anything at or below MIN_EXIT_PRICE -- or at or above 1 - MIN_EXIT_PRICE --
# is therefore routed through the resolution check (Gamma "closed" lookup +
# a confirming re-fetch) instead of straight into stop-loss logic. Booking a
# resolution as a stop-loss corrupts the performance record twice over: it
# misattributes the exit reason AND records a fabricated exit price.
MIN_EXIT_PRICE = 0.03

# Largest price move between two consecutive scan cycles that is treated as
# real without a second opinion. Weather buckets do move fast, but a jump
# bigger than this against a position's last known price is more likely a
# bad quote, a stale feed, or a token-id mix-up than genuine market movement
# -- so it requires a confirming re-fetch before ANY exit action is taken.
# If the re-fetch disagrees or fails, the cycle acts on NOTHING and leaves
# the position open: skipping one cycle is recoverable, exiting on a
# phantom price is not.
#
# Was 0.5 until 2026-08-09, which is most of the tradeable range and meant
# the check almost never fired: a 0.49c jump sailed through unconfirmed
# straight into the high-water mark, and the HWM is a monotone ratchet
# that persists to SQLite and never comes back down. One bad-but-plausible
# high print therefore set a permanent floor
# the position had to keep beating -- entry 0.30, a spurious 0.42, and the
# next honest 0.31 quote is a give-back that closes the position. 0.15 is
# roughly the largest intraday move a weather bucket makes between two
# scans without something else having gone wrong, and confirmation costs
# one extra request only when it fires.
MAX_SINGLE_CYCLE_MOVE = 0.15

# --- Scheduler windows ----------------------------------------------------
# All times are LOCAL to each station's own market day. The registry spans
# UTC+5 (Karachi) through UTC+9 (Japan/Korea), so scheduler.py groups
# stations by utc_offset_hours and evaluates these windows against each
# group's OWN local clock -- Tokyo's 05:00 primary window opens an hour
# before Singapore's. Defined as (start_hour, start_min, end_hour, end_min,
# interval_min, mode, min_net_ev, description).
#
# Hard floor: nothing runs before 04:00 local, by explicit design decision --
# not a technical limitation, a deliberate choice. The 23:00-ish "market
# open" window explored earlier in the framework's development was walked
# back after real data showed markets typically open ~48h ahead of
# resolution, not the evening before -- so it's excluded from the default
# schedule below and available only as an opt-in extra (see
# ENABLE_MARKET_OPEN_WINDOW).
#
# modes:
#   "closed"       -- scheduler does nothing, sleeps until next window
#   "pre_poll"     -- watching for the official forecast to publish
#   "primary"      -- full cycle: forecast -> calibration -> EV -> exits.
#                      Lowest EV threshold of the day (peak edge window)
#   "secondary"    -- same full cycle, but a higher EV bar and wider interval
#                      (edge is decaying, require more confidence to act)
#   "monitor_only" -- exit-checks only, no new entries surfaced
#   "risk_only"    -- exit-checks only, plus same-day nowcast signal watch
SCHEDULE_WINDOWS = [
    (0, 0, 4, 0, None, "closed", None, "Overnight -- explicit floor, nothing runs before 04:00"),
    (4, 0, 4, 45, 15, "pre_poll", None, "Early watch -- checking if forecast posted ahead of schedule"),
    (4, 45, 5, 0, 2, "pre_poll", None, "Tight pre-poll -- waiting for the 05:00 forecast publish event"),
    (5, 0, 8, 0, 10, "primary", 0.15, "Primary edge window -- confirmed bias-correction edge, tightest scan interval"),
    # ENTRIES CLOSE AT 08:00 (changed 2026-08-17). This was two "secondary"
    # entry windows, 08:00-09:00 at an EV bar of 0.20 and 09:00-10:00 at
    # 0.25 -- a decaying-edge ramp that raised the bar rather than closing
    # the door. Measured over 176 closed paper trades, the ramp does not
    # earn its place: realized P&L per dollar staked, by the LOCAL HOUR THE
    # POSITION WAS OPENED, is
    #
    #     05:00-07:59   +3.9%   (n=143)
    #     08:00-08:59   -8.9%   (n=19)
    #     09:00+        -7.8%   (n=14)
    #
    # A higher EV bar did not rescue the late hours; it selected a smaller
    # number of equally bad trades. n is small on both late buckets and the
    # magnitudes should not be trusted to a percentage point, but the sign
    # is the one the edge-decay thesis in this very file predicts, and the
    # cost of being wrong is asymmetric: closing early forgoes trades whose
    # measured expectancy is negative.
    #
    # The clearest single case is WMKK 2026-08-11. The bucket-33 bid fell
    # 0.49 -> 0.36 -> 0.20 across the morning; the 09:00 window bought the
    # ask at 0.24 at 09:35, and the 10:00 stop tightening liquidated it 25
    # minutes later. An entry taken in the 09:00 hour has under an hour
    # before EDGE_DECAY_TIGHTEN_HOUR_LOCAL halves its stop distance -- the
    # entry window used to stay open until the exact instant the exit rule
    # got stricter.
    #
    # 08:00-10:00 is now monitor_only at the same 15-minute cadence as the
    # 10:00-12:00 window that follows it. Interval is a MONITORING decision
    # once no entry can be surfaced (see the note below on post-decision
    # intervals), so the old 30-minute late-entry cadence would have halved
    # the resolution of every exit level for two hours for no remaining
    # reason.
    #
    # NOT changed here, deliberately: EDGE_DECAY_TIGHTEN_HOUR_LOCAL stays at
    # 10:00, so there is now a two-hour gap where a position holds under the
    # loose stop with no new entries being taken. Whether the tightening
    # should follow the entry close down to 08:00 is a SEPARATE question
    # about exits, and the measurement that would answer it has to model the
    # capital a stop frees (stop_loss_audit.py's held column explicitly
    # cannot).
    (8, 0, 10, 0, 15, "monitor_only", None, "Decision-closed early -- entries shut at 08:00, watching existing positions"),
    # POST-DECISION INTERVALS ARE EXIT-MONITORING INTERVALS, NOT ENTRY ONES.
    # No new position is opened after 08:00, so the only thing these windows
    # decide is how often a stop-loss, trailing stop or profit-take can fire
    # at all -- a threshold is only ever evaluated on a scan tick, so the
    # scan interval IS the resolution of every exit level in risk_manager.
    #
    # These were widened to 120/60/180. They are now 15/15/30 -- but NOT
    # for the reason originally given here, and the correction is worth
    # keeping because the original reasoning is the tempting one.
    #
    # THE ORIGINAL ARGUMENT, WHICH DID NOT HOLD. Across the 12 stop-losses
    # in the three days from 2026-08-09, the distance between the trigger
    # price and the price actually filled looked linear in the window
    # interval (~0.010 at 10 min, 0.018 at 30, 0.070 at 60), so the clock
    # was moved to close the gap. Re-measured over the 16 stop-losses in
    # the two days after, on the tighter cadence, it had not improved:
    # genuine misses went 0.0177 -> 0.0322 mean, 0.070 -> 0.073 worst, and
    # the 10-minute primary window itself produced misses of 0.073 and
    # 0.032. The apparent linearity was small-sample coincidence across
    # different stations, books and days.
    #
    # WHAT THE FILLS ACTUALLY ARE. Reconstructed from price_snapshots,
    # they are single jumps, not sampling misses. ZGGG 35C YES sat flat at
    # 0.260 for seven consecutive reads and printed 0.110 on the eighth,
    # straight through a 0.203 trigger; ZBAA 30C YES was 0.360 at one read
    # (three-tenths of a cent above its stop) and 0.260 at the next. No
    # scan interval catches a price that never trades in between. Stop
    # slippage on a thin book is an EXECUTION problem -- resting orders,
    # or sizing for the gap -- and cannot be bought with polling.
    #
    # WHY THE TIGHTER CADENCE STAYS ANYWAY. It pays for itself somewhere
    # else entirely: take-profit went from 21% to 44% of all exits and
    # resolution from 21% to 0%, i.e. positions that used to drift to
    # settlement now get their level taken while it is there. Stop-loss
    # share was flat (52% -> 50%), so tighter watching did not convert
    # recovering dips into extra stop-outs, which was the live worry. Cost
    # is 42 -> 72 ticks/day/station with fd count flat at 5 and RSS 72 MB
    # after 1d19h, so there is nothing to buy back by reverting.
    #
    # ONE MEASUREMENT TRAP, since it will otherwise be rediscovered. At
    # 10:00 the stop tightens from 30% to 15% of the risk unit, which
    # RAISES the trigger price, so positions already sitting between the
    # two levels are captured on the first tick after 10:00. That is the
    # edge-decay policy firing as designed, not a missed fill, and it
    # accounted for 5 of 12 and then 8 of 16 of what was first counted as
    # overshoot. Measure the two separately or the cadence will look
    # worse, and the stop tighter, than either really is.
    #
    # The entry window (05:00-08:00 since 2026-08-17, 05:00-10:00 when this
    # was written) is deliberately UNCHANGED by that cadence work: its
    # interval encodes the edge-decay thesis rather than a monitoring
    # requirement, and scanning for entries more often is a different
    # decision from watching open risk more often.
    (10, 0, 12, 0, 15, "monitor_only", None, "Decision-closed -- no new entries, watching existing positions"),
    (12, 0, 16, 0, 15, "risk_only", None, "Afternoon peak-heat window -- nowcast-triggered risk checks only"),
    (16, 0, 22, 45, 30, "monitor_only", None, "Evening -- position monitoring, wider as volatility falls off"),
    (22, 45, 24, 0, None, "closed", None, "Late night -- log closing state, then stop until 04:00"),
]

# Opt-in only -- disabled by default per the walked-back Window-0 analysis.
# If enabled, adds a sparse scan around a station's likely next-day market
# open; treat any signal from this window as lower-confidence than the
# 05:00-08:00 primary window (see edge analysis).
ENABLE_MARKET_OPEN_WINDOW = False
MARKET_OPEN_WINDOW = (23, 0, 23, 30, 10, "secondary", 0.35, "Optional market-open window -- higher EV bar, unconfirmed edge")

# --- Entry sizing & gating (entry_manager.py) ------------------------------
# Bankroll figure used purely for Kelly-fraction sizing math -- NOT a real
# funded balance (no wallet/custody exists in this codebase, see executor.py).
# Set this to whatever a real deployment's actual bankroll would be before
# trusting recommended_size_usd for anything real.
BANKROLL_USD = 1000.0

# Fractional Kelly, not full Kelly -- full Kelly is provably correct only
# under known-exact probabilities, which a calibrated estimate is not.
# Quarter-Kelly trades some growth rate for a much shallower drawdown curve
# when the model's probability is wrong, which it sometimes will be.
KELLY_FRACTION = 0.25

# Floor under Kelly's denominator. compute_kelly_fraction() is
# raw_edge / (1 - price), which is correct binary Kelly and also a
# division by something that goes to zero as price -> 1. The practical
# effect at high prices is that sizing stops responding to edge quality:
# at price 0.95 the MINIMUM legal edge (MIN_ABS_RAW_EDGE = 0.03) already
# yields 0.60 full-Kelly -> 15% of bankroll -> pinned at MAX_POSITION_USD,
# so every approved entry comes out at maximum size no matter how good it
# is. MAX_POSITION_USD was the only thing standing between that and a
# much larger bet.
#
# Flooring the denominator at 0.20 caps full-Kelly at 5x the raw edge, so
# sizing keeps discriminating. It binds only above price 0.80, which makes
# it a backstop behind MAX_ENTRY_PRICE = 0.75 rather than the primary
# control -- deliberately so: the failure it guards against is silent
# (sizing looks normal, it just stopped meaning anything), and a future
# loosening of the entry-price cap should not quietly re-arm it.
MIN_KELLY_DENOMINATOR = 0.20

# Hard per-trade cap, independent of what Kelly sizing alone would suggest --
# a large enough apparent edge should never translate into betting the whole
# bankroll on one bucket of one station's market.
MAX_POSITION_USD = 150.0

# --- Real-money execution (executor.py, clients/wallet_client.py) ----------
# Everything below governs the ONLY path in this codebase that can spend real
# funds. Read the mode ladder in executor.py before changing any of it.
#
# THE LADDER: manual_review -> paper -> simulation -> live
#   Only "live" spends money. "simulation" is the new rung: it runs the REAL
#   order-construction path (tick-size resolution, share rounding, minimum
#   order-size check, balance/allowance preflight) against the REAL market,
#   and stops immediately before submission. Its purpose is that the code
#   which will eventually move money is the code being exercised now --
#   "paper" cannot test any of that, because it fabricates a fill at the
#   decision price and never touches wallet_client at all.

# Stations allowed to leave paper trading. Membership here is necessary but
# NOT sufficient -- a station must ALSO be STATION_MATURITY == "mature"
# (see live_size_cap_usd() below, which enforces the conjunction).
#
# NEITHER MEMBER IS HERE ON MEASURED EDGE. Both are live only via
# MATURITY_OVERRIDE below, and as of 2026-08-19 BOTH fail the beats_market
# criterion -- WSSS on staleness (its run predates HEAD), RCSS on substance
# (model Brier 0.145 vs market 0.062 over the 9 entries scored so far).
# RCSS was added 2026-08-19 at the operator's instruction, mirroring the
# WSSS setup. Its bias half of the case is now genuinely strong -- -1.82C
# is large but pinned to +/-0.31C over 13 pairs and stable across halves,
# which is precisely the correctable case ENABLE_FORECAST_BIAS_CORRECTION
# exists for -- and its market-calibration half is the weakest in the
# allowlist. See its MATURITY_OVERRIDE entry.
#
# Everything on the live track is bounded by ONE shared blast radius
# (LIVE_MAX_CONCURRENT_POSITIONS / _TOTAL_EXPOSURE_USD / _ORDERS_PER_DAY),
# so a second station does not enlarge the exposure -- it competes with
# WSSS for the same 3 slots and the same $11.25. Adding a third would need
# that trade-off re-read, not just another name here.
#
# NOTE this widens what the PROCESS-GLOBAL second gate authorises:
# POLYMARKET_LIVE_TRADING=true is not per-station, so this set is the only
# thing scoping real money to particular stations. Read both together.
LIVE_TRADING_STATIONS = {"WSSS", "RCSS"}

# Fixed notional for every simulation/live order, in dollars. This REPLACES
# Kelly sizing entirely for those modes rather than capping it: at $1 the
# point is to validate the execution path, not to express edge, and a size
# that varies with Kelly would make a rejected order impossible to
# distinguish from a mis-sized one.
#
# Applied in entry_manager.size_position(), NOT in executor. That matters:
# slippage, net EV and the depth cap are all re-checked at the actual size,
# so clamping after those checks would file a $1 trade under a $150 trade's
# gate results.
LIVE_TRADE_SIZE_USD = 1.0

# THE MINIMUM ORDER SIZE IS DENOMINATED IN SHARES, NOT DOLLARS, AND IT
# BINDS HARD AT $1.
# --------------------------------------------------------------------------
# Probed against the live CLOB API on 2026-08-10: the WSSS bucket markets
# report min_order_size = 5 SHARES ("mos":5), not $1 of notional. Minimum
# viable notional is therefore 5 x price:
#
#     price   0.10    0.20    0.31    0.50    0.62    0.75
#     min $   0.50    1.00    1.55    2.50    3.10    3.75
#
# A $1.00 order is legal only at price <= 0.20. On the live WSSS book the
# realistically tradeable buckets sat at 0.31 and 0.62 -- both of which
# REJECT a $1 order. MAX_ENTRY_PRICE is 0.75, so the true worst case for a
# single minimum-size trade is $3.75.
#
# (Corroborating evidence for the share reading: resting book orders of
# 23.67 shares @ 0.004 = $0.09 notional exist and are legal, and the value
# is identically 5 on markets priced 0.045 and 0.74. Strong, but NOT proof
# -- a resting order can be a partially-filled remnant. Confirm with one
# deliberately undersized live order and read the rejection before trusting
# any sizing formula.)
#
# Separately, the CLOB order builder ROUNDS SHARE COUNTS DOWN
# (round_down(size, 2), verified in py_clob_client_v2/order_builder/
# builder.py:36), so even where $1 is legal a naive $1.00 order is submitted
# for slightly under $1.00. wallet_client.build_entry_order() rounds the
# share count UP onto the same 2-decimal grid to cancel that out.
#
# WHAT THIS MEANS FOR A "$1 TRADE SIZE"
# --------------------------------------
# LIVE_TRADE_SIZE_USD stays $1: it is the size ASKED FOR, and it is what
# entry_manager sizes to. The exchange minimum then raises the order to the
# smallest legal size for that bucket's price, so actual orders land at
# roughly $1.55-$3.86 rather than $1 (the top of that range was $3.75 before
# the minimum began being funded at the padded limit; see the exposure note
# below). That is not the requested size and is
# not treated as if it were -- Position.size_usd records the RESOLVED
# notional, so P&L, exposure and every report read the real number.
#
# The ceiling is the hard stop on that upsizing. At $5.00 it clears the
# $3.75 worst case (MAX_ENTRY_PRICE 0.75 x a 5-share minimum) with room to
# absorb a wider minimum on a bucket that reports one, and still refuses
# anything that would make a "small validation trade" quietly material.
LIVE_SIZE_OVERSHOOT_CEILING_USD = 5.0

# Whether an order may be sized UP to the exchange minimum when the
# requested notional is below it. False keeps LIVE_TRADE_SIZE_USD a hard
# statement about how much may be spent per trade, at the cost of declining
# nearly every candidate (a $1 order is legal only at price <= 0.20).
# True lets the exchange minimum set the size, bounded by
# LIVE_SIZE_OVERSHOOT_CEILING_USD above.
#
# ON: the ceiling is now the only thing standing between a thin-book bucket
# reporting an unexpectedly large minimum and an order several times the
# intended size. Do not raise the ceiling without re-reading the exposure
# backstops below, which are what bound the total rather than the trade.
LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE = True

# Fail-closed backstops on the live track. These are not sizing knobs --
# they are the blast radius if something upstream is wrong. Breaching any of
# them stops NEW live entries; open positions still exit normally, because
# stranding a real position is worse than the exposure that opened it.
#
# The exposure ceiling admits the concurrent-position limit at the
# worst-case single trade -- and since 2026-08-20 the two are NO LONGER
# EXACTLY ALIGNED. They used to be: at a 5-share minimum the most expensive
# legal entry was MAX_ENTRY_PRICE x 5 = $3.75, and three of those is exactly
# $11.25.
#
# WHAT MOVED. A BUY is submitted in dollars while the exchange minimum is in
# SHARES, so the floor is now funded at the PADDED LIMIT rather than at the
# expected price (wallet_client._shares_at_worst_fill -- a fill one tick
# inside the pad used to deliver fewer than 5 shares and strand a position
# that could never be sold). At MAX_ENTRY_PRICE the floor is therefore 5.14
# shares costing $3.86, payable at up to $3.96 rather than $3.75. Three of
# those want $11.87 against an $11.25 cap.
#
# LEFT AS IT IS, DELIBERATELY. The third entry gets blocked rather than
# sized up, which is the same safe direction the 8-share case below relies
# on, and $11.25 is a blast radius rather than a number the sizing needs to
# be consistent with. Raising it to $11.87 would be a decision to carry more
# real exposure, and is the operator's to make, not a bookkeeping tidy-up.
#
# LIVE_SIZE_OVERSHOOT_CEILING_USD ($5.00) does NOT set the trade size -- it
# is headroom above that worst case, and only becomes the binding number if
# a bucket reports a minimum larger than 5 shares.
#
# That is the case to watch. A bucket demanding, say, 8 shares at 0.60
# would cost $4.80: still under the ceiling, but three of them need $14.40
# and the exposure cap would stop the third. That failure is the safe
# direction (fewer positions, not larger ones), which is why the ceiling is
# allowed to run ahead of the exposure cap rather than being pinned to it.
# If you raise the ceiling further, re-derive this ceiling x
# LIVE_MAX_CONCURRENT_POSITIONS product rather than assuming it still holds.
LIVE_MAX_CONCURRENT_POSITIONS = 3        # across all live stations
LIVE_MAX_TOTAL_EXPOSURE_USD = 11.25      # sum of open live size_usd
LIVE_MAX_ORDERS_PER_DAY = 10             # submitted entries per UTC day

# Entries submit as FOK (fill-or-kill), not GTC. A GTC limit order returns
# an order id and then RESTS -- it is not a fill. Recording that as an open
# position creates a position whose shares do not exist, which the exit path
# will later try to sell. FOK either fills completely and immediately or is
# killed, so submission and fill are the same event and there is no
# unreconciled state to get wrong. The cost is giving up the maker rebate
# and accepting the taker fee, which ev_engine already charges for anyway.
LIVE_ENTRY_ORDER_TYPE = "FOK"

# --- Stop-gap allowance (entry_manager sizing) ----------------------------
# A stop-out does not cost you the stop distance. It costs the stop distance
# PLUS however far the book gapped past the trigger before anything could
# sell, and on these markets that second term is not small.
#
# It is also not fixable by watching more often, which is the whole reason
# this constant exists rather than another schedule change. The 2026-08-12
# cadence tightening (120/60/180 -> 15/15/30 min, commit 6ed099e) did not
# reduce it at all: the fills are single jumps with no trades in between.
# ZGGG 35C YES sat at 0.260 for seven consecutive reads and printed 0.110
# on the eighth, straight through a 0.203 trigger. You cannot poll your way
# into a price that never existed, and a resting sell limit at the stop is
# worse than useless -- below the market it fills instantly, and in a
# falling market nobody lifts it. So the gap is priced in at ENTRY instead,
# by sizing for it.
#
# MEASURED, not guessed. Over all price_snapshots joined to market_tokens,
# taking consecutive observations of the same token 10-20 minutes apart
# (the horizon the 15-minute exit windows actually decide on) with a prior
# price above MIN_EXIT_PRICE, the downward move distribution was:
#
#     median 0.010 (one tick) at every station
#     p90    0.030-0.060, clustered on 0.040
#     p99    0.060-0.330, noisy and unstable
#
# 0.04 is that p90: nine times in ten the gap beyond a stop is under four
# cents. p99 was rejected as the basis -- it swings by 5x across stations
# on ~300 samples each and would halve position sizes off what is often a
# single resolution-adjacent print.
#
# DELIBERATELY ONE NUMBER, NOT THIRTEEN. The per-station spread above is
# not supported at n~300 per station, and the raw per-station figures are
# confounded anyway: WSSS and WMKK are sampled every ~5 minutes by the
# depth collector while the other eleven are on ~13, so an unnormalised
# comparison reads sampling cadence as market behaviour. Override a single
# station below only with a station-specific measurement that survives that
# normalisation.
EXPECTED_STOP_GAP = 0.04
STOP_GAP_BY_STATION: dict = {}


def stop_gap_allowance(station_icao: Optional[str] = None) -> float:
    """
    Dollars per share a stop is expected to slip past its trigger at this
    station. See EXPECTED_STOP_GAP for the measurement and for why this is
    one shared number rather than a per-station table.
    """
    if station_icao and station_icao in STOP_GAP_BY_STATION:
        return STOP_GAP_BY_STATION[station_icao]
    return EXPECTED_STOP_GAP


# Never size a position larger than this fraction of the visible order-book
# depth (within a 10% price-impact band, per market_client.get_available_depth_usd).
# Two reasons: (1) the slippage ESTIMATE itself becomes unreliable past this
# point since it's extrapolating past what the book actually shows, and
# (2) placing an order this large would materially move the market itself.
MAX_DEPTH_UTILIZATION_PCT = 0.25

# Absolute gate, independent of net EV. Even if net_ev_per_dollar is still
# positive after subtracting slippage, a trade requiring this much slippage
# to fill is a sign the book is too thin to trust the fill price at all --
# reject rather than trade into it.
MAX_ACCEPTABLE_SLIPPAGE_PCT = 0.10

# Hard plausibility ceiling on the raw edge (model_prob - market_price) of
# any candidate entry. On a liquid weather market, an edge this large is
# not alpha -- it is bad data. Real, tradeable disagreements between a
# calibrated forecast and a live book run to a few cents; anything past
# this is a stale calibration, a broken quote, or a side/price mix-up.
# This constant exists because of a real incident: market_client derived
# NO prices as `1 - yes_price` from a token that was already the NO token,
# so every NO position was priced at `1 - reality`. That produced "edges"
# of 0.88 and net EVs of +1298% which sailed through every EV and sizing
# gate into real money -- because nothing anywhere asked whether an edge
# that big was even believable. Now something does.
MAX_PLAUSIBLE_RAW_EDGE = 0.25

# ...but a flat ceiling is structurally blind at high prices, because raw
# edge is bounded by (1 - price) anyway: model_prob cannot exceed 1.0, so
# at price 0.80 the largest edge that can even be expressed is 0.20 and
# the 0.25 ceiling is unreachable. Above price 0.75 this gate could never
# fire at all -- exactly the band where a side/price mix-up produces the
# most believable-looking numbers. A spurious model_prob of 0.99 against a
# real 0.85 quote is an edge of 0.14: comfortably under the flat ceiling,
# through every other gate, and sized at the cap.
#
# So the ceiling also scales with the headroom actually available:
#
#   ceiling(price) = min(MAX_PLAUSIBLE_RAW_EDGE,
#                        MAX_PLAUSIBLE_EDGE_HEADROOM_FRACTION * (1 - price))
#
# At 0.50 the two terms are equal (0.5 x 0.5 = 0.25), so the curve is
# continuous and the flat ceiling still binds everywhere below 0.50 --
# no existing behaviour changes. Above 0.50 the headroom term takes over:
# 0.10 at price 0.80, 0.025 at 0.95. "More than half the maximum edge
# that could possibly exist at this price" is not a trading signal.
MAX_PLAUSIBLE_EDGE_HEADROOM_FRACTION = 0.50


def max_plausible_edge_for(market_price: Optional[float]) -> float:
    """
    The largest raw edge that counts as believable at this price:
    min(MAX_PLAUSIBLE_RAW_EDGE, MAX_PLAUSIBLE_EDGE_HEADROOM_FRACTION x
    (1 - price)). See the two constants above for why the flat ceiling
    alone is structurally blind above price 0.75.

    Lives in config, not in entry_manager, because it is pure policy over
    two config constants and it has THREE consumers that must never
    disagree: the live entry gate, backtest/entry_sim's replica, and the
    status dashboard's "veto zone" badge. The dashboard keeping its own
    copy of a trading screen is a mistake this codebase has already made
    twice -- once with the min-price screen (fixed in 46db643, after the
    page spent a week ranking sub-cent phantoms at the top of the table),
    and again here, where a flat local constant left rows the entry path
    now rejects displayed as clean opportunities. config is the one
    module all three already import, and it depends on nothing but
    models, so the dashboard pays no new import surface for the fix.
    """
    if market_price is None:
        return MAX_PLAUSIBLE_RAW_EDGE
    headroom_ceiling = MAX_PLAUSIBLE_EDGE_HEADROOM_FRACTION * (1.0 - market_price)
    return min(MAX_PLAUSIBLE_RAW_EDGE, max(headroom_ceiling, 0.0))

# Maximum simultaneously-open positions on the exact same
# (station, target_date, bucket, side). One is the right number: repeat
# entries on one bucket are not independent bets, they are the same bet
# sized up by accident, and they silently multiply exposure past every
# per-trade cap above. On day 1 in production the same bucket was entered
# 4 times across consecutive cycles because nothing checked what was
# already open before approving another leg.
MAX_OPEN_POSITIONS_PER_BUCKET = 1

# Station maturity gating, per the edge analysis: WSSS has a confirmed,
# measured bias-correction edge (14+ days of observed history). No other
# station has earned that status -- every trade there is exploratory and
# sized down hard rather than treated as equivalent-confidence.
# entry_manager defaults unlisted stations to "exploratory" anyway; the
# explicit entries below exist so a promotion is a deliberate, greppable
# one-line edit, never an accident of a missing key.
# STATION_MATURITY IS NOW DERIVED -- see station_maturity() at the foot of
# this file. It used to be a hand-typed dict here, which made the one gate
# authorising real money the only unmeasured gate in the system.
#
# The name is kept as a read-only mapping so existing readers (the backtest
# manifest, reports) still work, but it is built from the measured criteria
# rather than typed. Anything gating on maturity should call
# station_maturity(icao) so it gets the live answer, not a snapshot.

EXPLORATORY_SIZE_MULTIPLIER = 0.20  # exploratory stations get 20% of what mature-station sizing would suggest

# A station may not open ANY position until this many stored observations
# exist whose source is its own resolution_grade_source. A brand-new station
# has an unmeasured model bias, a placeholder climatological normal, and
# (usually) spread_source="fallback_default" -- a 2C-wrong placeholder
# produces "edges" squarely inside the tradeable band, and every one of
# those trades is wrong. Collection costs nothing; wrong entries don't.
# New stations therefore start collection-only automatically and graduate
# by simply existing for a few days.
#
# Raised 5 -> 10 on 2026-08-09, and demoted to a FLOOR rather than the
# test. At 5 it was doing nothing at all: a forecast/observation pair
# requires an observation, so n_pairs <= n_observations always, and
# MIN_BIAS_PAIRS_BEFORE_ENTRY = 5 already implies five observations. The
# adaptive bias gate below (pairs + standard error) is the real check --
# it asks for more evidence from a noisy station and less from a
# consistent one, which a flat count cannot do. What the count still buys
# is history behind the OTHER half of the estimate: observed readings
# carry 60% of the weight in blend_central_estimate() and are the
# observed_variance spread fallback, and five of them is a thin basis for
# either. Ten is roughly a fortnight of settlement-grade record. Both
# currently-trading stations (WSSS and WMKK, 13 each) clear it, so this
# changes nothing today -- it is a guard for the next station added.
MIN_RESOLUTION_OBS_BEFORE_ENTRY = 10

# --- HKO ingest lookback: match the window to the source's LAG -----------
# How far back clients/official/hko.ingest_missing_recent() looks for days
# it has no "hko_daily_max" observation for.
#
# This is not a tuning knob, it is the fix for a window that could never
# overlap its source. The ingest ran with metar_client's 3-day default,
# while HKO's CLMMAXT climate dataset publishes a COMPLETE MONTH AT A TIME,
# weeks in arrears:
#
#   2026-08-05 probe: published through 2026-06-30 (July absent)
#   2026-08-16 probe: published through 2026-07-31 (August absent, 0 rows)
#
# So July 1st became readable roughly 46 days after the fact. A sweep that
# only ever asks about the last three days is therefore GUARANTEED to save
# nothing, forever -- and it did: VHHH accumulated hko_fnd forecasts from
# 2026-08-06 onward against zero settlement-grade observations, so its
# resolution_grade_source had no rows at all and the station could never
# have matured or been scored. The data was sitting in the API the whole
# time; nothing ever asked for it.
#
# 75 days covers the observed worst case (~46) with room for a month that
# publishes slower, and costs little: rows are cached per (year, month), so
# a 75-day span is at most ~4 month-queries, once per station per local day.
#
# NOTE this backfills real history, which lifts VHHH over the raw
# MIN_RESOLUTION_OBS_BEFORE_ENTRY count without it having been watched
# forward. That is safe on its own: bias pairs need a FORECAST for the same
# date (storage.forecast_error_samples joins on target_date) and VHHH has no
# stored forecast before 2026-08-06, so the adaptive bias gate -- the real
# check per MIN_RESOLUTION_OBS_BEFORE_ENTRY's own note above -- still has to
# be earned forward, one day at a time.
HKO_INGEST_LOOKBACK_DAYS = 75

# --- Forecast bias: measure it, correct it, and refuse to trade blind ----
# Counting observations was never the point -- MEASURING THE BIAS was, and
# until 2026-08-09 nothing did. Measured that day across the registry:
# WSSS +0.07C (the only near-unbiased station, and the only reliably
# profitable one) against RCSS -1.80, WMKK -1.76, RKPK -1.49, RKSI -1.48,
# ZGGG -1.17. Buckets are whole degrees, so a 1.7C bias misplaces the
# model's probability mass by ~2 buckets -- on every bucket, every cycle,
# all in the same direction, at exactly the tradeable size. That is the
# failure collection_only_reason()'s docstring predicted; the gate did not
# catch it because 5 stored observations was the whole test.
#
# Two constants, doing two different jobs:
#   ENABLE_FORECAST_BIAS_CORRECTION subtracts the measured bias from the
#   forecast term of the central estimate (calibration.blend_central_estimate)
#   -- the fix. It is what the collected observations were collected FOR.
#
#   MIN_BIAS_PAIRS / MAX_BIAS_STANDARD_ERROR_C gate on whether that
#   correction can be TRUSTED. Correcting by a number measured off two
#   days is just a different guess. Standard error (sd/sqrt(n)), not raw
#   |bias|, is the right test: a large bias measured precisely is
#   correctable, a small one measured noisily is not.
ENABLE_FORECAST_BIAS_CORRECTION = True

# --- Blend weight: how much of the central estimate is the FORECAST ------
# blend_central_estimate() mixes today's (bias-corrected) forecast with the
# mean of recent settled maxima. The split was 40/60 toward the observed
# term, with the code's own justification being "directionally justified by
# the measured NEA bias" -- i.e. forecasts were known to be biased, so they
# were down-weighted. That is a workaround, not a fix, and once the bias is
# corrected explicitly the two compensations stack: at w=0.4 a measured
# -1.66C bias only moved the estimate +0.66C.
#
# MEASURED on 2026-08-10 over 52 station-days that have a forecast, prior
# observations, and settlement-grade truth. Bias applied leave-one-out, so
# the correction is never fitted on the point it is scored against:
#
#     w_forecast   0.0    0.4    0.7    0.85   0.9    0.95   1.0
#     RMSE (C)     2.665  1.760  1.234  1.092  1.072  1.068  1.080
#
# Leave-one-STATION-out CV lands at RMSE 1.074 with per-fold weights of
# 0.90-0.95, so the optimum is not an artifact of fitting 52 points.
# Persistence alone (w=0) is roughly HALF as accurate as the forecast
# alone -- the old blend put 60% of the weight on the worse predictor.
#
# 0.85 rather than the measured 0.90-0.95: the plateau is flat (1.068 to
# 1.092 across 0.85-0.95, ~2%), and keeping 15% on observed history is a
# cheap hedge for cycles where the forecast set is thin or stale.
FORECAST_BLEND_WEIGHT_DEFAULT = 0.85

# Per-station overrides. WSSS is the one station that genuinely wants a
# persistence-heavy blend, and that part is not a fitting artifact:
# equatorial Singapore's daily max barely moves, so yesterday really does
# predict today there, and the legacy 0.40 was tuned ON Singapore -- right
# for this station, wrong for the other ten.
#
# 0.40, NOT the 0.50 this shipped as on 2026-08-10. That commit justified
# 0.50 as WSSS's own optimum; independently replicated on the same data, it
# is neither the optimum nor an improvement:
#
#     w      0.20    0.35    0.40    0.50    0.85    0.95
#     RMSE   0.537   0.525   0.527   0.538   0.654   0.703
#            n=9, WSSS only, leave-one-out bias
#
# Bootstrapping WSSS's own days, 0.40 beats 0.50 about three times in four
# (P(0.50 better) = 0.26). The commit's own reported figures said the same
# thing -- "WSSS is 0.541 -> 0.582" is the legacy 0.40 scoring better than
# the new 0.50 -- and it shipped anyway, describing 0.50 as near-optimal.
#
# What the data DOES support is overriding away from the 0.85 default:
# P(0.50 better than 0.85) = 0.93. Both 0.40 and 0.50 clear that bar; 0.40
# is simply the better of the two and the one that was already here.
#
# NOT set to the 0.35 argmin. On n=9 the bootstrap optimum ranges 0.00-0.70
# (median 0.30), so the per-station weight is essentially unidentified and
# chasing the argmin would be fitting noise. Leave-one-station-out CV
# cannot settle this either -- WSSS's fold picks 0.95, because it learns
# only from ten forecast-heavy stations.
#
# Only add an override with a PHYSICAL reason and enough samples to
# distinguish it from its neighbours on the grid. Nine is not enough; it
# was enough to establish the direction here, not the value.
FORECAST_BLEND_WEIGHT_BY_STATION = {
    "WSSS": 0.40,
}


def forecast_blend_weight(station_icao: str) -> float:
    """Weight on the forecast term of the central estimate, for one station."""
    return FORECAST_BLEND_WEIGHT_BY_STATION.get(station_icao, FORECAST_BLEND_WEIGHT_DEFAULT)


# --- Per-station forecast source exclusions --------------------------------
# Sources that are still COLLECTED and stored for this station but kept OUT
# of the forecast mean (calibration.blend_central_estimate averages whatever
# it is handed) and out of the bias estimate fitted on that mean.
#
# Excluded, never "not fetched": the row keeps landing in `forecasts` so this
# decision stays re-checkable against accruing data, and so re-including a
# source later needs no backfill. Only the blend stops reading it.
#
# The bar for an entry here is a source that is WRONG at a station, not one
# that merely scored worse over a short window. Measured 2026-08-16 over the
# EC2 database (Aug 1-15, truth = each station's resolution_grade_source,
# forecasts fetched on or before the target date, per-station leave-one-out
# bias correction so nothing is scored against a bias fitted on it):
#
#   pooled, 87 station-days, RMSE after bias correction
#     ecmwf only 1.299 | gfs only 1.310 | official only 1.209
#     ecmwf+gfs  1.136 | ALL SOURCES 1.022   <- the flat mean already wins
#
# So the source POOL is not the lever and is deliberately left alone. The one
# station-level defect the same sweep found is RKSI, where the three sources
# behave completely differently (n=9, error = forecast - settled truth):
#
#   ecmwf  mean +0.08  sd 0.78  signs ---+-+-++
#   gfs    mean -4.84  sd 1.24  signs ---------   (-6.6 -5.7 -6.1 ... -3.3 -2.9)
#   wwis   mean +1.67  sd 1.80  signs ++-+-+-++   (whole degrees; +4.0 twice)
#
# GFS is broken here, not merely worse: nine days, every error negative, 3-7C
# cold, against an essentially unbiased ECMWF over the same days. Open-Meteo
# returns the same 5m elevation for both grid points, so it is not a land/sea
# snap. And a CONSTANT bias correction cannot absorb it -- the series DRIFTS
# (-6.6 -> -2.9 across the window), so the single scalar
# entry_manager.forecast_bias_stats fits chases a moving offset.
#
# WWIS IS EXCLUDED TOO, AND THE REASON IS THE WHOLE POINT OF THIS ENTRY.
# Dropping GFS alone makes RKSI WORSE, not better:
#
#   RMSE after per-station bias correction / whole-degree bucket hit rate
#     ecmwf+gfs+wwis (was)   0.85   44%
#     ecmwf+wwis             0.93   22%   <- dropping only the broken source
#     ecmwf alone            0.82   56%
#
# GFS's -4.84 and WWIS's +1.67 were partly CANCELLING in the three-way mean.
# Remove one and the survivor's error is exposed: WWIS is a city-level, whole-
# degree WMO forecast (Seoul) scored against Incheon airport truth, and its sd
# of 1.80 is more than twice ECMWF's. Its only contribution at RKSI was
# offsetting a bug. That is not a reason to keep either.
#
# n=9, so treat the RANKING as thin and the GFS defect as solid: the sign is
# unanimous and the magnitude is 4x ECMWF's worst day, which is the standard
# FORECAST_BLEND_WEIGHT_BY_STATION demands ("a PHYSICAL reason and enough
# samples to distinguish it from its neighbours"). WWIS is bad AT RKSI only --
# at RJTT the official source is the single best variant (1.50) -- so this
# stays per-station and must not become a policy about either source.
# Re-check as the window grows; if GFS at RKSI returns to the ~1C band the
# other stations sit in, delete the entry.
FORECAST_SOURCES_EXCLUDED_BY_STATION = {
    "RKSI": ("open_meteo_gfs", "wwis"),
}


def forecast_source_is_blended(station_icao: str, source: str) -> bool:
    """Whether `source` may enter the forecast mean for this station."""
    return source not in FORECAST_SOURCES_EXCLUDED_BY_STATION.get(station_icao, ())


def blendable_forecasts(station_icao: str, forecasts: list) -> list:
    """
    `forecasts` minus this station's excluded sources.

    NO FALLBACK when that leaves nothing. RKSI blends ECMWF alone, so a cycle
    where only the excluded sources returned would fall back to precisely the
    GFS+WWIS pair measured as the worst combination available there (RMSE 0.93
    against 0.82, bucket hit 22% against 56%). Worse, the bias correction is
    fitted on the BLENDED source set: applying it to a set it was never
    measured on adds an error of about the size it is meant to remove.

    An empty forecast term is the honest outcome -- blend_central_estimate
    falls to the observed term, which is a worse predictor but a KNOWN one,
    and entry_manager's bias-quality gate keeps the station collection-only
    while the correction cannot be trusted.
    """
    return [f for f in forecasts if forecast_source_is_blended(station_icao, f.source)]

# Forecast/observation pairs required before the bias estimate may be
# trusted enough to trade on. Distinct from (and stricter in practice
# than) MIN_RESOLUTION_OBS_BEFORE_ENTRY: an observation with no matching
# stored forecast for the same target date measures nothing.
MIN_BIAS_PAIRS_BEFORE_ENTRY = 5

# Cap on the standard error of the bias estimate, in degrees C. At 0.5C
# the correction is worth less than the noise in the number correcting.
# Reference points from the 2026-08-09 measurement: WSSS n=9 sd 0.66 ->
# SE 0.22 (trustworthy), WMKK n=8 sd 0.76 -> SE 0.27 (trustworthy),
# RCSS n=3 sd 2.16 -> SE 1.25 (not).
MAX_BIAS_STANDARD_ERROR_C = 0.5

# --- Per-station override of the BIAS-QUALITY half of the gate -----------
# Stations listed here skip the MIN_BIAS_PAIRS_BEFORE_ENTRY /
# MAX_BIAS_STANDARD_ERROR_C checks in
# entry_manager.collection_only_reason(). They do NOT skip
# MIN_RESOLUTION_OBS_BEFORE_ENTRY: a station with no settlement-grade
# history at all stays blind, and this override cannot open it.
#
# VHHH, added 2026-08-20 at the operator's instruction, is the case this
# exists for. Its bias is not merely noisy, it is UNMEASURABLE and will
# stay that way for weeks: HKO's CLMMAXT climate dataset publishes a whole
# month at a time, weeks in arrears, so VHHH's 60 settlement-grade
# observations stop at 2026-07-31 while its stored forecasts start
# 2026-08-06. storage.forecast_error_samples joins on target_date, so the
# intersection is EXACTLY ZERO and no amount of waiting inside the current
# month changes it (see HKO_INGEST_LOOKBACK_DAYS' note). Every other gated
# station could earn its way out by collecting; VHHH cannot.
#
# WHAT THIS BUYS AND WHAT IT COSTS. It buys the forward record that the
# batch backfill will not produce on its own: 243 cycles over 14
# station-days were blocked with 1-4 candidates each, and none of them can
# ever be scored because there is no settlement truth for them either.
# It costs the protection the gate was written for on 2026-08-09, and the
# blocked candidates carry that failure's exact signature -- NO on the hot
# buckets (34/33/35/36/37), YES on the cool ones (29/30/31/32), i.e. "the
# model runs cooler than the market" on every bucket in one direction,
# which is what a cold forecast bias looks like and what every measured
# neighbour has (RCSS -1.80, WMKK -1.76, RKPK -1.49, RKSI -1.48,
# ZGGG -1.17). TREAT VHHH POSITIONS OPENED FROM 2026-08-20 AS TRADES ON AN
# UNMEASURED BIAS, not as evidence of edge, until August CLMMAXT publishes
# (~2026-09-16) and the bias can be measured for real.
#
# REMOVE THE STATION FROM THIS SET once its bias is measured -- leaving it
# here permanently disables the check that would then have real data.
BIAS_GATE_OVERRIDE_STATIONS = {"VHHH"}


def bias_gate_is_overridden(station_icao: str) -> bool:
    """
    Whether `station_icao` skips the bias-quality checks.

    REFUSES TO APPLY TO ANY REAL-MONEY STATION. The override says "trade
    this station on an unmeasured bias", which is a sentence that must
    never be true of money. LIVE_TRADING_STATIONS is the allowlist that
    authorises the real order path (config.live_mode_is_permitted ANDs it
    with maturity), so a station in both sets is a contradiction -- and it
    resolves in the safe direction: the override stops working and the
    station goes back to being gated, rather than the allowlist silently
    inheriting permission to spend on numbers nobody has checked.

    That is deliberately the failure mode for the one-line edit somebody
    makes later. Promoting VHHH to LIVE_TRADING_STATIONS without first
    deleting it here does not arm it; it re-blocks it.
    """
    if station_icao in LIVE_TRADING_STATIONS:
        return False
    return station_icao in BIAS_GATE_OVERRIDE_STATIONS

# Shared budget across ALL approved legs for one station on one day -- e.g.
# a YES leg on the top bucket plus NO legs hedging tail buckets, opened
# together (see entry_manager.apply_portfolio_budget). Without this, each
# leg is sized independently up to MAX_POSITION_USD and the combined total
# exposure for one station/day is unbounded. Set higher than a single
# MAX_POSITION_USD to allow a real multi-leg basket, but still capped well
# below "every leg at max size simultaneously."
MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD = 250.0

# Hard ceiling across EVERY station for one day. The per-station cap alone
# stopped meaning anything the day the registry grew from 2 stations to 13:
# 13 x $250 would quietly authorize $3,250/day of exposure against a $1,000
# bankroll -- and the new stations' edges are CORRELATED wrong-way bets
# (shared placeholder normals, shared fallback spreads), not independent
# ones, so diversification arguments don't apply. Sized at 40% of bankroll:
# room for a couple of real multi-leg baskets, never most of the roll.
MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD = 400.0


def live_size_cap_usd(station_icao: str, execution_mode: str) -> Optional[float]:
    """
    The fixed notional this station must trade at under `execution_mode`,
    or None if normal Kelly sizing applies.

    Takes the mode as an ARGUMENT rather than reading executor.EXECUTION_MODE
    directly: executor imports risk_manager which imports ev_engine which
    imports this module, so config cannot import executor without a cycle.
    Callers already have the mode in hand.

    The real-money conjunction lives here, in one place, so it cannot be
    half-satisfied anywhere else: a station trades at the live size only if
    it is BOTH explicitly allowlisted in LIVE_TRADING_STATIONS AND carries
    STATION_MATURITY == "mature". Adding a station to the allowlist without
    promoting it therefore does nothing, which is the safe direction for a
    one-line edit to fail in.
    """
    if execution_mode not in ("simulation", "live"):
        return None
    if station_icao not in LIVE_TRADING_STATIONS:
        return None
    if STATION_MATURITY.get(station_icao, "exploratory") != "mature":
        return None
    return LIVE_TRADE_SIZE_USD


def live_mode_is_permitted(station_icao: str, execution_mode: str) -> bool:
    """
    Whether `station_icao` may run in `execution_mode` at all.

    SIMULATION REQUIRES THE ALLOWLIST ONLY. LIVE ALSO REQUIRES MATURITY.
    That asymmetry is load-bearing, and getting it wrong creates a trap:
    maturity depends on order_path evidence, order_path evidence comes from
    resolved SIMULATED orders, so gating simulation behind maturity means a
    station that loses its certification can never regain it -- it is barred
    from the only activity that produces the evidence.

    Simulation submits nothing and spends nothing, so there is no risk to
    gate. The thing worth gating is real money, and that is what maturity
    now gates. A station demoted by the measured criteria therefore keeps
    building its record and stops being able to spend, which is the correct
    direction for both.
    """
    if execution_mode not in ("simulation", "live"):
        return True
    if station_icao not in LIVE_TRADING_STATIONS:
        return False
    if execution_mode == "simulation":
        return True
    return station_maturity(station_icao) == "mature"


# --- Predictive spread (calibration.estimate_std_dev) ----------------------
# std_dev_c is the width of the distribution every bucket probability is cut
# from, so it decides how much mass sits on the modal bucket versus the
# tails. MEASURED 2026-08-10 on 50 station-days, scoring multi-class Brier
# against settled buckets: it is the single biggest lever in the model, and
# the estimator was losing to a constant.
#
#   current chain (forecast_variance for all 50)   Brier 0.8040
#   a flat 1.0C                                    Brier 0.7228
#   measured per-station error spread + pooled     Brier 0.7225
#
# The old chain's second tier was stdev across the available point
# forecasts -- i.e. how much the MODELS DISAGREE, which is not the quantity
# the probability step needs and is estimated from two or three numbers. It
# emitted spreads from 0.25C to 4.33C against a 1.0C bucket width. An
# over-wide distribution underprices the modal bucket and overprices the
# tails, which manufactures exactly the fake tail edges the entry gates then
# have to veto; an over-narrow one manufactures confidence.
#
# What the probability step actually wants is the spread of this station's
# own FORECAST ERRORS -- and the mean of that distribution is already
# measured as forecast_bias_c. Its standard deviation was sitting unused.

# Forecast/observation pairs required before a station's own error spread is
# preferred over the pooled one. Matches MIN_BIAS_PAIRS_BEFORE_ENTRY: the
# same pairs, the same reason to distrust three of them.
MIN_SPREAD_PAIRS = 5

# Hard band on any returned spread, whatever tier produced it.
#
# The floor is the safety-critical end and is deliberately ABOVE the grid
# optimum. A too-NARROW spread is the dangerous direction: it makes the
# model look certain, which inflates the gap between model probability and
# market price, which is an edge the entry gates will happily size into.
# A too-wide spread only costs missed trades. WSSS's own measured spread
# (~0.56C) sits below this floor and gets raised to it.
SPREAD_FLOOR_C = 0.7
SPREAD_CEILING_C = 2.0

# Used only when even the pooled estimate is unavailable -- an empty
# database. The measured pooled value on 2026-08-10 was 0.81C.
POOLED_SPREAD_FALLBACK_C = 1.0

# How long the pooled spread is cached. It reads every station's error
# samples, so recomputing it per station per cycle would be 13x the queries
# for a number that moves once a day at most.
POOLED_SPREAD_CACHE_TTL_S = 3600

# Spread sources that mean "not measured FOR THIS STATION". Entries against
# one of these must clear LOW_CONFIDENCE_EDGE_MULTIPLIER x the normal
# minimum edge, because the probability the edge is computed from is only
# as good as the spread behind it.
#
# "pooled_error" is new to this set and it TIGHTENS the gate: under the old
# chain every station got "forecast_variance", which never tripped the
# multiplier even though it was the least trustworthy number in the model.
# A station with fewer than MIN_SPREAD_PAIRS pairs now correctly has to
# clear a higher bar. WSSS is unaffected -- it has its own measured spread.
LOW_CONFIDENCE_SPREAD_SOURCES = {"fallback_default", "pooled_error"}


# --- Order-book and balance defects in Polymarket's own API ---------------
# Both of these are upstream bugs with production evidence behind them,
# ported from a second Polymarket bot on this machine after it hit them
# (~/Downloads/hermes/core/execution.py). Neither is theoretical.

# THE "GHOST BOOK". get_order_book() intermittently returns bid=0.01 /
# ask=0.99 for an active, liquid market while get_price() stays accurate --
# Polymarket/py-clob-client issue #180, repo archived May 2026, no fix
# coming. A book like that is not a market, it is a failed read wearing a
# market's shape, and trading against it prices a bucket at the extreme
# opposite of reality.
#
# Detection has to be a DEDICATED check, not a drift comparison: the stale
# snapshot persists unchanged across repeated fetches, so any "did the book
# move?" guard sees 0% drift and passes it.
#
# The signature is both sides pinned at once. A real far-tail bucket sits at
# bid 0.000 / ask 0.001 and a near-resolved one at bid 0.998 / ask 1.000 --
# each trips one bound but never both, which is what makes this safe to
# apply to every book fetch rather than only to mid-priced ones.
GHOST_BOOK_BID_MAX = 0.02
GHOST_BOOK_ASK_MIN = 0.98

# BALANCE PROPAGATION. update_balance_allowance() returning 200 OK does not
# mean the refreshed balance is what post_order() will read: production logs
# elsewhere show a 200, then a "balance: 0" rejection ~250ms later, then a
# correct nonzero read ~10s after that. The sync call only TRIGGERS a
# re-check on Polymarket's side. Poll until the balance reads nonzero rather
# than racing straight into post_order() on the sync response alone.
#
# 3 x 0.5s is at most 1.5s of added latency, which is nothing next to the
# FOK's own worst-price protection -- and the alternative is a rejected
# order on a live entry that had already cleared every gate.
BALANCE_POLL_ATTEMPTS = 3
BALANCE_POLL_DELAY_SEC = 0.5


# --- FOK limit padding (clients/wallet_client.py) --------------------------
# The price on a Polymarket order is a WORST-PRICE LIMIT, not a target: the
# FOK fills at the best available price up to it, or is killed. Submitting
# at exactly the observed ask therefore means any single adverse tick
# between reading the book and matching kills an order that had already
# cleared every gate -- and padding costs NOTHING when the book has not
# moved, because the fill still happens at the real price. Position.
# entry_price records the fill parsed from the response, never the limit.
#
# Denominated in TICKS, not percent, because the failure is mechanical: the
# book moved one price level. A flat percentage is meaningless across this
# registry's tick sizes, which run 0.01 on mid-book buckets and 0.001 on the
# tails -- 3% is three ticks in one place and none in the other.
LIVE_LIMIT_PAD_TICKS = 2

# ...but capped proportionally, because ticks are not proportional either.
# Two 0.001 ticks on a 0.03 bucket is 6.7%, which is no longer "absorb a
# tick" but "agree to pay a lot more". Whichever of the two binds first
# wins, and the pad is then re-aligned onto the tick grid -- so where the
# cap bites below one tick, the order simply goes out unpadded, exactly as
# it did before.
LIVE_LIMIT_PAD_MAX_PCT = 0.03


# --- Exchange reconciliation (clients/wallet_client.py, executor.py) -------
# The live blast-radius backstops (LIVE_MAX_CONCURRENT_POSITIONS,
# LIVE_MAX_TOTAL_EXPOSURE_USD, LIVE_MAX_ORDERS_PER_DAY) are all computed from
# the local positions table. That makes them caps on THIS DATABASE'S
# RECOLLECTION of exposure, not on exposure -- and the two provably diverge:
# on 2026-08-10 a real WSSS position (order 0x1ea0d2d8...) existed on the
# exchange while the daemon, running in simulation, had no row for it. During
# that window every cap read one position lighter than reality.
#
# DB_PATH is also relative to the checkout, so the same code run from a
# different clone reads an empty positions table and every cap reads zero.
#
# Reconciliation asks the exchange directly before any live entry. It fails
# CLOSED: an unexplained divergence, or an inability to check at all, blocks
# new live entries. It never blocks an EXIT -- stranding a real position is
# worse than the exposure that opened it, which is the same asymmetry
# executor._live_budget_breach() already observes.

# How far back to scan fills when hunting for positions the database does not
# know about. Long enough to cover a weather market's whole life (listed ~48h
# ahead, resolves next day) plus slack for a daemon that was down.
RECONCILE_TRADE_LOOKBACK_HOURS = 96

# Hard floor on that scan: trades before this date are not this system's
# business. ISO date, or None to scan the full lookback.
#
# Set 2026-08-11 on the operator's statement that the funding wallet has
# been disconnected from the other bot (hermes) and carries no earlier
# holdings this database should know about.
#
# WHAT THIS COSTS, stated plainly because it narrows a safety check: the
# exchange_only direction of reconciliation works by listing traded tokens
# and then asking what is still held. Anything bought BEFORE this date and
# still held is therefore invisible to it -- and invisible exposure is
# exactly what the LIVE_MAX_* caps cannot see, which is the failure the
# check exists to prevent. The assumption doing the work here is the
# operator's, not a measurement.
#
# The db_only direction is UNAFFECTED: it iterates recorded positions and
# checks each balance directly, so a stored position is verified whatever
# its age.
RECONCILE_IGNORE_TRADES_BEFORE = "2026-08-11"

# Share-count tolerance when comparing a stored position against the
# exchange's balance. Share counts are rounded to 2 decimals by the order
# builder, and a partial fill is a real divergence rather than noise, so this
# only absorbs representation error.
#
# IT MUST COVER AN EXIT'S DUST, and it does so with zero margin. Because
# build_exit_order() FLOORS the sell size onto the 2-decimal share grid
# (selling more than is held fails outright), every exit can leave up to --
# but strictly less than -- 10**-wallet_client.SHARE_DECIMALS = 0.01 shares
# behind. This tolerance is exactly that bound, so dust never trips
# reconciliation and a genuine partial fill still does.
#
# Raising SHARE_DECIMALS without raising this would silently start blocking
# live entries on dust, which is precisely the 2026-08-15 failure mode --
# a residue of 0.008885 shares reading as an unrecorded position. The
# relationship is asserted in tests/test_reconciliation_units.py rather
# than left as a coincidence between two constants in two modules.
RECONCILE_SHARE_TOLERANCE = 0.01

# Reconciliation result cache, in seconds. _live_budget_breach() runs per
# candidate entry and several candidates can clear the screen in one cycle;
# without this each one would re-scan every fill. Short enough that a
# position opened by another process is seen within a cycle.
RECONCILE_CACHE_TTL_S = 60

# Collateral balance/allowance result cache, in seconds. preflight() runs on
# EVERY simulation entry and the check is an authenticated round trip; without
# this a cycle that screens several candidates would make one call per
# candidate to re-read a number that changes only when the operator funds the
# wallet or an order fills.
COLLATERAL_STATUS_TTL_S = 60


# --- Station maturity: MEASURED, not asserted -----------------------------
# STATION_MATURITY used to be a hand-typed dict, and it was the master key:
# config.live_mode_is_permitted() ANDs it with LIVE_TRADING_STATIONS, and
# manual_trigger.py honours it and nothing else. So every gate that asked
# "is this TRADE sound?" was measured from data -- collection count, bias
# pairs, bias stderr, depth, slippage, net EV -- while the one gate that
# asked "may this STATION risk real money?" was a string somebody edited.
# That is the wrong way round for the only irreversible decision here.
#
# station_maturity() derives it instead. The criteria below are the ones
# that can be honestly computed from what is stored today.
#
# WHAT IS DELIBERATELY *NOT* A CRITERION
# ---------------------------------------
# REALISED P&L. At a plausible per-trade edge the sample needed to
# distinguish it from zero is in the hundreds of closed trades; this
# registry has 1-20 per station. Measured 2026-08-10, the whole book's P&L
# comparison was formally unmeasurable at n=3-9, and backtest/compare.py
# refuses to call a winner below 30. Writing a P&L threshold here would
# either never pass or, worse, pass on noise. It is named here so nobody
# assumes it is implied by the word "mature".
#
# CALIBRATION AGAINST THE MARKET is now a criterion -- see
# calibration_vs_market() below. It reads brier_model vs brier_market from
# the station's most recent backtest, which is the only honest source of
# point-in-time model probabilities. That closes the gap this section used
# to describe.
#
# "mature" therefore now means: the model is measurably well-behaved at this
# station AND its probabilities have beaten the market on a replayed window
# under the current code. It still does not mean the station makes money --
# see the P&L note above.

# Settlement-grade observations. Same threshold the collection-first entry
# gate uses; a station that cannot open a paper position has no business
# being called mature.
MATURITY_MIN_OBSERVATIONS = MIN_RESOLUTION_OBS_BEFORE_ENTRY

# Forecast/observation pairs. HIGHER than MIN_BIAS_PAIRS_BEFORE_ENTRY (5):
# five pairs is enough to start correcting a bias, not enough to certify a
# station for real money.
MATURITY_MIN_BIAS_PAIRS = 8

# The bias must be pinned precisely...
MATURITY_MAX_BIAS_STDERR_C = MAX_BIAS_STANDARD_ERROR_C

# ...and be the SAME BIAS over time. stderr asks how tightly the mean is
# pinned; it does not ask whether the mean is stationary. A station whose
# bias drifts has a correction fitted to its past rather than measured, and
# that failure is invisible to stderr alone. Split the pairs chronologically
# and require the halves to agree within this many combined standard errors.
MATURITY_BIAS_SPLIT_HALF_SIGMAS = 2.0

# Simulated orders that RESOLVED against the real book. This is the
# graduation criterion "simulation" was built to supply and never had: proof
# that the order path -- tick size, share rounding, the exchange minimum,
# the drift check -- actually works for THIS station's markets.
#
# Three is deliberately a floor, and is a smoke test rather than a
# statistical claim. It is not fitted to make any particular station pass:
# a station running in simulation accumulates these every cycle, so the bar
# is a question of days, not of luck.
MATURITY_MIN_SIMULATED_ORDERS = 3

# Explicit operator overrides: {icao: (maturity, justification)}.
#
# An override is a decision to trade on something other than the evidence,
# so it is visible in a diff, carries a written reason, and is logged loudly
# every time it is used -- never the quiet default it was when the whole
# thing was a literal.
#
# WSSS, set 2026-08-11. The measured criteria say exploratory, and they are
# right: the model LOSES to the market on every window scored, under both
# code versions --
#
#     Jul 28 - Aug 9  (n=5, current code)   model 0.2046  market 0.1318
#     Aug  6 - Aug 11 (n=7, current code)   model 0.1713  market 0.1444
#     Jul 28 - Aug 2  (n=32, old code)      model 0.1671  market 0.1625
#     Aug  3 - Aug  4 (n=9,  old code)      model 0.1653  market 0.1114
#
# Four windows, one direction. This override does not dispute that. It is
# an explicit decision to spend a bounded amount finding out how the real
# order path behaves -- fills, partial fills, redemption -- which no
# backtest can tell us and which simulation can only half-answer.
#
# The blast radius is what makes it defensible: $1.55-$3.75 per order, 3
# concurrent, $11.25 total exposure, 10 submissions a day. The expected
# value of those trades is NEGATIVE by this system's own best estimate.
#
# REMOVE THIS once brier_model < brier_market on n >= MATURITY_MIN_BRIER_
# ENTRIES under current code -- at which point the measured criteria will
# carry WSSS on their own and this line becomes a lie that still works.
#
# RCSS, set 2026-08-19 at the operator's instruction, mirroring WSSS.
# Measured on the box the same day (17 obs, 13 pairs), it passes FOUR of the
# six criteria and this entry overrides the other two:
#
#   [ok] observations    17 settlement-grade, need 10
#   [ok] bias_pairs      13, need 8
#   [ok] bias_precision  -1.82C pinned to +/-0.31C, need <= 0.50
#   [ok] bias_stability  -1.89 then -1.76, gap 0.13 vs 2x0.69 combined SE
#   [--] order_path      0 resolved simulated orders, need 3
#   [--] beats_market    9 entries scored (2026-08-06..08-16), need 20 --
#                        and on those 9, model Brier 0.145 vs MARKET 0.062
#
# ON THE BIAS, which the first version of this comment got wrong: -1.82C is
# the coldest bias in the registry, but MAGNITUDE IS NOT THE TEST and was
# never meant to be. The system gates on stderr precisely because a large
# bias measured tightly is correctable while a small one measured noisily is
# not, and this one is tight (+/-0.31) and stationary across halves. The
# 2026-08-09 figures that said otherwise (-1.80 +/-1.25 over 3 pairs) were
# ten days stale by the time this station was allowlisted; the correction is
# now doing its job and RCSS clears the collection-first gate on live data.
#
# THE REAL OBJECTION IS beats_market. The market's prices are more than
# TWICE as well calibrated as the model at this station (0.062 vs 0.145) --
# a wider gap than WSSS ever showed (0.144 vs 0.171 on its own n=7 window).
# n=9 is under the threshold and so decides nothing formally, but it points
# one way, and it is the criterion that most deserves the word "mature".
# This override is therefore the same bet WSSS's is -- bounded money spent
# on execution-path evidence -- and NOT a claim of edge. By this system's
# own best estimate the EV of these trades is negative.
#
#   Also unlike WSSS: Taiwan is absent from WWIS, so there is no official-
#   forecast leg at all and calibration runs on Open-Meteo + METAR alone.
#
# UNLIKE WSSS, RCSS HAS ZERO ORDER-PATH EVIDENCE (0 resolved simulated
# orders against 7 for WSSS), which is the one gap that is cheap to close
# and worth closing FIRST: `manual_trigger.py --station RCSS --mode
# simulation` needs the allowlist above only, not this override.
#
# The blast radius is the same one WSSS runs inside and is SHARED, not
# per-station: $1.55-$3.75 per order, 3 concurrent and $11.25 total across
# both stations, 10 submissions a day.
#
# REMOVE THIS once brier_model < brier_market for RCSS on n >=
# MATURITY_MIN_BRIER_ENTRIES under current code -- or sooner, if that gap
# stays where it is.
MATURITY_OVERRIDE: dict = {
    "WSSS": ("mature", "buying execution-path evidence, not edge"),
    "RCSS": ("mature", "operator decision 2026-08-19, mirroring WSSS: buying "
                       "execution-path evidence, not edge -- the market outscores "
                       "the model 0.062 to 0.145 on the 9 entries scored so far"),
}

# Frozen snapshot for the BACKTEST replica only. backtest/entry_sim.py must
# stay a pure function of its injected state -- it cannot read storage
# without (a) breaking the parity contract it exists to hold and (b) reading
# the whole stored record, which is lookahead of exactly the kind the engine
# refuses for forecast_bias_c. So replays use this frozen dict, which is
# what station_maturity() evaluated to on 2026-08-11, and any drift between
# the two shows up as a parity failure rather than silently.
MATURITY_SNAPSHOT = {
    "WSSS": "mature",
    "WMKK": "exploratory",
    "RJTT": "exploratory",
    "RKSI": "exploratory",
    "RKPK": "exploratory",
    "VHHH": "exploratory",
    "RPLL": "exploratory",
    "RCSS": "exploratory",
    "ZSPD": "exploratory",
    "ZBAA": "exploratory",
    "ZGGG": "exploratory",
    "ZGSZ": "exploratory",
    "OPKC": "exploratory",
}

_maturity_cache: dict = {}


def _bias_split_half(errors) -> tuple:
    """
    (is_stable, detail) for a station's forecast errors, split chronologically.

    Returns (None, reason) when there are too few pairs to split at all --
    which counts as NOT stable, since an unmeasurable property is not a
    satisfied one.
    """
    import statistics

    if len(errors) < 6:
        return None, f"only {len(errors)} pair(s), need 6 to split"
    mid = len(errors) // 2
    first, second = errors[:mid], errors[mid:]
    if len(first) < 2 or len(second) < 2:
        return None, "a half has fewer than 2 pairs"

    mean_a, mean_b = statistics.fmean(first), statistics.fmean(second)
    se_a = statistics.stdev(first) / (len(first) ** 0.5)
    se_b = statistics.stdev(second) / (len(second) ** 0.5)
    combined = (se_a ** 2 + se_b ** 2) ** 0.5
    gap = abs(mean_a - mean_b)
    if combined <= 0:
        return None, "zero variance in a half -- cannot compare"
    stable = gap <= MATURITY_BIAS_SPLIT_HALF_SIGMAS * combined
    return stable, (f"{mean_a:+.2f} then {mean_b:+.2f}, gap {gap:.2f} vs "
                    f"{MATURITY_BIAS_SPLIT_HALF_SIGMAS:.0f}x{combined:.2f} combined SE")


# --- Maturity criterion: does the model beat the MARKET? ------------------
# The criterion that most deserves the word "mature", and the one the first
# measured version shipped without.
#
# Beating a uniform guess proves nothing: measured 2026-08-10 the bucket
# probabilities scored Brier 0.72 against 0.91 for uniform, +11.6% skill, and
# that number is compatible with losing money on every trade. The market is
# the counterparty. If its prices are better-calibrated than the model's
# probabilities, then every "edge" the EV table reports is the model being
# wrong in a direction the book already knows about.
#
# THE SOURCE IS THE BACKTEST, deliberately. Scoring this needs POINT-IN-TIME
# model probabilities -- what the model believed on the morning of a day that
# has since settled -- and the live path keeps only the latest EV snapshot
# (ev_engine.save_ev_snapshot overwrites ev_latest_<ICAO>.json). Recomputing
# past probabilities from today's record would be lookahead, the same
# reconstruction problem that keeps forecast_bias_c pinned at 0.0 inside the
# engine. backtest/report._brier_scores() already computes both numbers
# honestly, over the entries a replay actually took, scoring the model's
# side-adjusted probability and the price paid against the settled bucket.
#
# WHAT THIS INHERITS FROM THE BACKTEST, and cannot fix here: replays price
# entries off the BID for any snapshot captured before 2026-08-10, because no
# ask was stored. brier_market is therefore scoring a price that was not
# always achievable. Both arms see the same prices so the comparison is not
# meaningless, but it is not clean either, and it will only become clean once
# enough ask-captured history accumulates.

# Scored entries required before the comparison means anything. A Brier
# difference over a handful of trades is noise; this is the same reasoning
# behind backtest/compare.py's refusal to call a winner below 30, set lower
# here because a Brier score uses every entry rather than every closed trade.
MATURITY_MIN_BRIER_ENTRIES = 20

# The replayed window must END within this many days of today. A model
# certified on July's weather is not certified on today's.
MATURITY_BRIER_MAX_WINDOW_AGE_DAYS = 14

# The run's manifest git_sha must match the current checkout.
#
# DELIBERATELY STRICT: the code is what produced the number, so changing the
# code invalidates it. This session alone changed the blend weight, replaced
# the spread estimator and moved entry pricing to the ask -- every one of
# which moves brier_model, and all four runs on the box predate them.
# Certifying a station on a number produced by superseded code is exactly
# the kind of stale evidence the whole measured-maturity change exists to
# stop.
#
# The operational cost is real: after any commit, a station is uncertified
# until a fresh backtest runs. Set False to accept older evidence, knowing
# what that means.
MATURITY_BRIER_REQUIRE_CURRENT_CODE = True


def _latest_backtest_summary(station_icao: str):
    """
    The most recently generated backtest summary for one station, as
    (summary_dict, manifest_dict), or (None, reason).
    """
    import json

    from backtest import settings as bt_settings

    base = Path(bt_settings.BACKTESTS_OUT_DIR)
    if not base.is_dir():
        return None, f"no backtest output directory at {base}"

    best = None
    for run_dir in base.iterdir():
        summary_path = run_dir / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if summary.get("station_icao") != station_icao:
            continue
        if best is None or str(summary.get("generated_at", "")) > str(best[0].get("generated_at", "")):
            manifest = {}
            manifest_path = run_dir / "manifest.json"
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    manifest = {}
            best = (summary, manifest)

    if best is None:
        return None, f"no backtest run found for {station_icao}"
    return best, ""


def _current_git_sha() -> Optional[str]:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parent),
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:  # noqa: BLE001
        return None


def calibration_vs_market(station_icao: str) -> tuple:
    """
    (passed, detail) for "does this station's model beat the market?"

    Fails on every kind of absence -- no run, too few entries, an unscorable
    run, a stale window, superseded code. An unmeasured criterion is not a
    satisfied one, and this is the criterion standing between a station and
    real money.
    """
    from datetime import date as _date

    found, reason = _latest_backtest_summary(station_icao)
    if found is None:
        return False, f"{reason} -- run `python -m backtest.cli run --station {station_icao} ...`"

    summary, manifest = found
    extras = summary.get("extras") or {}
    model = extras.get("brier_model")
    market = extras.get("brier_market")
    window = f"{summary.get('start_date')}..{summary.get('end_date')}"

    if model is None or market is None:
        return False, f"run {summary.get('run_id')} ({window}) scored nothing"

    # COUNT WHAT WAS SCORED, NOT WHAT WAS TRADED. These differ: an entry is
    # only scorable once its target date has a published observation, so a
    # run that traded today's market carries entries no Brier term exists
    # for. This used to read counters.n_entries, and WMKK's 2026-08-15 run
    # reported n_entries 16 against Brier means of 14. Counting 16 toward a
    # 20-entry minimum inflates the evidence behind the ONE criterion that
    # authorises real money, in the direction of passing it early.
    #
    # A run predating brier_n fails rather than falling back to n_entries:
    # an unmeasured criterion is not a satisfied one, and the fallback would
    # silently reinstate the exact overcount this replaced. Re-run the
    # backtest -- the same requirement MATURITY_BRIER_REQUIRE_CURRENT_CODE
    # already imposes on every summary written by superseded code.
    brier_n = extras.get("brier_n")
    if brier_n is None:
        return False, (f"run {summary.get('run_id')} ({window}) predates brier_n and "
                       f"cannot say how many entries it scored -- re-run the backtest")

    if brier_n < MATURITY_MIN_BRIER_ENTRIES:
        n_entries = (summary.get("counters") or {}).get("n_entries") or 0
        unscored = (f", {n_entries - brier_n} entered but unscored"
                    if n_entries > brier_n else "")
        return False, (f"only {brier_n} entry(s) scored in {window}{unscored}, "
                       f"need {MATURITY_MIN_BRIER_ENTRIES}")

    if MATURITY_BRIER_REQUIRE_CURRENT_CODE:
        run_sha = manifest.get("git_sha")
        head = _current_git_sha()
        if not run_sha or not head:
            return False, "cannot confirm the run was produced by the current code"
        if run_sha != head:
            return False, (f"run was produced by {run_sha[:8]}, HEAD is {head[:8]} -- "
                           f"the code that made this number has changed; re-run the backtest")

    try:
        end = _date.fromisoformat(str(summary.get("end_date")))
        age = (_date.today() - end).days
        if age > MATURITY_BRIER_MAX_WINDOW_AGE_DAYS:
            return False, (f"window {window} ended {age} days ago, "
                           f"stale past {MATURITY_BRIER_MAX_WINDOW_AGE_DAYS}")
    except (TypeError, ValueError):
        return False, f"unparseable window {window}"

    if model < market:
        return True, (f"model {model:.4f} beats market {market:.4f} "
                      f"by {market - model:.4f} over {brier_n} scored entries in {window}")
    return False, (f"model {model:.4f} LOSES to market {market:.4f} "
                   f"by {model - market:.4f} over {brier_n} scored entries in {window}")


def maturity_report(station_icao: str) -> dict:
    """
    Every maturity criterion for one station, with its value and verdict.

    Reads storage, so it is NOT usable from backtest/entry_sim.py -- see
    MATURITY_SNAPSHOT. Never raises: an unreadable criterion is a FAILED
    criterion, because "I could not check" must not read as "it passed".
    """
    import calibration
    import storage

    criteria = {}
    try:
        station = get_station(station_icao)
    except KeyError:
        return {"station": station_icao, "mature": False,
                "criteria": {"registered": (False, "not in the station registry")}}

    try:
        obs = storage.count_observations_from_source(station_icao, station.resolution_grade_source)
        criteria["observations"] = (
            obs >= MATURITY_MIN_OBSERVATIONS,
            f"{obs} settlement-grade, need {MATURITY_MIN_OBSERVATIONS}",
        )
    except Exception as exc:  # noqa: BLE001
        criteria["observations"] = (False, f"unreadable ({exc})")

    try:
        errors = storage.forecast_error_samples(station_icao, station.resolution_grade_source)
        bias, n_pairs, stderr = calibration.bias_stats(errors)
        criteria["bias_pairs"] = (
            (n_pairs or 0) >= MATURITY_MIN_BIAS_PAIRS,
            f"{n_pairs or 0} pair(s), need {MATURITY_MIN_BIAS_PAIRS}",
        )
        criteria["bias_precision"] = (
            stderr is not None and stderr <= MATURITY_MAX_BIAS_STDERR_C,
            f"stderr {stderr:.2f}C, need <= {MATURITY_MAX_BIAS_STDERR_C}"
            if stderr is not None else "no bias measured",
        )
        stable, detail = _bias_split_half(errors)
        criteria["bias_stability"] = (bool(stable), detail)
    except Exception as exc:  # noqa: BLE001
        criteria["bias_pairs"] = (False, f"unreadable ({exc})")
        criteria["bias_precision"] = (False, "not evaluated")
        criteria["bias_stability"] = (False, "not evaluated")

    try:
        history = storage.load_position_history(station_icao, limit=1000)
        sims = [p for p in history if getattr(p, "execution_mode", "") == "simulation"]
        criteria["order_path"] = (
            len(sims) >= MATURITY_MIN_SIMULATED_ORDERS,
            f"{len(sims)} simulated order(s) resolved, need {MATURITY_MIN_SIMULATED_ORDERS}",
        )
    except Exception as exc:  # noqa: BLE001
        criteria["order_path"] = (False, f"unreadable ({exc})")

    try:
        criteria["beats_market"] = calibration_vs_market(station_icao)
    except Exception as exc:  # noqa: BLE001
        criteria["beats_market"] = (False, f"unreadable ({exc})")

    return {
        "station": station_icao,
        "mature": all(passed for passed, _ in criteria.values()),
        "criteria": criteria,
    }


def station_maturity(station_icao: str) -> str:
    """
    "mature" or "exploratory", DERIVED from stored evidence.

    Cached per process: it reads storage and is consulted once per candidate
    entry. The criteria move on the timescale of days, so a long-lived
    daemon picking up a promotion at its next restart is the correct
    granularity -- and it means a station cannot be promoted mid-cycle by a
    row landing halfway through an evaluation.
    """
    override = MATURITY_OVERRIDE.get(station_icao)
    if override:
        maturity, why = override
        print(
            f"[config] MATURITY OVERRIDE: {station_icao} forced to '{maturity}' by "
            f"config.MATURITY_OVERRIDE, bypassing the measured criteria. Reason: {why}"
        )
        return maturity

    if station_icao in _maturity_cache:
        return _maturity_cache[station_icao]
    result = "mature" if maturity_report(station_icao)["mature"] else "exploratory"
    _maturity_cache[station_icao] = result
    return result


def print_maturity_report(station_icao: str) -> None:
    """Human-readable criterion-by-criterion breakdown."""
    report = maturity_report(station_icao)
    verdict = "MATURE" if report["mature"] else "exploratory"
    print(f"{station_icao}: {verdict}")
    for name, (passed, detail) in report["criteria"].items():
        print(f"  [{'ok' if passed else '--'}] {name:<16} {detail}")


# Built from the measured criteria, for readers that want the whole mapping
# (the backtest manifest, paper_trading_report). Gating code must call
# station_maturity() instead: this is evaluated once at import, and a
# station promoted later in the process's life would not appear here.
class _MaturityMapping(dict):
    """dict-alike that derives on access, so .get() stays honest."""

    def __getitem__(self, key):
        return station_maturity(key)

    def get(self, key, default="exploratory"):
        if key not in STATIONS:
            return default
        return station_maturity(key)

    def items(self):
        return [(icao, station_maturity(icao)) for icao in STATIONS]

    def __contains__(self, key):
        return key in STATIONS

    def keys(self):
        return STATIONS.keys()

    def values(self):
        return [station_maturity(icao) for icao in STATIONS]

    def __iter__(self):
        return iter(STATIONS)

    def __len__(self):
        return len(STATIONS)


STATION_MATURITY = _MaturityMapping()
