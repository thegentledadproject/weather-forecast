"""
backtest/engine.py

PURPOSE
-------
The replay loop. Walks simulated local days tick by tick on the SAME
schedule the live daemon wakes on (simclock.generate_ticks, derived from
scheduler.determine_window), and at each tick does exactly what
scheduler.run_cycle() would have done for that window's mode -- marks the
book, checks exits, and in trading windows recalibrates, recomputes the
EV table and runs the entry gates.

WHAT MAKES THIS HONEST (OR NOT)
-------------------------------
Every look-ahead guard in this file is load-bearing. In order of how
easily each one could have been got wrong:

  - PRICES come only from price_store.get_price_at(), which returns the
    newest quote at or BEFORE the tick and refuses stale ones. Never a
    nearest-neighbour lookup, never a forward fill across a gap.
  - FORECASTS are filtered on fetched_at <= the simulated UTC instant,
    and only the LATEST such row per source is used -- because live
    fetches one forecast per source per cycle and calibrates on that,
    not on a history.
  - OBSERVATIONS are filtered through resolution.observation_visible(),
    so a day's max temperature cannot inform a decision made before the
    day ended and the figure published (settings.OBS_PUBLISH_LAG_DAYS).
  - RESOLUTION uses that same visibility rule, and pays par-or-nothing
    per resolution.resolution_exit_price(), not the last quote.
  - The CLOCK is a SimClock that raises on any backwards move, and
    risk_manager.evaluate_exit() is given its local_hour explicitly, so
    edge-decay tightening fires at the simulated hour rather than
    whatever hour the backtest process happens to run at.

DETERMINISM
-----------
Nothing in this module reads the wall clock -- no datetime.now(), no
date.today(), no time.time(). run_id is a hash of the run parameters.
Buckets and sides are iterated in a fixed order, open positions in
sorted position_id order. Two runs over the same databases produce
byte-identical BacktestRun objects, manifest git SHA excepted.

TARGET-DATE SEMANTICS
---------------------
The trading target date is D, the same local day being simulated -- not
D+1. Verified against the live path rather than assumed:
scheduler._run_full_cycle() calibrates for date.today() and passes that
estimate to ev_engine.run_for_station(), which discovers the token map
for estimate.target_date; openmeteo_client._fetch_daily_max() only ever
returns a PointForecast for date.today(). The system trades the market
resolving on the day it is running. (Markets do list ~48h ahead -- see
config.ENABLE_MARKET_OPEN_WINDOW's walked-back note -- but the default
schedule never trades them.)

DELIBERATE DIVERGENCES FROM A NAIVE READING OF "REPLAY THE LIVE SYSTEM"
-----------------------------------------------------------------------
1. A day with NO token map is not skipped wholesale. Its entry pass is
   skipped and the day is counted, but its ticks still run so open
   positions from earlier days still get marked, exit-checked and
   resolved. This matches scheduler.py, where _run_exit_check() runs
   after the EV block regardless of whether discovery produced a token
   map. Skipping the whole day would strand every position whose
   resolution day happens to have no market listed.
2. Exit checks run only in the window modes that run them live --
   primary, secondary, monitor_only, risk_only (see scheduler.run_cycle).
   pre_poll ticks mark equity and do nothing else, because live pre_poll
   cycles only look for a published forecast.
3. Calibration observations include BOTH the station's seed observations
   and stored observations, mirroring pipeline.run(). NOTE THE LIVE
   QUIRK: the path that actually trades, scheduler._run_full_cycle(),
   passes ONLY climate_monitor_client's seed observations to calibrate()
   -- it never adds storage's. A replay using both is therefore better
   informed than the live trader is. Recorded in the manifest under
   observation_sources so a result can never quietly claim otherwise.
4. The implausible-move guard SKIPS the position for the tick rather
   than re-fetching for confirmation as position_manager does. There is
   only one price series in a replay, so a confirming re-fetch would
   return the same number and confirm every move -- the opposite of the
   live guard's intent. Skipping is the conservative stand-in. Like
   live, the last-observed baseline is NOT updated on a skip.

DEPENDENCIES
------------
dataclasses, datetime, hashlib, subprocess, typing (standard library)
config.py, models.py, storage.py, calibration.py, probability.py,
ev_engine.py, risk_manager.py (local)
backtest/{settings,simclock,price_store,portfolio,resolution,
          fill_model,entry_sim}.py (local)
"""

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import bucket_axis
import calibration
import config
import ev_engine
import probability
import risk_manager
import storage
from models import CalibratedEstimate, EVResult, ObservedReading, Position

from backtest import entry_sim
from backtest import fill_model as fill_model_mod
from backtest import price_store
from backtest import resolution
from backtest import settings
from backtest import simclock
from backtest.portfolio import PortfolioState

# Window modes that run an exit check in the live system. Read straight
# off scheduler.run_cycle(): "primary"/"secondary" dispatch to
# _run_full_cycle(), which ends with _run_exit_check(); "monitor_only"/
# "risk_only" call _run_exit_check() directly. "pre_poll" does not, and
# "closed" emits no ticks at all.
EXIT_CHECK_MODES = ("primary", "secondary", "monitor_only", "risk_only")

# Window modes that surface new entries. Same source: only these two
# reach the ev_engine/entry_manager block. Tick.min_net_ev is None for
# every other mode, which is a second, independent guard.
ENTRY_MODES = ("primary", "secondary")

# Forecast source strings the live pipeline actually writes, per
# pipeline.gather_forecasts(): two Open-Meteo models for every station,
# plus whichever official adapter the station is registered with.
# Verified against clients/openmeteo_client.py (source_label) and
# clients/official/{nea,met_malaysia}.py (source=).
OPEN_METEO_SOURCES = ("open_meteo_ecmwf", "open_meteo_gfs")
OFFICIAL_SOURCE_BY_CLIENT_KEY = {
    "nea": "nea_24hr",
    "met_malaysia": "wwis_met_malaysia",
    # The 11 new Asia-expansion stations register these two client keys
    # (clients/official/wwis.py, clients/official/hko.py). A station whose
    # key is missing here silently drops its official forecast out of
    # _forecast_sources() -- the replay would run without it and nobody
    # would be told, rather than raising or logging the gap.
    "wwis": "wwis",
    "hko": "hko_fnd",
}

# How far back calibration looks, mirroring the live call sites:
# climate_monitor_client.load_recent_observations(station, days=30) for
# seed data, and pipeline.run()'s target_date.replace(day=1) for stored
# observations.
OBSERVATION_WINDOW_DAYS = 30

