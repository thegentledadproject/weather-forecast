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

from models import StationConfig

# --- Trading-day clock ----------------------------------------------------
# Both registered stations (and the markets they trade) live in UTC+8
# (SGT/MYT). The deployment box runs on UTC, where date.today() is still
# YESTERDAY for the first eight hours of the local day -- including the
# entire 05:00-08:00 primary entry window. Every forecast fetched in that
# window was being labeled with the previous day's date, and the trading
# cycle was calibrating for (and discovering the market of) a day that had
# already ended. Any code that needs "today" in the trading sense MUST use
# local_today(), never date.today().
LOCAL_UTC_OFFSET_HOURS = 8


def local_today() -> date:
    """The current calendar date in the market's timezone (UTC+8)."""
    return (datetime.now(timezone.utc) + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)).date()


# --- Observation source ranking -------------------------------------------
# Polymarket settles these markets on Wunderground's station history, which
# is the airport METAR record. clients/metar_client.py ingests exactly that
# (source below), so when several sources report the same day, settlement-
# grade truth must win: METAR first, then any other fetched reading (e.g.
# the Open-Meteo analysis backfill, "openmeteo_recent_actual"), with the
# hand-maintained seed constants last. Used by resolution picking in the
# backtest AND by dedup before calibration blending -- two rows for one day
# would otherwise double-count it in the observed mean.
RESOLUTION_GRADE_OBSERVATION_SOURCE = "metar_daily_max"


def observation_source_rank(source: str) -> tuple:
    """Sort key: lower ranks win. Deterministic across ties via the name."""
    if source == RESOLUTION_GRADE_OBSERVATION_SOURCE:
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
# CONFIRMED against real Polymarket data (multiple live pulls across this
# project): Singapore/KL temperature-bracket markets consistently list 11
# outcomes -- "25 or below", 26 through 34 individually, then "35 or above".
# BUCKET_MIN_C/MAX_C represent the two EDGE buckets themselves (25 and 35),
# not the lowest/highest explicit numbers -- range(BUCKET_MIN_C, BUCKET_MAX_C+1)
# must yield exactly 11 values to match the real market structure.
# Previously set to 27/36 (10 buckets, shifted and wrong on both ends) --
# corrected after rechecking against live order-book data.
BUCKET_MIN_C = 25
BUCKET_MAX_C = 35

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
# All times are LOCAL (SGT/MYT, UTC+8 -- both registered stations share this
# offset). Defined as (start_hour, start_min, end_hour, end_min, interval_min,
# mode, min_net_ev, description).
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
# measured bias-correction edge (14+ days of observed history). WMKK does
# not yet -- any WMKK trade is exploratory until it earns the same status,
# so it's sized down hard rather than treated as equivalent-confidence.
STATION_MATURITY = {
    "WSSS": "mature",
    "WMKK": "exploratory",
}
EXPLORATORY_SIZE_MULTIPLIER = 0.20  # exploratory stations get 20% of what mature-station sizing would suggest

# Shared budget across ALL approved legs for one station on one day -- e.g.
# a YES leg on the top bucket plus NO legs hedging tail buckets, opened
# together (see entry_manager.apply_portfolio_budget). Without this, each
# leg is sized independently up to MAX_POSITION_USD and the combined total
# exposure for one station/day is unbounded. Set higher than a single
# MAX_POSITION_USD to allow a real multi-leg basket, but still capped well
# below "every leg at max size simultaneously."
MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD = 250.0
