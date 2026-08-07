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
        long_term_normal_max_c=32.2,  # placeholder -- confirm against MET Malaysia's
                                       # published climatological normals before relying
                                       # on this for real calibration (see framework doc:
                                       # Polymarket resolves KL to WMKK's Wunderground page)
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
        long_term_normal_max_c=31.3,  # placeholder (Tokyo Aug 1991-2020 normal) --
                                       # confirm against JMA's published normals for
                                       # Haneda itself before trusting for calibration
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
        long_term_normal_max_c=29.1,  # placeholder (Incheon Aug normal) -- confirm
                                       # against KMA's published normals before trusting
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
        long_term_normal_max_c=30.8,  # placeholder (Busan Aug normal) -- confirm
                                       # against KMA's published normals before trusting
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
        long_term_normal_max_c=31.6,  # placeholder (HKO Aug 1991-2020 normal) --
                                       # confirm against HKO's published normals
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
        long_term_normal_max_c=30.9,  # placeholder (Manila Aug normal) -- confirm
                                       # against PAGASA's published normals
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
        long_term_normal_max_c=34.3,  # placeholder (Taipei Aug normal) -- confirm
                                       # against CWA's published normals
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
        long_term_normal_max_c=32.2,  # placeholder (Shanghai Aug normal; Pudong's
                                       # coastal site may run cooler) -- confirm
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
        long_term_normal_max_c=30.3,  # placeholder (Beijing Aug normal) -- confirm
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
        long_term_normal_max_c=33.4,  # placeholder (Guangzhou Aug normal) -- confirm
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
        long_term_normal_max_c=32.5,  # placeholder (Shenzhen Aug normal) -- confirm
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
        long_term_normal_max_c=31.7,  # placeholder (Karachi Aug monsoon normal) -- confirm
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
PROFIT_TAKE_PCT = 0.50      # exit once unrealized gain hits +50% of entry price
STOP_LOSS_PCT = 0.30        # exit once unrealized loss hits -30% of entry price

# Trailing stop: once a position's gain (measured from its high-water mark,
# not just current price) crosses TRAILING_STOP_ACTIVATION_PCT, the fixed
# PROFIT_TAKE_PCT target above is superseded -- the position is allowed to
# keep running, but protected by a stop that trails the peak price down by
# TRAILING_STOP_PCT. This lets a strong move keep paying out past the fixed
# target while still locking in most of the gain if it reverses, rather
# than capping every winner at the same fixed percentage.
# Deliberately set the activation threshold BELOW the fixed profit-take so
# trailing takes over before the hard cap would otherwise fire.
TRAILING_STOP_ACTIVATION_PCT = 0.25   # start trailing once peak gain reaches +25%
TRAILING_STOP_PCT = 0.15              # exit if price falls 15% off its peak, once trailing is active

# After this local hour, tighten both thresholds (see risk_manager.py) --
# reflects the edge-decay curve: once the primary trading window closes,
# be quicker to lock in gains and quicker to cut losses, since there's no
# more new edge coming to justify holding through volatility.
EDGE_DECAY_TIGHTEN_HOUR_LOCAL = 10  # 10:00 local, per the scanning-schedule analysis
TIGHTENED_PROFIT_TAKE_PCT = 0.25
TIGHTENED_STOP_LOSS_PCT = 0.15
TIGHTENED_TRAILING_STOP_ACTIVATION_PCT = 0.15  # trail sooner once edge is decaying
TIGHTENED_TRAILING_STOP_PCT = 0.08             # and trail tighter -- lock in gains faster

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
# upside exits (profit-take, trailing stop) and resolution detection
# still apply.
LOTTERY_PRICE_THRESHOLD = 0.15

# After this many stop-loss exits on the same (station, date, bucket, side),
# entries there are blocked for the rest of the day. The per-bucket open-
# position cap stops STACKING but has no memory of exits, so on 2026-08-03
# the same bucket ran a "stop -> re-buy -> stop" churn loop, paying the
# spread on every lap. One stop-out is the market's answer for the day.
MAX_STOP_OUTS_PER_BUCKET_PER_DAY = 1

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
MAX_SINGLE_CYCLE_MOVE = 0.5

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
    # 08:00-10:00 used to tighten both the EV bar (0.15->0.25) AND the scan
    # interval (10min->30min) in one step at 08:00 -- two independent
    # "be more conservative" levers moving together meant real
    # opportunities in the 08:00-09:00 hour could be missed to sparse
    # polling on top of a stricter EV bar, which is a different failure
    # mode than "rejected for insufficient EV." Split into two steps so
    # the interval widens gradually instead of tripling in one jump:
    # bar tightens first, interval widens second.
    (8, 0, 9, 0, 15, "secondary", 0.20, "Edge decaying (early) -- EV bar raised, scan interval only modestly wider"),
    (9, 0, 10, 0, 30, "secondary", 0.25, "Edge decaying (late) -- original wider interval, highest pre-close EV bar"),
    (10, 0, 12, 0, 120, "monitor_only", None, "Decision-closed -- no new entries, watching existing positions"),
    (12, 0, 16, 0, 60, "risk_only", None, "Afternoon peak-heat window -- nowcast-triggered risk checks only"),
    (16, 0, 22, 45, 180, "monitor_only", None, "Evening -- sparse position monitoring"),
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

# Hard per-trade cap, independent of what Kelly sizing alone would suggest --
# a large enough apparent edge should never translate into betting the whole
# bankroll on one bucket of one station's market.
MAX_POSITION_USD = 150.0

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
STATION_MATURITY = {
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
EXPLORATORY_SIZE_MULTIPLIER = 0.20  # exploratory stations get 20% of what mature-station sizing would suggest

# A station may not open ANY position until this many stored observations
# exist whose source is its own resolution_grade_source. A brand-new station
# has an unmeasured model bias, a placeholder climatological normal, and
# (usually) spread_source="fallback_default" -- a 2C-wrong placeholder
# produces "edges" squarely inside the tradeable band, and every one of
# those trades is wrong. Collection costs nothing; wrong entries don't.
# New stations therefore start collection-only automatically and graduate
# by simply existing for a few days.
MIN_RESOLUTION_OBS_BEFORE_ENTRY = 5

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
