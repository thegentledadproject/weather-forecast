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
}

# How far back calibration looks, mirroring the live call sites:
# climate_monitor_client.load_recent_observations(station, days=30) for
# seed data, and pipeline.run()'s target_date.replace(day=1) for stored
# observations.
OBSERVATION_WINDOW_DAYS = 30

# Rejection reasons are long human sentences; the funnel needs stable
# short keys. Matched by prefix, longest-lived first, so a reworded tail
# of a reason string cannot silently reclassify a gate.
_REASON_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("VETOED: raw edge", "raw_edge_veto"),
    ("VETOED: same-bucket YES+NO conflict", "same_bucket_conflict"),
    ("Open positions unreadable", "open_positions_unreadable"),
    ("Per-bucket cap", "per_bucket_cap"),
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
    """Stable funnel key for an EntryDecision reason string."""
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
            f"{float(fee_rate_pct):.10g}",
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
        row = self.snapshot(token_id, ts)
        return None if row is None else row["price"]


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


def _pick_observation(candidates: List[ObservedReading]) -> Optional[ObservedReading]:
    """
    One observation from possibly several for the same date. Prefers a
    real stored reading over seed data (a confirmed figure beats a
    manually maintained constant), then lowest source name for a stable
    tie-break. Deterministic by construction -- resolution must never
    depend on row order.
    """
    if not candidates:
        return None
    return sorted(candidates, key=lambda o: (o.source == "seed_data", o.source))[0]


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def run(
    station_icao: str,
    start_date: date,
    end_date: date,
    depth_regime: str = "strict",
    fee_rate_pct: float = 0.0,
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

    run_id = run_id or _make_run_id(
        station_icao, start_date, end_date, depth_regime, fee_rate_pct, bankroll_mode
    )

    params = {
        "station_icao": station_icao,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "depth_regime": depth_regime,
        "fee_rate_pct": float(fee_rate_pct),
        "bankroll_mode": bankroll_mode,
        "trade_size_screen_usd": ev_engine.DEFAULT_TRADE_SIZE_USD,
    }

    portfolio = PortfolioState(bankroll_usd=config.BANKROLL_USD, bankroll_mode=bankroll_mode)
    fill_model = fill_model_mod.FillModel(
        depth_regime=depth_regime,
        fee_rate_pct=float(fee_rate_pct),
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
        "n_exits_trailing_stop": 0,
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
    all_observations = seed_obs + stored_obs

    tokens_seen: set = set()

    # --- clock --------------------------------------------------------------
    first_ts = simclock.local_minute_to_ts(start_date, settings.SIM_DAY_START_HOUR_LOCAL * 60)
    clock = simclock.SimClock(first_ts)
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

        for tick in simclock.generate_ticks(day):
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
                    fee_rate_pct=float(fee_rate_pct),
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
    end_ts = simclock.local_minute_to_ts(end_date + timedelta(days=1), 0)
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
            "The live trading path (scheduler._run_full_cycle) calibrates on seed "
            "observations ONLY; this replay also uses stored observations, matching "
            "pipeline.run(). The replay is therefore better informed than the live trader."
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
            "TRAILING_STOP_ACTIVATION_PCT": config.TRAILING_STOP_ACTIVATION_PCT,
            "TRAILING_STOP_PCT": config.TRAILING_STOP_PCT,
            "EDGE_DECAY_TIGHTEN_HOUR_LOCAL": config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL,
            "TIGHTENED_PROFIT_TAKE_PCT": config.TIGHTENED_PROFIT_TAKE_PCT,
            "TIGHTENED_STOP_LOSS_PCT": config.TIGHTENED_STOP_LOSS_PCT,
            "TIGHTENED_TRAILING_STOP_ACTIVATION_PCT": config.TIGHTENED_TRAILING_STOP_ACTIVATION_PCT,
            "TIGHTENED_TRAILING_STOP_PCT": config.TIGHTENED_TRAILING_STOP_PCT,
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
            "LOCAL_UTC_OFFSET_HOURS": settings.LOCAL_UTC_OFFSET_HOURS,
            "OBS_PUBLISH_LAG_DAYS": settings.OBS_PUBLISH_LAG_DAYS,
            "MAX_STALENESS_FACTOR": settings.MAX_STALENESS_FACTOR,
            "DEFAULT_SNAPSHOT_FIDELITY_MIN": settings.DEFAULT_SNAPSHOT_FIDELITY_MIN,
            "SIM_DAY_START_HOUR_LOCAL": settings.SIM_DAY_START_HOUR_LOCAL,
            "MIN_DEPTH_COVERAGE": settings.MIN_DEPTH_COVERAGE,
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
        # position_manager does -- the trailing stop must see this
        # cycle's peak, not last cycle's.
        new_hwm = risk_manager.update_high_water_mark(position, price)
        position.high_water_mark = new_hwm

        decision = risk_manager.evaluate_exit(position, price, local_hour=local_hour)
        if not decision.should_exit:
            continue

        # Status/exit_reason exactly as executor.close_position() derives
        # them in paper mode (every replay fill is paper by definition).
        portfolio.close_position(
            position_id=position_id,
            exit_price=decision.current_price,
            exit_time_iso=clock.now_iso(),
            status=f"closed_{decision.reason}",
            exit_reason=f"{decision.reason} (paper, pnl={decision.pnl_pct:+.1%})",
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
    fee_rate_pct: float,
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
    forecasts = []
    for source in sorted(forecast_history):
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
    )

    model_probs = {b.bucket_c: b.probability for b in probability.bucket_probabilities(estimate)}

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
            price = None if snapshot is None else snapshot["price"]

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
                    fee_rate_pct=fee_rate_pct,
                    net_ev_per_dollar=None,
                    notes="No live price available this cycle.",
                ))
                continue

            # Screening slippage at the flat default size, mirroring live's
            # market_client.estimate_slippage(token, DEFAULT_TRADE_SIZE_USD).
            slippage = fill_model.slippage(
                ev_engine.DEFAULT_TRADE_SIZE_USD, fill_model.depth_at(snapshot)
            )
            raw_edge = side_model_prob - price
            net_ev = (raw_edge / price) - slippage - fee_rate_pct if price > 0 else None

            rows.append(EVResult(
                station_icao=estimate.station_icao,
                target_date=estimate.target_date,
                bucket_c=bucket_c,
                side=side,
                model_prob=side_model_prob,
                market_price=price,
                raw_edge=raw_edge,
                estimated_slippage_pct=slippage,
                fee_rate_pct=fee_rate_pct,
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

    decisions = entry_sim.decide_portfolio_entries_sim(
        candidates=candidates,
        portfolio=portfolio,
        fill_model=fill_model,
        price_lookup=lambda token_id: prices.snapshot(token_id, tick.ts),
        min_net_ev=tick.min_net_ev,
        sizing_bankroll=portfolio.sizing_bankroll(),
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
        observation = _pick_observation(visible)
        if observation is None:
            counters["n_resolution_pending"] = int(counters["n_resolution_pending"]) + 1
            continue

        winning_bucket = resolution.bucket_for_temp(observation.max_temp_c)
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
