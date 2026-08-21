"""
models.py

PURPOSE
-------
Plain dataclasses that every other module passes around. Keeping these
in one place means clients, calibration, and probability modules all
agree on shape without importing each other directly -- reduces
circular-import risk and makes the pipeline's data flow easy to read.

StationConfig is the key addition for multi-station support: adding a
new station (e.g. WMKK) means creating one StationConfig entry plus,
if it needs a new official data source, one small adapter class in
clients/official/ -- nothing else in the codebase hardcodes "Changi"
or "Singapore" anymore.

DEPENDENCIES
------------
Standard library only (dataclasses, datetime, typing).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Dict


@dataclass
class StationConfig:
    """
    Everything the pipeline needs to know about one weather station /
    market. Register new stations in config.STATIONS -- nothing else
    needs to change to add a station whose official source already has
    an adapter in clients/official/.
    """
    icao: str                          # e.g. "WSSS", "WMKK"
    display_name: str                  # e.g. "Singapore Changi Airport"
    country: str                       # e.g. "Singapore", "Malaysia"
    lat: float
    lon: float
    wunderground_slug: str             # path segment, e.g. "sg/singapore/WSSS"
    long_term_normal_max_c: float      # climatological reference daily max
    official_client_key: str           # lookup key into clients.official.registry
    polymarket_city_slug: str = ""     # e.g. "singapore" -- used to build Gamma API event slugs
    monsoon_phase_by_month: Dict[int, str] = field(default_factory=dict)
    seed_observations: list = field(default_factory=list)  # list of (date, temp_c) tuples

    # Fixed UTC offset of the station's market-local calendar day. A plain
    # integer, not a tz database entry, because NONE of the registered cities
    # observes DST -- that is an assumption that happens to hold for every
    # Asian market listed so far, not a general truth; re-check before
    # registering a station in a DST region.
    utc_offset_hours: int = 8

    # Sanity CROSS-CHECK bounds for this station's Polymarket bucket range.
    # NOT the source of truth on the trading path: Polymarket shifts a city's
    # 11-bucket window seasonally (Singapore moved 25-35 -> 27-37 between July
    # and August 2026), so live trading derives the real bounds from the
    # discovered token map each cycle and only logs when these drift.
    bucket_min_c: int = 25
    bucket_max_c: int = 35

    # How a raw settlement reading maps to a bucket:
    #   "half_up" -- source reports whole degrees C (METAR/Wunderground);
    #                bucket = round-half-up(temp), intervals [b-0.5, b+0.5).
    #   "floor"   -- source reports 0.1 C precision and the market resolves to
    #                the range that CONTAINS the reading (Hong Kong Observatory);
    #                bucket = floor(temp), intervals [b, b+1).
    bucket_edge_mode: str = "half_up"

    # City name in the WMO WWIS index (worldweather.wmo.int) for the generic
    # "wwis" official client. Empty string = city not listed there (e.g.
    # Taipei -- WWIS is a UN service); the client then returns None honestly.
    wwis_city_name: str = ""

    # ObservedReading.source string that counts as settlement-grade truth for
    # THIS station. METAR is only resolution-grade where the market actually
    # settles on the airport's Wunderground record; Hong Kong settles on the
    # HK Observatory's climate extract instead ("hko_daily_max").
    resolution_grade_source: str = "metar_daily_max"

    # What the METAR ingest is allowed to do for this station:
    #   "resolution" -- save daily maxima as "metar_daily_max" (settlement grade)
    #   "proxy"      -- save as "metar_daily_max_proxy" (rank 1, never settles;
    #                   Karachi: the market names "Masroor Airbase" but links
    #                   OPKC -- unconfirmed station identity)
    #   "skip"       -- do not ingest METAR at all (Hong Kong: the airport
    #                   reading is systematically cooler than the urban HKO
    #                   settlement station and would bias the observation blend)
    metar_ingest_mode: str = "resolution"

    @property
    def wunderground_history_url(self) -> str:
        return f"https://www.wunderground.com/history/daily/{self.wunderground_slug}"


@dataclass
class PointForecast:
    """A single point forecast for the day's max temperature, from one source, for one station."""
    station_icao: str
    source: str            # e.g. "nea_24hr", "open_meteo_ecmwf", "accuweather"
    target_date: date
    max_temp_c: Optional[float]
    fetched_at: str        # ISO timestamp string
    raw_note: str = ""     # freeform context, e.g. NEA's named rain zones


