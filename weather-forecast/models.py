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

    # The station's STANDARD-time (winter) UTC offset -- a plain integer, not
    # a tz database entry. This is the value the backtest engine reads
    # directly (backtest/engine.py): the backtest has no moving clock, so it
    # always trades on this static number. A DST-observing station
    # additionally sets iana_timezone below, which
    # config.current_utc_offset_hours() resolves at call time for the LIVE
    # path -- but utc_offset_hours here must still be set, and must still be
    # the winter value, even then.
    #
    # Historical note: this field was originally a plain int because NONE of
    # the registered cities observed DST. That was an assumption that
    # happened to hold for every Asian market listed at the time, not a
    # general truth -- it no longer holds now that European stations are
    # registered, which is why iana_timezone exists.
    utc_offset_hours: int = 8

    # Which capital pool and blast radius this station draws from. Every
    # per-region limit in config.py (REGION_BANKROLL_USD,
    # REGION_MAX_DAILY_EXPOSURE_USD, REGION_LIVE_MAX_*) is keyed off this
    # string. Defaults to "asia" so the 13 stations registered before
    # 2026-08-24 keep the single shared pool they already had, unedited.
    region: str = "asia"

    # IANA timezone name, e.g. "Europe/London". When set, it OVERRIDES
    # utc_offset_hours on the live trading path via
    # config.current_utc_offset_hours() -- the offset is resolved from the
    # tz database at call time, so a DST-observing station is correct in
    # both halves of the year. When None (every Asian station), the static
    # utc_offset_hours above is used exactly as before.
    #
    # utc_offset_hours MUST STILL BE SET even when this is: the backtest
    # engine reads station.utc_offset_hours directly (backtest/engine.py)
    # and has no notion of a moving clock. Set it to the station's
    # STANDARD-time (winter) offset.
    iana_timezone: Optional[str] = None

    # Sanity CROSS-CHECK bounds for this station's Polymarket bucket range,
    # EXPRESSED IN bucket_unit (below) -- the `_c` suffix is historical and
    # is NOT a claim that these are Celsius. bucket_axis.for_station() is
    # authoritative for what a bucket number means; the field name never is.
    #
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

    # The MARKET's bucket axis. See bucket_axis.BucketAxis.
    #
    # bucket_unit is the unit of the market's bucket LABELS -- and therefore
    # of bucket_min_c/bucket_max_c above and of every bucket_c key. It is NOT
    # the unit of any temperature: forecasts, std_dev, observations, midpoints
    # and bias are Celsius everywhere, always.
    #
    # Defaults reproduce every market registered before 2026-08. The American
    # cities are the first exception: 11 of the 15 list Fahrenheit in
    # two-degree buckets ("70-71°F"), so they set ("F", 2).
    bucket_unit: str = "C"
    bucket_step: int = 1

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

    # How many METARs this station files per LOCAL day when reporting
    # normally. A half-hourly-filing airport (most of the Asia/Europe
    # registry) files ~48/day; an hourly-filing one (every US ASOS
    # station) files ~24/day. clients/metar_client.py derives its daily
    # coverage floor as half of this number rather than hard-coding it,
    # so an hourly station isn't held to a half-hourly station's bar --
    # see MIN_REPORTS_PER_DAY there for why a floor exists at all.
    expected_metar_reports_per_day: int = 48

    def __post_init__(self):
        # clients/metar_client.py derives its coverage floor as
        # expected_metar_reports_per_day // 2. An unvalidated 0 or 1 would
        # derive a floor of 0 -- daily_max_temp_c would then either accept
        # a day with NO corroborating reports at all, or crash calling
        # max() on an empty list. Both are the wrong failure mode for a
        # codebase that otherwise always prefers refusing to trade over
        # trading on a number it can't justify -- so refuse construction
        # instead of silently deriving a permissive floor. 2 is the floor
        # of sane: a half-hourly-filing airport reports ~48/day, an
        # hourly-filing one (every US ASOS station) reports ~24/day --
        # anything below 2 isn't a real filing cadence.
        if self.expected_metar_reports_per_day < 2:
            raise ValueError(
                f"{self.icao}: expected_metar_reports_per_day="
                f"{self.expected_metar_reports_per_day} is not a plausible "
                "METAR filing cadence (must be >= 2). A half-hourly-filing "
                "airport reports ~48/day; an hourly-filing one (every US "
                "ASOS station) reports ~24/day."
            )

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
    # THE BID FOR THIS SIDE AT THE INSTANT OF ENTRY. entry_price above is the
    # ASK -- what was actually paid -- and stays the basis for P&L, sizing and
    # the risk unit. This is the same book's other side, recorded because the
    # STOP measures MOVEMENT and an open position is marked at the bid: taking
    # entry_price - current_bid charged the whole spread against the stop's
    # budget before the market had moved at all. Measured on 511 closed
    # positions, that spread was a median 24% of the stop distance and 57% of
    # scored fires would not have fired without it. See
    # risk_manager.evaluate_exit().
    #
    # None means THE ENTRY-SIDE BOOK WAS NOT RECORDED -- every row written
    # before this column existed, plus manual_trigger, which asserts a trade
    # without an EV cycle behind it. Those positions keep the old basis rather
    # than being backfilled: there is no honest way to recover a bid from a
    # book that is hours or days gone.
    entry_bid: Optional[float] = None
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

    # WHY THIS POSITION CANNOT BE SOLD, or None if it can.
    #
    # Set at FILL TIME, and only there. wallet_client._shares_at_worst_fill()
    # already refuses to BUILD an order whose worst-case fill lands under the
    # market's share minimum, which is the right place for a guard that can
    # still say no. It cannot cover this case: the exchange decides what it
    # actually fills, and a partial fill is a real holding whatever its size.
    # WSSS on 2026-08-20 asked for 5.00 shares, got 4.891 against a 5-share
    # minimum, and carried no working stop for the whole life of the position
    # -- discovered on the first stop attempt, which is far too late.
    #
    # The position is STILL RECORDED when this is set. An unrecorded real
    # holding is strictly worse than a flagged one: the flag is a fact about
    # the exit path, not a reason to forget the shares exist.
    exit_blocked_reason: Optional[str] = None

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
    # WHICH BASIS THE STOP MEASURED FROM -- risk_manager.stop_basis_price()'s
    # label ("entry_bid" or "entry_ask_fallback"), set only on the
    # reason="stop_loss" branch of evaluate_exit(). None everywhere else: a
    # hold, a take-profit or a resolution never read stop_basis_price(), so
    # there is no basis to report. NOT part of `reason` -- executor.py
    # derives `status` as f"closed_{reason}" and config.py's
    # COOLDOWN_COUNTED_EXIT_STATUSES matches that string exactly, so
    # appending the label there would silently break cooldown counting and
    # the backtest's per-reason funnel. It is instead folded into the
    # persisted exit_reason TEXT (see executor.close_position), which is
    # free-form.
    stop_basis: Optional[str] = None


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
    # THE OTHER SIDE OF THE SAME BOOK. market_price above is the ASK -- what an
    # entry pays, and the only price any EV number here is computed from. This
    # is the BID for the same side at the same instant, and it feeds NO entry
    # decision. It exists to be carried onto the Position, because the STOP is
    # a movement filter and needs the price this position started at expressed
    # on the basis the position is later marked at. See
    # risk_manager.evaluate_exit(). Already fetched every cycle by
    # fetch_market_quotes() for snapshot capture, so carrying it costs no
    # extra call.
    market_bid: Optional[float] = None
    # THE SECOND TAKER FEE, as a fraction of ENTRY notional, and 0.0 whenever
    # this candidate's book holds to settlement (config.HOLD_TO_SETTLEMENT_MODES)
    # or the caller did not declare a mode. Polymarket charges per LEG, so a
    # book that sells pays twice and a position held to par pays once --
    # redeeming a resolved token is not a trade.
    #
    # Carried on the result rather than recomputed downstream for the same
    # reason fee_rate_pct is: entry_manager re-derives net EV at the resolved
    # size, and a cost it has to recompute is a cost it can get wrong.
    expected_exit_fee_pct: float = 0.0
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
    entry_bid: Optional[float] = None      # the BID for this same side at evaluation time, carried from
                                             # EVResult.market_bid purely so executor can persist it on the
                                             # Position. Not a decision input -- nothing at entry reads it.
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
class SettledToken:
    """
    An outcome token a LIVE position's `closed_resolution` closed but that
    this repo never redeemed -- see storage.load_settled_live_tokens().

    Carries the position's identity (station, target date, bucket, side)
    alongside its size and settlement price, so every message built from it
    can name "WSSS 2026-08-20 32°NO" instead of a token-id prefix that
    identifies nothing to a human. Before this field set existed, the map
    was keyed purely on token_id and threw the identity away even though it
    sat on the same database row.
    """
    station_icao: str
    target_date: date
    bucket_c: int
    side: str
    size_shares: float
    exit_price: float


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