# Rejection reasons are long human sentences; the funnel needs stable
# short keys. Matched by prefix, longest-lived first, so a reworded tail
# of a reason string cannot silently reclassify a gate.
# One entry per rejection site in entry_sim.evaluate_entry_sim, plus the
# post-hoc reasons apply_portfolio_budget appends. MUST be kept in step
# with entry_sim's GATE_COUNT: a gate whose reason string is missing here
# lands in "other", and a funnel that says "other=191" cannot tell you
# why a station traded nothing. That is exactly what happened to the four
# gates added between 2026-08-05 and 2026-08-09 (entry-price ceiling,
# edge materiality, stop-out cooldown, position-history unreadable) --
# the 2026-08-12 cohort run reported 100% "other" for nine stations and
# the reason had to be reverse-engineered from source.
_REASON_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("VETOED: raw edge", "raw_edge_veto"),
    ("VETOED: same-bucket YES+NO conflict", "same_bucket_conflict"),
    # Emitted by entry_manager.collection_only_reason via
    # collection_only_decision, not by an entry_sim reason= site -- which
    # is why the AST test below scans both modules.
    ("Collection-only", "collection_only"),
    ("Entry price", "entry_price_ceiling"),
    ("Absolute edge", "edge_immaterial"),
    ("Open positions unreadable", "open_positions_unreadable"),
    ("Per-bucket cap", "per_bucket_cap"),
    ("Opposite side already open", "opposite_side_open"),
    ("Position history unreadable", "position_history_unreadable"),
    ("Stop-out cooldown", "stop_out_cooldown"),
    ("No positive edge", "no_positive_edge"),
    ("Order book depth unavailable", "depth_unavailable"),
    ("Depth-capped size", "depth_capped_too_small"),
    ("Slippage at this size", "slippage_gate"),
    ("Net EV at actual size", "net_ev_at_size"),
    ("Approved", "approved"),
)


@dataclass
class BacktestRun:
    """
    Everything one replay produced, and everything needed to reproduce or
    discredit it. `manifest` is not decoration: a P&L figure without the
    resolved constants and the data-coverage that produced it is not a
    result, it is an anecdote.
    """
    run_id: str
    station_icao: str
    start_date: date
    end_date: date
    params: dict
    closed_positions: List[Position]
    unresolved_positions: List[Position]
    portfolio: PortfolioState
    counters: dict
    provenance: dict
    entry_records: Dict[str, dict]
    decisions_log: List[dict]
    manifest: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------


def _reason_key(reason: str) -> str:
    """
    Stable funnel key for an EntryDecision reason string.

    The budget check is first because apply_portfolio_budget() APPENDS to
    a decision's existing reason rather than replacing it, so a leg that
    was approved on its own merits and then refused for budget still
    starts with "Approved:" -- prefix matching alone files it under
    `approved` and the funnel undercounts budget refusals.
    """
    if "budget exhausted" in reason:
        return "station_day_budget_exhausted"
    for prefix, key in _REASON_PREFIXES:
        if reason.startswith(prefix):
            return key
    return "other"


def _parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO timestamp written by executor/pipeline
    (datetime.now(timezone.utc).isoformat()) into an aware UTC datetime.

    Naive strings are ASSUMED UTC -- every writer in this codebase is
    explicitly UTC. Unparseable values return None, and callers treat that
    as "cannot prove this was in the past", i.e. exclude it. Excluding a
    good row costs a data point; including a bad one is look-ahead.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _forecast_sources(station) -> List[str]:
    """The forecast source strings this station's live pipeline writes, sorted."""
    sources = set(OPEN_METEO_SOURCES)
    official = OFFICIAL_SOURCE_BY_CLIENT_KEY.get(station.official_client_key)
    if official:
        sources.add(official)
    return sorted(sources)


def _make_run_id(
    station_icao: str,
    start_date: date,
    end_date: date,
    depth_regime: str,
    fee_rate_pct: float,
    bankroll_mode: str,
) -> str:
    """
    Deterministic run id: a sha1 prefix over the run parameters. NOT a
    timestamp -- two runs of the same experiment must collide, so that a
    re-run overwrites rather than accumulates near-duplicate results.

    The market-data path is deliberately excluded: it is environment, not
    experiment, and it is recorded in the manifest instead.
    """
    payload = "|".join(
        [
            station_icao,
            start_date.isoformat(),
            end_date.isoformat(),
            depth_regime,
            "auto" if fee_rate_pct is None else f"{float(fee_rate_pct):.10g}",
            bankroll_mode,
        ]
    )
    return "bt_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _git_sha() -> str:
    """Current commit, or "unknown". Never fatal -- provenance, not a gate."""
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