@dataclass
class ObservedReading:
    """An actual recorded value for a past date, for one station, used for backtesting/calibration."""
    station_icao: str
    target_date: date
    max_temp_c: float
    source: str             # e.g. "climate_monitor", "wunderground_manual"


@dataclass
class CalibratedEstimate:
    """Output of the calibration step: a bias-corrected central estimate + spread, for one station."""
    station_icao: str
    target_date: date
    central_estimate_c: float
    std_dev_c: float
    monsoon_phase: str
    inputs_used: list = field(default_factory=list)  # list of PointForecast.source strings
    notes: str = ""
    # Where std_dev_c came from, in order of trustworthiness:
    # "ensemble" (real spread across ensemble members) > "forecast_variance"
    # (spread across point forecasts) > "observed_variance" (spread across
    # recent observed readings) > "fallback_default" (no real spread data
    # at all -- a flat 1.2C guess). Set by calibration.estimate_std_dev().
    # Downstream (entry_manager.py) uses this to require a bigger edge
    # before trading on a "fallback_default" estimate, since an edge that
    # size is only as trustworthy as the spread it was computed against.
    spread_source: str = "fallback_default"
    # Measured mean (forecast - settled truth) for this station, in degrees
    # C, ALREADY SUBTRACTED from the forecast term of central_estimate_c by
    # calibration.blend_central_estimate(). Recorded so a stored estimate
    # says how much correction it carries: 0.0 means uncorrected, either
    # because the station has no measured bias yet or because
    # config.ENABLE_FORECAST_BIAS_CORRECTION is off.
    forecast_bias_c: float = 0.0


@dataclass
class BucketProbability:
    """Probability mass assigned to one whole-degree-C Polymarket-style bucket."""
    bucket_c: int
    probability: float


@dataclass
class Position:
    """
    An open (or closed) trade on one bucket of one station's market.
    Tracked independently of resolution -- a position can be entered
    and exited entirely before the underlying weather outcome is known.
    """
    position_id: str        # unique id, e.g. f"{station_icao}:{target_date}:{bucket_c}:{side}:{entry_time}"
    station_icao: str
    target_date: date
    bucket_c: int
    side: str               # "YES" or "NO"
    entry_price: float      # price per share at entry, in dollars (e.g. 0.32)
    size_usd: float         # dollar amount staked
    entry_time: str         # ISO timestamp
    # The EXACT set of status strings actually written anywhere in this codebase:
    #   "open"                 -- executor.open_position()
    #   "closed_take_profit"   -- executor.close_position(), from ExitDecision.reason
    #   "closed_stop_loss"     -- executor.close_position(), from ExitDecision.reason
    #   "closed_trailing_stop" -- executor.close_position(), from ExitDecision.reason
    #   "closed_resolution"    -- position_manager._close_as_resolved(), passed explicitly
    # The first three closed_* strings are derived as f"closed_{decision.reason}"
    # from the reasons risk_manager.evaluate_exit() sets should_exit=True on, so
    # they track that function exactly. "closed_resolution" is passed explicitly
    # instead, precisely so a resolved market can never be filed as a stop-loss.
    status: str = "open"
    high_water_mark: float = None  # best price seen since entry; defaults to entry_price -- drives the trailing stop
    exit_price: float = None
    exit_time: str = None
    exit_reason: str = ""
    token_id: Optional[str] = None   # Polymarket CLOB token id for this side/bucket -- lets
                                       # position_manager price-check this position live without
                                       # needing rediscovery (see entry_manager -> executor wiring)
    is_paper: bool = False           # True for paper-trading positions -- never touches wallet_client,
                                       # tracked separately so paper P&L never mixes with real capital
    size_shares: Optional[float] = None  # actual number of outcome-token SHARES held. size_usd alone is
                                       # not enough to exit a real position: the exit must sell an exact
                                       # share count, and re-deriving it as size_usd/entry_price at exit
                                       # time uses the wrong price and asks to sell shares that do not
                                       # exist. None for paper/manual_review positions, where no real
                                       # shares exist.
    execution_mode: str = "paper"    # which executor mode opened this position -- one of
                                       # "manual_review", "paper", "simulation", "live". Replaces is_paper
                                       # as the real discriminator now that there are four modes rather
                                       # than two; is_paper is retained and stays True for every mode
                                       # except "live", so existing paper-vs-real queries keep their
                                       # current meaning.
    order_id: Optional[str] = None   # exchange order id for a live fill, so a stored position can be
                                       # reconciled against the exchange by hand. None in every non-live
                                       # mode.

    # WHAT THE MODEL BELIEVED WHEN IT ENTERED. Recorded so a closed trade can
    # be scored against its own prediction rather than only against P&L.
    #
    # Without these the EV engine was unfalsifiable on live data: every entry
    # asserted an edge and a positive net EV, but nothing persisted the
    # assertion, so "was the edge real?" could only be asked of backtest
    # replays -- the track that disagreed with the live paper book by 40
    # percentage points at WMKK (2026-08-16: backtest +30.3% vs paper -10.6%
    # dollar-weighted over 30 trades).
    #
    # READ THEM AGAINST RESOLUTION, NOT AGAINST EXIT P&L. model_prob is
    # P(this side wins AT SETTLEMENT); a position closed by a stop or a
    # take-profit never tested that claim. At WMKK 29 of 30 closed paper
    # trades exited before resolution, so exit P&L scores the exit rules and
    # these fields score the forecast -- different questions.
    #
    # None means NO MODEL RAN (manual_trigger asserts a trade with the model
    # gates bypassed), which is a different claim from 0.0. Rows written
    # before this field existed also read None, and unknown must not be
    # mistaken for measured.
    model_prob: Optional[float] = None      # EVResult.model_prob -- already side-adjusted,
                                              # i.e. P(the side actually taken wins)
    raw_edge: Optional[float] = None        # EVResult.raw_edge -- model_prob - market_price
    net_ev_at_size: Optional[float] = None  # EntryDecision.net_ev_at_size -- net EV per dollar
                                              # re-checked at the size actually ordered

    def __post_init__(self):
        if self.high_water_mark is None:
            self.high_water_mark = self.entry_price


@dataclass
class ExitDecision:
    """Output of risk_manager's exit evaluation for one open position."""
    position_id: str
    should_exit: bool
    # Reasons actually produced: risk_manager.evaluate_exit() sets "stop_loss",
    # "trailing_stop", "take_profit" (should_exit=True) and "trailing_active",
    # "hold" (should_exit=False); position_manager adds "resolution"
    # (should_exit=True) and "resolution_unknown" (should_exit=False).
    reason: str
    current_price: float
    pnl_pct: float             # unrealized P&L, as a fraction (0.25 = +25%)


@dataclass
class MarketQuote:
    """
    Live YES/NO quotes (and, where available, book depth) for one bucket.

    yes_price/no_price are ENTRY prices -- the ASK, what a purchase pays.
    They feed raw_edge and the limit price on an order, both of which are
    about buying. They were the BID until 2026-08-10, which overstated
    every edge by the spread.

    yes_bid/no_bid are the other side, carried alongside rather than
    instead: the snapshot capture that feeds the backtest stores the bid
    (its historical series is bids, and switching the column would make
    old and new rows silently incomparable), while the entry path needs
    the ask. One fetch pass, both sides, no ambiguity about which is which.
    """
    bucket_c: int
    yes_price: Optional[float]              # ASK -- what an entry pays
    no_price: Optional[float]               # ASK
    yes_depth_usd: Optional[float] = None   # top-of-book size available at yes_price, if known
    no_depth_usd: Optional[float] = None
    fetched_at: str = ""
    yes_bid: Optional[float] = None         # BID -- what a sale receives
    no_bid: Optional[float] = None