class _PriceReader:
    """
    Memoising wrapper over price_store.get_price_at().

    Two reasons it exists. Correctness: the same (token, tick) is looked
    up several times per tick -- once to mark, once to price an exit, once
    to build the EV row -- and every lookup must return the same snapshot
    dict, not merely an equal one. Cost: get_price_at() opens a fresh
    sqlite connection and runs four CREATE TABLE IF NOT EXISTS statements
    per call, which at ~40 ticks x 22 tokens per day is the dominant cost
    of a run.

    Safe to cache because the market database is read-only for the
    duration of a run.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path
        self._cache: Dict[Tuple[str, int], Optional[dict]] = {}
        self.n_lookups = 0
        self.n_hits = 0
        # How entry prices were sourced across this run. Reported in
        # provenance because a run spanning 2026-08-10 mixes both, and a
        # blended number with no label is indistinguishable from a clean one.
        self.n_entry_priced_ask = 0
        self.n_entry_priced_bid_fallback = 0

    def snapshot(self, token_id: Optional[str], ts: int) -> Optional[dict]:
        if not token_id:
            return None
        key = (token_id, int(ts))
        self.n_lookups += 1
        if key in self._cache:
            self.n_hits += 1
            return self._cache[key]
        row = price_store.get_price_at(token_id, int(ts), db_path=self.db_path)
        self._cache[key] = row
        return row

    def price(self, token_id: Optional[str], ts: int) -> Optional[float]:
        """
        The BID. Correct for marking an open position and for pricing an
        exit -- both are about what a sale would receive. Entries must use
        entry_price() instead.
        """
        row = self.snapshot(token_id, ts)
        return None if row is None else row["price"]

    def entry_price(self, token_id: Optional[str], ts: int) -> Optional[float]:
        """
        The ASK where it was captured, else the bid.

        An entry pays the ask; pricing one off the bid overstates raw edge
        by the spread, which on the live WSSS book runs 0.01-0.03 against a
        MIN_ABS_RAW_EDGE of 0.03. That was fixed on the live path
        2026-08-10 (market_client.get_entry_price_for_side).

        Every snapshot captured BEFORE that date has ask_price = NULL,
        because only one side was ever stored. The fallback is what lets
        those replay at all -- but a replay that falls back is reproducing
        the old overstatement, not correcting it, so the two cases are
        counted separately and reported. Silently blending them would make
        a run's edge figures depend on the calendar in a way nothing in the
        output revealed.
        """
        row = self.snapshot(token_id, ts)
        if row is None:
            return None
        ask = row.get("ask_price")
        if ask is not None:
            self.n_entry_priced_ask += 1
            return ask
        self.n_entry_priced_bid_fallback += 1
        return row["price"]


def _seed_observations(station) -> List[ObservedReading]:
    """
    StationConfig.seed_observations as ObservedReadings, exactly as
    climate_monitor_client.load_recent_observations() builds them
    (source="seed_data") -- but without its date.today() window, which is
    applied per-tick against simulated time instead.
    """
    readings = []
    for date_iso, temp_c in station.seed_observations:
        readings.append(
            ObservedReading(
                station_icao=station.icao,
                target_date=date.fromisoformat(date_iso),
                max_temp_c=temp_c,
                source="seed_data",
            )
        )
    return readings


def _pick_observation(candidates: List[ObservedReading], station) -> Optional[ObservedReading]:
    """
    One observation from possibly several for the same date, by
    config.observation_source_rank(source, station.resolution_grade_source):
    the station's OWN settlement-grade source first (METAR for most
    stations, but "hko_daily_max" for Hong Kong -- see StationConfig),
    then any other fetched reading, seed constants last. Deterministic by
    construction -- resolution must never depend on row order.

    If the best-ranked candidate's source is NOT the station's
    resolution_grade_source, return None even though a candidate exists.
    Callers then take the existing n_resolution_pending path instead of
    settling (or Brier-scoring) a day against a lower-tier reading -- e.g.
    Karachi's proxy-grade METAR (identity unconfirmed, see config.py) or
    Hong Kong's skipped/absent airport METAR. This is what keeps
    Hong Kong/Karachi honest rather than quietly settling on a maybe-wrong
    station's numbers.
    """
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda o: config.observation_source_rank(o.source, station.resolution_grade_source),
    )
    best = ranked[0]
    if best.source != station.resolution_grade_source:
        return None
    return best


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def run(
    station_icao: str,
    start_date: date,
    end_date: date,
    depth_regime: str = "strict",
    fee_rate_pct: Optional[float] = None,
    bankroll_mode: str = "static",
    market_db_path=None,
    run_id: str = None,
) -> BacktestRun:
    """
    Replay [start_date, end_date] inclusive, in local days, for one
    station. See the module docstring for the semantics of each phase and
    for every deliberate divergence from the live path.
    """
    if end_date < start_date:
        raise ValueError(f"end_date {end_date} precedes start_date {start_date}.")
    if depth_regime not in settings.DEPTH_REGIMES:
        raise ValueError(
            f"Unknown depth_regime '{depth_regime}' -- expected one of {settings.DEPTH_REGIMES}."
        )

    station = config.get_station(station_icao)
    station_icao = station.icao

    # C5 RESOLVED 2026-08-10: the simulated clock is per-station.
    #
    # backtest/simclock.py used to hold ONE fixed timezone shared by every
    # simulated instant, so a station whose utc_offset_hours disagreed with
    # it replayed the entire trading thesis at the wrong local hour -- RJTT
    # (+9) an hour off, OPKC (+5) three hours off. That drives window replay
    # (generate_ticks -> scheduler.determine_window), edge-decay tightening
    # (risk_manager.evaluate_exit's local_hour) and observation-visibility
    # boundaries (resolution.observation_visible's local-date comparison).
    # Rather than produce a mistimed result that looks complete, run()
    # refused those stations outright -- which left 6 of the 13 registered
    # stations unbacktestable, including every UTC+9 one.
    #
    # simclock now takes utc_offset_hours on SimClock, local_minute_to_ts()
    # and generate_ticks(), and this function threads the station's own
    # offset through all three. The guard is gone because the limitation is.
    local_offset = station.utc_offset_hours

    run_id = run_id or _make_run_id(
        station_icao, start_date, end_date, depth_regime, fee_rate_pct, bankroll_mode
    )

    params = {
        "station_icao": station_icao,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "depth_regime": depth_regime,
        # None = the live default since ca42555: the price-dependent
        # Polymarket weather taker fee, ev_engine.taker_fee_pct_of_notional.
        # A float is a flat override, exactly as in compute_ev_table.
        "fee_rate_pct": "auto" if fee_rate_pct is None else float(fee_rate_pct),
        "bankroll_mode": bankroll_mode,
        "trade_size_screen_usd": ev_engine.DEFAULT_TRADE_SIZE_USD,
    }

    # Region-scoped, not config.BANKROLL_USD: a European replay must size
    # off Europe's own (zero) Kelly bankroll, not Asia's, or it reports a
    # P&L number the live path structurally cannot produce. See
    # config.region_bankroll_usd() and REGION_BANKROLL_USD.
    portfolio = PortfolioState(
        bankroll_usd=config.region_bankroll_usd(station_icao), bankroll_mode=bankroll_mode
    )
    fill_model = fill_model_mod.FillModel(
        depth_regime=depth_regime,
        fee_rate_pct=fee_rate_pct,
        station_icao=station_icao,
        market_db_path=market_db_path,
    )
    prices = _PriceReader(db_path=market_db_path)

    counters: Dict[str, object] = {
        "n_days": 0,
        "n_no_tokens_days": 0,
        "n_cycles": 0,
        "n_exit_cycles": 0,
        "n_entry_cycles": 0,
        "n_candidates_screened": 0,
        "n_decisions": 0,
        "n_entries": 0,
        "n_entries_missing_token": 0,
        "n_exits_take_profit": 0,
        "n_exits_stop_loss": 0,
        "n_exits_resolution": 0,
        "n_resolution_pending": 0,
        "n_skipped_no_price": 0,
        "n_skipped_implausible_move": 0,
        "n_ev_rows_no_price": 0,
        "n_unresolved": 0,
        "rejections": {},
    }
    rejections: Dict[str, int] = counters["rejections"]  # type: ignore[assignment]

    entry_records: Dict[str, dict] = {}
    decisions_log: List[dict] = []

    # --- data loaded once for the whole run --------------------------------
    # Static for the run's duration, so loading per tick would only add
    # cost and non-determinism risk. Every time-based filter is applied
    # per tick against the SimClock, never here.
    forecast_sources = _forecast_sources(station)
    forecast_history = {}
    for source in forecast_sources:
        rows = storage.load_forecast_history(station_icao, source, limit=100000)
        # Sort ascending by parsed fetched_at, dropping unparseable rows --
        # a forecast we cannot place in time cannot be proven to be in the
        # past, so it must not be usable.
        dated = [(ts, f) for ts, f in ((_parse_iso_utc(f.fetched_at), f) for f in rows) if ts is not None]
        dated.sort(key=lambda pair: (pair[0], pair[1].target_date.isoformat()))
        forecast_history[source] = dated

    seed_obs = _seed_observations(station)
    stored_obs = storage.load_observations_since(
        station_icao, start_date - timedelta(days=OBSERVATION_WINDOW_DAYS + 400)
    )
    stored_obs.sort(key=lambda o: (o.target_date, o.source))
    # Best source per day (METAR settlement-grade > other fetched > seed),
    # exactly as pipeline.run() now feeds calibration live -- and it makes
    # resolution deterministic in what it settles against, closing the
    # resolution-source mismatch the framework originally flagged.
    all_observations = storage.dedupe_observations(seed_obs + stored_obs)

    tokens_seen: set = set()

    # --- clock --------------------------------------------------------------
    first_ts = simclock.local_minute_to_ts(
        start_date, settings.SIM_DAY_START_HOUR_LOCAL * 60, local_offset,
    )
    clock = simclock.SimClock(first_ts, utc_offset_hours=local_offset)
    last_ts = first_ts

    # Last price actually OBSERVED per position, mirroring
    # position_manager._last_observed_price: seeded from entry_price and
    # NOT updated on a skipped cycle.
    last_observed: Dict[str, float] = {}

    # --- day loop -----------------------------------------------------------
    day = start_date
    while day <= end_date:
        counters["n_days"] = int(counters["n_days"]) + 1

        token_map = price_store.load_token_map(station_icao, day, db_path=market_db_path)
        if not token_map:
            counters["n_no_tokens_days"] = int(counters["n_no_tokens_days"]) + 1
            token_map = {}
        for ids in token_map.values():
            for key in ("yes_token_id", "no_token_id"):
                if ids.get(key):
                    tokens_seen.add(ids[key])

        for tick in simclock.generate_ticks(day, local_offset):
            clock.advance_to(tick.ts)
            last_ts = tick.ts
            counters["n_cycles"] = int(counters["n_cycles"]) + 1

            # (a) mark the book at this instant
            portfolio.mark(tick.ts, lambda pos: prices.price(pos.token_id, tick.ts))

            runs_exit_check = tick.mode in EXIT_CHECK_MODES
            if runs_exit_check:
                counters["n_exit_cycles"] = int(counters["n_exit_cycles"]) + 1
                _exit_pass(
                    clock=clock,
                    tick=tick,
                    portfolio=portfolio,
                    prices=prices,
                    last_observed=last_observed,
                    counters=counters,
                )

            # (c) entry pass -- trading windows only, and only where the
            # window actually carries an EV bar.
            if tick.mode in ENTRY_MODES and tick.min_net_ev is not None and token_map:
                counters["n_entry_cycles"] = int(counters["n_entry_cycles"]) + 1
                _entry_pass(
                    station=station,
                    day=day,
                    clock=clock,
                    tick=tick,
                    token_map=token_map,
                    portfolio=portfolio,
                    fill_model=fill_model,
                    prices=prices,
                    forecast_history=forecast_history,
                    all_observations=all_observations,
                    fee_rate_pct=fee_rate_pct,
                    counters=counters,
                    rejections=rejections,
                    entry_records=entry_records,
                    decisions_log=decisions_log,
                    last_observed=last_observed,
                )

            # (d) resolution sweep -- past-dated positions settle against a
            # VISIBLE observation. Gated to exit-check modes for the same
            # reason the exit pass is: live only touches positions in
            # cycles that call position_manager.
            if runs_exit_check:
                _resolution_sweep(
                    station=station,
                    clock=clock,
                    portfolio=portfolio,
                    all_observations=all_observations,
                    counters=counters,
                    last_observed=last_observed,
                )

        day += timedelta(days=1)

    # --- final mark and unresolved handling ---------------------------------
    portfolio.mark(last_ts, lambda pos: prices.price(pos.token_id, last_ts))

    unresolved = [portfolio.open[pid] for pid in sorted(portfolio.open)]
    counters["n_unresolved"] = len(unresolved)

    # --- provenance and manifest --------------------------------------------
    end_ts = simclock.local_minute_to_ts(end_date + timedelta(days=1), 0, local_offset)
    coverage = price_store.coverage_stats(
        sorted(tokens_seen), first_ts, end_ts, db_path=market_db_path
    )

    provenance = {
        "n_tokens": len(tokens_seen),
        "start_ts": first_ts,
        "end_ts": end_ts,
        "coverage": coverage,
        "depth_coverage_ok": (
            coverage["pct_with_depth"] >= settings.MIN_DEPTH_COVERAGE
            if coverage["n_ticks"]
            else False
        ),
        "min_depth_coverage_required": settings.MIN_DEPTH_COVERAGE,
        "price_lookups": prices.n_lookups,
        "price_lookup_cache_hits": prices.n_hits,
        # How this run priced its entries. An entry pays the ask; snapshots
        # captured before 2026-08-10 only ever stored the bid, so a replay
        # over older data falls back and REPRODUCES the pre-fix
        # edge overstatement rather than correcting it. A run whose
        # entry_pricing is not "ask" is not comparable to one that is.
        "n_entry_priced_ask": prices.n_entry_priced_ask,
        "n_entry_priced_bid_fallback": prices.n_entry_priced_bid_fallback,
        "entry_pricing": (
            "ask" if prices.n_entry_priced_bid_fallback == 0 and prices.n_entry_priced_ask
            else "bid_fallback" if prices.n_entry_priced_ask == 0
            else "MIXED -- spans the 2026-08-10 ask-capture change; edges are not comparable across it"
        ),
    }

    manifest = {
        "run_id": run_id,
        "git_sha": _git_sha(),
        "params": dict(params),
        "market_db_path": str(market_db_path) if market_db_path else str(settings.MARKET_DATA_DB),
        "trading_db_path": str(config.DB_PATH),
        "target_date_semantics": "trades the market resolving on the simulated day D (same-day), per scheduler._run_full_cycle",
        "exit_check_modes": list(EXIT_CHECK_MODES),
        "entry_modes": list(ENTRY_MODES),
        "forecast_sources": forecast_sources,
        "observation_sources": [
            "StationConfig.seed_observations",
            "storage.observations",
        ],
        "observation_source_note": (
            "Live and replay calibrate on the same inputs since 2026-08-02: "
            "scheduler._run_full_cycle uses pipeline.gather_observations() "
            "(seeds + stored observations, deduped by source rank), matching "
            "this replay's assembly. Before that fix the live path used seed "
            "observations only and replays of earlier dates were better "
            "informed than the live trader that traded them."
        ),
        "ensemble_members": None,
        "ensemble_note": (
            "No historical ensemble spread exists, so calibration.estimate_std_dev falls "
            "back to forecast spread, then observed spread, then its 1.2C default. Live "
            "passes openmeteo_client.get_ensemble_spread(); spreads will differ."
        ),
        "fill_model": fill_model.describe(),
        "config_constants": {
            "BANKROLL_USD": config.BANKROLL_USD,
            "KELLY_FRACTION": config.KELLY_FRACTION,
            "MAX_POSITION_USD": config.MAX_POSITION_USD,
            "MAX_DEPTH_UTILIZATION_PCT": config.MAX_DEPTH_UTILIZATION_PCT,
            "MAX_ACCEPTABLE_SLIPPAGE_PCT": config.MAX_ACCEPTABLE_SLIPPAGE_PCT,
            "MAX_PLAUSIBLE_RAW_EDGE": config.MAX_PLAUSIBLE_RAW_EDGE,
            "MAX_OPEN_POSITIONS_PER_BUCKET": config.MAX_OPEN_POSITIONS_PER_BUCKET,
            "MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD": config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD,
            "STATION_MATURITY": dict(config.STATION_MATURITY),
            "EXPLORATORY_SIZE_MULTIPLIER": config.EXPLORATORY_SIZE_MULTIPLIER,
            "PROFIT_TAKE_PCT": config.PROFIT_TAKE_PCT,
            "STOP_LOSS_PCT": config.STOP_LOSS_PCT,
            "EDGE_DECAY_TIGHTEN_HOUR_LOCAL": config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL,
            "TIGHTENED_PROFIT_TAKE_PCT": config.TIGHTENED_PROFIT_TAKE_PCT,
            "TIGHTENED_STOP_LOSS_PCT": config.TIGHTENED_STOP_LOSS_PCT,
            # The lottery carve-out's two halves. Recorded because
            # backtest/take_sweep.py VARIES the take across runs, and a
            # manifest that omitted the constant being swept would label
            # every row of that sweep identically.
            "LOTTERY_PRICE_THRESHOLD": config.LOTTERY_PRICE_THRESHOLD,
            "LOTTERY_PROFIT_TAKE_PCT": config.LOTTERY_PROFIT_TAKE_PCT,
            "TIGHTENED_LOTTERY_PROFIT_TAKE_PCT": config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT,
            # The stop carve-out's upper half, for the same reason: it
            # decides WHICH positions STOP_LOSS_PCT reaches, so a sweep of
            # that constant means something different on either side of it.
            "STOP_EXEMPT_ABOVE_PRICE": config.STOP_EXEMPT_ABOVE_PRICE,
            "MAX_SINGLE_CYCLE_MOVE": config.MAX_SINGLE_CYCLE_MOVE,
            "MIN_EXIT_PRICE": config.MIN_EXIT_PRICE,
            "BUCKET_MIN_C": config.BUCKET_MIN_C,
            "BUCKET_MAX_C": config.BUCKET_MAX_C,
            "ENABLE_MARKET_OPEN_WINDOW": config.ENABLE_MARKET_OPEN_WINDOW,
            "SCHEDULE_WINDOWS": [list(w) for w in config.SCHEDULE_WINDOWS],
            "ev_engine.DEFAULT_TRADE_SIZE_USD": ev_engine.DEFAULT_TRADE_SIZE_USD,
            "ev_engine.DEFAULT_FEE_RATE_PCT": ev_engine.DEFAULT_FEE_RATE_PCT,
        },
        "backtest_settings": {
            # The DEFAULT offset. The clock this run actually used is
            # sim_utc_offset_hours below -- they differ for any station
            # outside UTC+8, which is the whole point of the C5 fix.
            "LOCAL_UTC_OFFSET_HOURS": settings.LOCAL_UTC_OFFSET_HOURS,
            "sim_utc_offset_hours": local_offset,
            "OBS_PUBLISH_LAG_DAYS": settings.OBS_PUBLISH_LAG_DAYS,
            "MAX_STALENESS_FACTOR": settings.MAX_STALENESS_FACTOR,
            "DEFAULT_SNAPSHOT_FIDELITY_MIN": settings.DEFAULT_SNAPSHOT_FIDELITY_MIN,
            "SIM_DAY_START_HOUR_LOCAL": settings.SIM_DAY_START_HOUR_LOCAL,
            "MIN_DEPTH_COVERAGE": settings.MIN_DEPTH_COVERAGE,
        },
        # The block above is a single global that every run has always
        # recorded, regardless of which station it replayed -- fine when
        # only WSSS/WMKK (both UTC+8) existed, but misleading now that the
        # registry spans UTC+5..+9: a reader skimming
        # backtest_settings.LOCAL_UTC_OFFSET_HOURS=8 could wrongly conclude
        # THIS run traded at UTC+8. The run() guard above (C5) already
        # refuses any station whose utc_offset_hours disagrees with that
        # global, so today the two can never actually diverge -- but this
        # section records the station's own values explicitly anyway, so
        # the manifest stays honest on its own once simclock is
        # parameterized per-station and the guard is lifted.
        "station_config": {
            "icao": station.icao,
            "utc_offset_hours": station.utc_offset_hours,
            "bucket_min_c": station.bucket_min_c,
            "bucket_max_c": station.bucket_max_c,
            "bucket_edge_mode": station.bucket_edge_mode,
            "resolution_grade_source": station.resolution_grade_source,
        },
        "gate_count": entry_sim.GATE_COUNT,
        "coverage": coverage,
    }

    return BacktestRun(
        run_id=run_id,
        station_icao=station_icao,
        start_date=start_date,
        end_date=end_date,
        params=params,
        closed_positions=list(portfolio.closed),
        unresolved_positions=unresolved,
        portfolio=portfolio,
        counters=counters,
        provenance=provenance,
        entry_records=entry_records,
        decisions_log=decisions_log,
        manifest=manifest,
    )


# --------------------------------------------------------------------------
# per-tick phases
# --------------------------------------------------------------------------


def _exit_pass(clock, tick, portfolio, prices, last_observed, counters) -> None:
    """
    One exit-check cycle, mirroring position_manager.check_and_exit_positions().

    Past-dated positions are left for the resolution sweep -- in a replay,
    settlement is decided by a VISIBLE observation, not by a price, so
    routing them through the price path first would only manufacture
    spurious no-price skips.

    Iterates position ids in sorted order: dict order would be insertion
    order, which is stable in CPython but is not a contract, and the exit
    loop can close positions and so must not depend on it.
    """
    local_date = clock.local_date()
    local_hour = clock.local_hour()

    for position_id in sorted(portfolio.open):
        position = portfolio.open.get(position_id)
        if position is None:
            continue
        if position.target_date < local_date:
            continue  # settled by _resolution_sweep

        price = prices.price(position.token_id, tick.ts)
        if price is None:
            counters["n_skipped_no_price"] = int(counters["n_skipped_no_price"]) + 1
            continue

        baseline = last_observed.get(position_id, position.entry_price)
        if abs(price - baseline) > config.MAX_SINGLE_CYCLE_MOVE:
            # Conservative stand-in for position_manager's confirming
            # re-fetch. Baseline deliberately NOT updated: live leaves it
            # stale too when confirmation fails.
            counters["n_skipped_implausible_move"] = int(counters["n_skipped_implausible_move"]) + 1
            continue

        last_observed[position_id] = price

        # High-water mark refreshed BEFORE evaluating, exactly as
        # position_manager does. No exit reads it since the trailing stop
        # was removed (2026-08-17); it is kept in step so the recorded
        # peak stays truthful and so live and replay do not diverge.
        new_hwm = risk_manager.update_high_water_mark(position, price)
        position.high_water_mark = new_hwm

        decision = risk_manager.evaluate_exit(position, price, local_hour=local_hour)
        if not decision.should_exit:
            continue

        # Status/exit_reason/exit_price exactly as executor.close_position()
        # derives them in paper mode (every replay fill is paper by
        # definition) -- INCLUDING the exit-side taker fee, which live
        # deducts from the recorded price. A replay that booked exits
        # gross while live booked them net would overstate every
        # simulated exit by the fee, which is a large fraction of a
        # typical exit's gain.
        #
        # "IN PAPER MODE" IS NOW THE LOAD-BEARING HALF OF THAT SENTENCE
        # (2026-08-19). The rungs no longer agree on which price is the
        # truth: live records result.fill_price and simulation records
        # spec.expected_price, because both place an order and a FOK limit
        # is a worst-price bound rather than a target. Paper places none, so
        # the quote is all it has -- and a replay has no order path at all,
        # which is exactly why paper is the rung it must mirror. Matching
        # live here instead would mean inventing a fill this data cannot
        # support. Same shape as the ENTRY PRICE PARITY note in
        # backtest/fill_model.py, which resolves the identical question the
        # identical way on the other leg.
        gross_exit_price = decision.current_price
        exit_fee_per_share = risk_manager.taker_fee_per_share(gross_exit_price)
        net_exit_price = max(gross_exit_price - exit_fee_per_share, 0.0)
        net_pnl_pct = risk_manager.compute_pnl_pct(position.entry_price, net_exit_price)
        portfolio.close_position(
            position_id=position_id,
            exit_price=net_exit_price,
            exit_time_iso=clock.now_iso(),
            status=f"closed_{decision.reason}",
            exit_reason=(
                f"{decision.reason} (paper, pnl={net_pnl_pct:+.1%} net; "
                f"gross {gross_exit_price:.4f} - exit fee {exit_fee_per_share:.4f}/share "
                f"= net {net_exit_price:.4f})"
            ),
        )
        last_observed.pop(position_id, None)

        key = f"n_exits_{decision.reason}"
        if key in counters:
            counters[key] = int(counters[key]) + 1


def _visible_observations(all_observations, clock, cutoff: date) -> List[ObservedReading]:
    """
    Observations this simulated instant is allowed to know: on or after
    `cutoff`, and past resolution.observation_visible()'s publish lag.
    """
    local_dt = clock.local_datetime()
    return [
        o for o in all_observations
        if o.target_date >= cutoff and resolution.observation_visible(o.target_date, local_dt)
    ]


def _entry_pass(
    station,
    day: date,
    clock,
    tick,
    token_map,
    portfolio,
    fill_model,
    prices,
    forecast_history,
    all_observations,
    fee_rate_pct: Optional[float],
    counters,
    rejections,
    entry_records,
    decisions_log,
    last_observed,
) -> None:
    """
    One trading cycle: calibrate on what was knowable, rebuild the EV
    table off quotes at or before this instant, screen, gate, fill.
    """
    sim_utc = clock.utc_datetime()

    # --- forecasts: latest per source, fetched at or before now ----------
    # Excluded sources are skipped HERE rather than filtered out of
    # forecast_history at load time, so the replay's source set matches what
    # pipeline.gather_forecasts() hands the live blend: stored either way,
    # blended only where config says this station blends it. Dropping them at
    # load would also hide them from anything else reading that history.
    forecasts = []
    for source in sorted(forecast_history):
        if not config.forecast_source_is_blended(station.icao, source):
            continue
        latest = None
        for fetched_ts, forecast in forecast_history[source]:
            if fetched_ts > sim_utc:
                break  # list is ascending by fetched_at
            if forecast.target_date == day:
                latest = forecast
        if latest is not None:
            forecasts.append(latest)

    # --- observations: visible only ---------------------------------------
    observations = _visible_observations(
        all_observations, clock, day - timedelta(days=OBSERVATION_WINDOW_DAYS)
    )

    estimate: CalibratedEstimate = calibration.calibrate(
        station=station,
        target_date=day,
        forecasts=forecasts,
        observations=observations,
        ensemble_members=None,  # no historical ensemble spread exists -- see manifest
        # forecast_bias_c deliberately left at its 0.0 default: correcting
        # by a bias measured over the whole record would leak the future
        # into every replayed day. Doing this honestly means reconstructing
        # the bias as of each simulated instant (only forecasts fetched on
        # or before each past target date, only observations visible then),
        # which is its own piece of work. Until that exists, replays model
        # the UNCORRECTED calibration -- and the same reasoning is why
        # enforce_bias_quality stays off for the gate below.
        #
        # allow_measured_spread=False for the IDENTICAL reason, one level
        # down: calibration.estimate_std_dev's measured and pooled tiers
        # both read the whole stored error record, so either would price a
        # replayed Aug-3 tick using errors from Aug-10. Replays get the
        # measured pooled constant instead, reported as "replay_constant".
        allow_measured_spread=False,
    )

    # Station's own bucket bounds + edge mode (B4): the legacy
    # config.BUCKET_MIN_C/MAX_C globals are Singapore/KL-era defaults and
    # would compute probability mass over the wrong bucket range/intervals
    # for every other station (and, per B1, over WSSS/WMKK's own stale
    # cross-check bounds too now that the live event has drifted to 27-37).
    model_probs = {
        b.bucket_c: b.probability
        for b in probability.bucket_probabilities(
            estimate,
            bucket_min=station.bucket_min_c,
            bucket_max=station.bucket_max_c,
            axis=bucket_axis.for_station(station),
        )
    }

    # --- EV table: ev_engine.compute_ev_table's arithmetic, verbatim -----
    # Buckets in sorted order and YES before NO, matching live's
    # per-bucket [YES, NO] iteration -- best_opportunities() sorts stably,
    # so this ordering is what breaks net-EV ties.
    rows: List[EVResult] = []
    for bucket_c in sorted(token_map):
        ids = token_map[bucket_c]
        model_prob = model_probs.get(bucket_c, 0.0)

        for side, token_key in (("YES", "yes_token_id"), ("NO", "no_token_id")):
            token_id = ids.get(token_key)
            if not token_id:
                counters["n_entries_missing_token"] = int(counters["n_entries_missing_token"]) + 1
                continue

            side_model_prob = model_prob if side == "YES" else (1 - model_prob)
            snapshot = prices.snapshot(token_id, tick.ts)
            # The ASK -- this row becomes raw_edge and the entry price. See
            # _PriceReader.entry_price() for the bid fallback on snapshots
            # captured before both sides were stored.
            price = prices.entry_price(token_id, tick.ts)

            if price is None:
                counters["n_ev_rows_no_price"] = int(counters["n_ev_rows_no_price"]) + 1
                rows.append(EVResult(
                    station_icao=estimate.station_icao,
                    target_date=estimate.target_date,
                    bucket_c=bucket_c,
                    side=side,
                    model_prob=side_model_prob,
                    market_price=None,
                    raw_edge=None,
                    estimated_slippage_pct=0.0,
                    fee_rate_pct=fee_rate_pct if fee_rate_pct is not None
                    else ev_engine.taker_fee_pct_of_notional(None),
                    net_ev_per_dollar=None,
                    notes="No live price available this cycle.",
                ))
                continue

            # Screening slippage at the flat default size, mirroring live's
            # market_client.estimate_slippage(token, DEFAULT_TRADE_SIZE_USD).
            slippage = fill_model.slippage(
                ev_engine.DEFAULT_TRADE_SIZE_USD, fill_model.depth_at(snapshot)
            )
            # Fee exactly as compute_ev_table since ca42555: flat override
            # if given, else the price-dependent weather taker fee.
            fee_pct = (
                fee_rate_pct
                if fee_rate_pct is not None
                else ev_engine.taker_fee_pct_of_notional(price)
            )
            raw_edge = side_model_prob - price
            net_ev = (raw_edge / price) - slippage - fee_pct if price > 0 else None

            rows.append(EVResult(
                station_icao=estimate.station_icao,
                target_date=estimate.target_date,
                bucket_c=bucket_c,
                side=side,
                model_prob=side_model_prob,
                market_price=price,
                raw_edge=raw_edge,
                estimated_slippage_pct=slippage,
                fee_rate_pct=fee_pct,
                net_ev_per_dollar=net_ev,
            ))

    screened = ev_engine.best_opportunities(rows, min_net_ev=tick.min_net_ev)
    counters["n_candidates_screened"] = int(counters["n_candidates_screened"]) + len(screened)

    # --- candidates: token lookup exactly as entry_manager.decide_entries -
    candidates: List[Tuple[EVResult, str]] = []
    for result in screened:
        bucket_ids = token_map.get(result.bucket_c)
        if not bucket_ids:
            continue
        token_id = bucket_ids.get("yes_token_id") if result.side == "YES" else bucket_ids.get("no_token_id")
        if not token_id:
            continue
        candidates.append((result, token_id))

    # Dollars already deployed into this station/day by earlier cycles --
    # open positions plus same-day closed ones (a stopped-out leg still
    # spent budget). Mirrors entry_manager.station_day_exposure_usd().
    existing_exposure_usd = portfolio.total_open_exposure(station.icao, day) + sum(
        p.size_usd
        for p in portfolio.closed
        if p.station_icao == station.icao and p.target_date == day
    )

    # Portfolio-wide (ALL stations) same-day exposure, mirroring
    # entry_manager.portfolio_day_exposure_usd() -- what makes
    # config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD actually bind in a
    # replay instead of only the per-station cap. station_icao=None so
    # total_open_exposure sums every station's open positions, not just
    # this one. A single-station run() only ever holds this one station's
    # positions in `portfolio` anyway (see the C5 guard above), so this
    # collapses to existing_exposure_usd in practice -- but it's computed
    # honestly rather than assumed, so a future multi-station replay
    # doesn't silently under-count the cap.
    portfolio_exposure_usd = portfolio.total_open_exposure(None, day) + sum(
        p.size_usd for p in portfolio.closed if p.target_date == day
    )

    # Point-in-time count of this station's OWN settlement-grade
    # observations (D1/D2's resolution_grade_source), for the
    # collection-first gate (F2). Reuses `observations` -- the same
    # _visible_observations() list _entry_pass already built above for
    # calibration -- rather than a fresh, wider query: that list is
    # already scoped to what THIS simulated instant could legitimately
    # know (no lookahead), which is exactly the count the live gate reads
    # from storage as of "now". Counting anything broader would let a
    # replay see observations its own sim clock hasn't reached yet.
    resolution_obs_count = sum(
        1 for o in observations if o.source == station.resolution_grade_source
    )

    decisions = entry_sim.decide_portfolio_entries_sim(
        candidates=candidates,
        portfolio=portfolio,
        fill_model=fill_model,
        price_lookup=lambda token_id: prices.snapshot(token_id, tick.ts),
        min_net_ev=tick.min_net_ev,
        sizing_bankroll=portfolio.sizing_bankroll(),
        existing_exposure_usd=existing_exposure_usd,
        portfolio_exposure_usd=portfolio_exposure_usd,
        # Region-scoped, mirroring entry_manager.decide_portfolio_entries():
        # without this, apply_portfolio_budget() falls back to its own
        # global default (Asia's MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD),
        # which is wrong for any station outside Asia.
        max_portfolio_usd=config.region_max_daily_exposure_usd(station.icao),
        resolution_obs_count=resolution_obs_count,
        enforce_collection_gate=True,
        # Off until point-in-time bias reconstruction exists (see the
        # calibrate() call above). Replays therefore model the OLD
        # counting-only gate, not the live bias-quality one -- a known and
        # deliberate live/replay divergence, recorded here rather than
        # papered over with a whole-record bias that would be lookahead.
        enforce_bias_quality=False,
    )
    counters["n_decisions"] = int(counters["n_decisions"]) + len(decisions)

    cycle_rejections: Dict[str, int] = {}
    for decision in decisions:
        key = _reason_key(decision.reason)
        if not decision.approved:
            rejections[key] = rejections.get(key, 0) + 1
            cycle_rejections[key] = cycle_rejections.get(key, 0) + 1

    # --- fills -------------------------------------------------------------
    # Sorted so the order positions are booked in cannot depend on how
    # veto_same_bucket_conflicts happened to regroup the list.
    approved = sorted(
        (d for d in decisions if d.approved),
        key=lambda d: (d.bucket_c, d.side),
    )

    opened_ids = []
    for decision in approved:
        entry_time = clock.now_iso()
        position_id = (
            f"{decision.station_icao}:{decision.target_date}:{decision.bucket_c}:"
            f"{decision.side}:{entry_time}"
        )
        if position_id in portfolio.open:
            # Cannot happen given the per-bucket cap, but a silent overwrite
            # here would be an invisible doubling of exposure.
            continue

        position = Position(
            position_id=position_id,
            station_icao=decision.station_icao,
            target_date=decision.target_date,
            bucket_c=decision.bucket_c,
            side=decision.side,
            # LIVE PARITY: the quote, not the slipped fill. See
            # backtest/fill_model.py's docstring -- executor.open_position
            # writes decision.entry_price, which entry_manager sets to
            # ev_result.market_price. Slippage is carried below instead.
            entry_price=decision.entry_price,
            size_usd=decision.recommended_size_usd,
            entry_time=entry_time,
            status="open",
            high_water_mark=decision.entry_price,
            token_id=decision.token_id,
            is_paper=True,
            # LIVE PARITY, same as entry_price above: executor.open_position
            # copies these three straight off the decision, so the replay
            # must too rather than re-deriving them from `rows`. The
            # entry_records entry below stays the authoritative copy for
            # scoring -- this one exists so a replayed Position carries the
            # same fields a live one now does.
            model_prob=decision.model_prob,
            raw_edge=decision.raw_edge,
            net_ev_at_size=decision.net_ev_at_size,
        )
        portfolio.open_position(position)
        last_observed[position_id] = decision.entry_price
        counters["n_entries"] = int(counters["n_entries"]) + 1
        opened_ids.append(position_id)

        depth_usd = decision.available_depth_usd
        entry_records[position_id] = {
            "model_prob": _model_prob_for(rows, decision.bucket_c, decision.side),
            "market_price": decision.entry_price,
            "raw_edge": _raw_edge_for(rows, decision.bucket_c, decision.side),
            "net_ev": decision.net_ev_at_size,
            "slippage_pct": decision.slippage_at_size_pct,
            "bucket_c": decision.bucket_c,
            "side": decision.side,
            "entry_ts": tick.ts,
            "entry_time": entry_time,
            "size_usd": decision.recommended_size_usd,
            "token_id": decision.token_id,
            "available_depth_usd": depth_usd,
            "kelly_fraction_raw": decision.kelly_fraction_raw,
            "kelly_fraction_applied": decision.kelly_fraction_applied,
            "station_maturity": decision.station_maturity,
            "tick_mode": tick.mode,
            "tick_min_net_ev": tick.min_net_ev,
            # Costs live paper mode does NOT record on the position.
            "slipped_fill_price": fill_model.entry_fill_price(
                decision.entry_price, decision.recommended_size_usd, depth_usd
            ),
            "slippage_cost_usd": fill_model.slippage_cost_usd(
                decision.entry_price, decision.recommended_size_usd, depth_usd
            ),
        }

    decisions_log.append({
        "ts": tick.ts,
        "local_date": day.isoformat(),
        "local_time": clock.local_datetime().isoformat(),
        "mode": tick.mode,
        "min_net_ev": tick.min_net_ev,
        "central_estimate_c": estimate.central_estimate_c,
        "std_dev_c": estimate.std_dev_c,
        "n_forecasts": len(forecasts),
        "forecast_sources": [f.source for f in forecasts],
        "n_observations": len(observations),
        "n_ev_rows": len(rows),
        "n_screened": len(screened),
        "n_candidates": len(candidates),
        "n_decisions": len(decisions),
        "n_approved": len(approved),
        "n_opened": len(opened_ids),
        "rejections": cycle_rejections,
        "opened_position_ids": opened_ids,
    })


def _model_prob_for(rows: List[EVResult], bucket_c: int, side: str) -> Optional[float]:
    for r in rows:
        if r.bucket_c == bucket_c and r.side == side:
            return r.model_prob
    return None


def _raw_edge_for(rows: List[EVResult], bucket_c: int, side: str) -> Optional[float]:
    for r in rows:
        if r.bucket_c == bucket_c and r.side == side:
            return r.raw_edge
    return None


def _resolution_sweep(station, clock, portfolio, all_observations, counters, last_observed) -> None:
    """
    Settle every open position whose target date has passed AND whose
    observation is visible at this simulated instant.

    Par-or-nothing via resolution.resolution_exit_price(), booked with
    status "closed_resolution" and exit_reason "market_resolved" --
    exactly what position_manager._close_as_resolved() passes to
    executor.close_position(), so a resolution can never be filed as a
    stop-loss.

    A past-dated position with no visible observation is LEFT OPEN and
    counted. Guessing a winner would put a fabricated number straight into
    the P&L record, which is the one thing worse than an unscored trade.
    """
    local_date = clock.local_date()
    local_dt = clock.local_datetime()

    for position_id in sorted(portfolio.open):
        position = portfolio.open.get(position_id)
        if position is None or position.target_date >= local_date:
            continue

        visible = [
            o for o in all_observations
            if o.target_date == position.target_date
            and resolution.observation_visible(o.target_date, local_dt)
        ]
        # station.resolution_grade_source-aware pick (D1/D2): if the best
        # candidate isn't the station's own settlement-grade source, this
        # returns None and the day falls through to n_resolution_pending
        # instead of settling on a lower-tier reading -- see
        # _pick_observation's docstring.
        observation = _pick_observation(visible, station)
        if observation is None:
            counters["n_resolution_pending"] = int(counters["n_resolution_pending"]) + 1
            continue

        winning_bucket = resolution.bucket_for_temp(
            observation.max_temp_c,
            station.bucket_min_c,
            station.bucket_max_c,
            axis=bucket_axis.for_station(station),
        )
        exit_price = resolution.resolution_exit_price(
            position.side, position.bucket_c, winning_bucket
        )
        portfolio.close_position(
            position_id=position_id,
            exit_price=exit_price,
            exit_time_iso=clock.now_iso(),
            status="closed_resolution",
            exit_reason="market_resolved",
        )
        last_observed.pop(position_id, None)
        counters["n_exits_resolution"] = int(counters["n_exits_resolution"]) + 1