@dataclass
class EVResult:
    """
    Expected-value estimate for buying one side of one bucket, combining
    the calibrated model probability with a REAL market quote (not the
    illustrative "naive market" used in earlier hand-calculated examples).
    """
    station_icao: str
    target_date: date
    bucket_c: int
    side: str                    # "YES" or "NO"
    model_prob: float
    market_price: Optional[float]
    raw_edge: Optional[float]           # model_prob - market_price
    estimated_slippage_pct: float       # cost estimate from walking the book, as a fraction
    fee_rate_pct: float
    net_ev_per_dollar: Optional[float]  # (raw_edge / market_price) - slippage - fee, None if price unavailable
    spread_source: str = "fallback_default"  # from CalibratedEstimate.spread_source -- see entry_manager's
                                              # confidence-scaled MIN_ABS_RAW_EDGE gate
    notes: str = ""


@dataclass
class EntryDecision:
    """
    Output of entry_manager's sizing/gating logic for one candidate
    trade. approved=False means "do not place this trade" -- the
    reason field always explains why, whether approved or not.
    """
    station_icao: str
    target_date: date
    bucket_c: int
    side: str
    kelly_fraction_raw: float        # full-Kelly fraction, before any fractional/cap adjustments
    kelly_fraction_applied: float    # after KELLY_FRACTION multiplier
    recommended_size_usd: float      # after all caps (bankroll, depth, station maturity)
    available_depth_usd: Optional[float]
    slippage_at_size_pct: Optional[float]  # slippage re-estimated at the ACTUAL recommended size
    net_ev_at_size: Optional[float]        # net EV re-checked at the actual recommended size
    approved: bool
    reason: str
    station_maturity: str            # "mature" or "exploratory"
    entry_price: Optional[float] = None    # price at evaluation time -- executor uses this, not a refetch,
                                             # so what gets logged matches what was actually decided on
    token_id: Optional[str] = None         # Polymarket CLOB token id for this specific bucket/side --
                                             # carried through to Position so position_manager can price-check
                                             # it later without needing to rediscover it (closes a real gap:
                                             # previously nothing stored this, so exit-side price checks
                                             # couldn't run at all)
    # Carried from the EVResult this decision was sized from, purely so
    # executor can persist them on the Position -- see Position.model_prob for
    # why. None on any decision built without a model behind it.
    model_prob: Optional[float] = None
    raw_edge: Optional[float] = None
    # The net-EV bar this decision was evaluated against -- evaluate_entry's
    # min_net_ev, which is a per-scan-window figure rather than a constant.
    # Carried so that a re-check AFTER the order is resolved (executor's
    # _resolved_size_ok, which runs at the notional the exchange minimum
    # forces) can re-run the same test the entry passed instead of a weaker
    # stand-in. None only on decisions not built by evaluate_entry or its
    # backtest replica -- manual_trigger, which bypasses the model gates and
    # so has no bar to carry (it also leaves net_ev_at_size None, which
    # skips the re-check that reads this).
    min_net_ev: Optional[float] = None


@dataclass
class BacktestResult:
    """
    One walk-forward evaluation: what the calibration WOULD have
    predicted for target_date, using only information available as of
    that prediction (strictly no data from target_date or later),
    compared against what actually happened.
    """
    station_icao: str
    target_date: date
    predicted_c: float
    actual_c: float
    error_c: float               # predicted - actual (positive = over-predicted)
    lead_time_days: Optional[int] = None  # None when lead-time isn't applicable (observed-only mode)
    method: str = ""             # "observed_only" or "lead_time_forecast"


@dataclass
class BacktestSummary:
    """Aggregated error statistics for a set of BacktestResults, optionally segmented by lead time."""
    station_icao: str
    method: str
    lead_time_days: Optional[int]   # None if aggregated across all lead times
    n: int
    mean_error_c: float              # bias -- positive means the model runs hot vs. actual, on average
    mae_c: float                     # mean absolute error
    std_error_c: float
