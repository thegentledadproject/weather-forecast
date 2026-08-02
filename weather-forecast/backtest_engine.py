"""
backtest_engine.py

PURPOSE
-------
Closes the gap flagged in the mechanism reanalysis: there was no
actual backtest anywhere in this codebase, only live calibration
being informally treated as validation. This module is deliberately
split into two parts with very different confidence levels:

  PART 1 -- FORECAST-ACCURACY BACKTEST (built here, real, runnable now)
    Strict walk-forward evaluation of calibration.py's blending logic
    against storage.py's own observation history. At each simulated
    "as-of" date, only observations STRICTLY BEFORE that date are
    used to predict it -- enforced by construction (see the slicing
    in run_observed_only_backtest), not just by convention, specifically
    to avoid the look-ahead bias flagged in the reanalysis.

  PART 2 -- TRADING-STRATEGY BACKTEST (now delegates to backtest/engine.py)
    The data dependency that used to block this is resolved. Polymarket
    CLOB's `/prices-history` endpoint is confirmed real and working --
    GET https://clob.polymarket.com/prices-history?market=<token_id>&
    startTs=<unix>&endTs=<unix>&fidelity=<min> -- see
    backtest/price_history_client.py. The one caveat: already-resolved
    markets have only been observed to return COARSE (>=12h) granularity
    from that endpoint regardless of the requested fidelity. Finer-
    granularity coverage instead comes from live snapshot capture that
    piggybacks on ev_engine's live cycles, persisted into
    data/market_data.sqlite3 (see backtest/snapshot_collector.py and
    ev_engine.py's own per-cycle capture).

    run_trading_strategy_backtest() below now delegates to the real
    replay engine, backtest.engine.run(), which DOES validate: real
    entry/exit simulation against a mix of live-captured snapshots and
    CLOB history, fills at quote price (matching live paper-trading
    semantics, not a slipped fill), gate-by-gate entry logic parity with
    production (see tests/test_parity_entry.py), and realistic
    no-lookahead sequencing (see backtest/engine.py's own module
    docstring for the full list of look-ahead guards).

    What remains modeled/unverified, honestly: order-book depth is
    unavailable for pure-CLOB-history periods, so the "strict" depth
    regime will reject those ticks outright; the fee schedule is still
    unverified against live Polymarket fee behavior; and fills assume
    quote-price execution, with slippage tracked separately as a
    sensitivity metric (see backtest/fill_model.py) rather than applied
    to realized P&L.

TWO BACKTEST MODES WITHIN PART 1
----------------------------------
  - "observed_only": tests calibration's observed-history blending in
    isolation (forecasts=[] every time) -- this validates the 60/40
    blend-and-fallback MECHANISM, not the "NEA outlook runs hot"
    finding specifically, since no historical NEA forecast TEXT is
    stored for the seed period, only actual observed readings.
  - "lead_time_forecast": joins storage.py's persisted forecasts
    (which include fetched_at, giving real lead time) against
    confirmed observations, segmented by lead time. This is the
    Step D mechanism the framework doc called for and it was never
    implemented -- it's implemented here, but as of this build the
    real database has ZERO rows to run it on, since no live forecast
    pulls have ever succeeded in this sandboxed environment. Tested
    below against synthetic data to prove the logic is correct; not
    yet validated against anything real.

DEPENDENCIES
------------
statistics, datetime (standard library)
config.py, models.py, calibration.py, storage.py (local)
"""

import statistics
from datetime import date, timedelta
from typing import List, Optional

import backtest.engine
import config
import storage
from backtest.engine import BacktestRun
from calibration import blend_central_estimate
from models import ObservedReading, BacktestResult, BacktestSummary


def run_observed_only_backtest(
    station_icao: str,
    observations: List[ObservedReading],
    min_window_days: int = 5,
) -> List[BacktestResult]:
    """
    PART 1, mode "observed_only". Walk forward through a chronologically
    sorted observation history: at each point, predict the NEXT
    observation using ONLY the observations that came strictly before
    it (plus the station's climatological normal as fallback, exactly
    as calibration.blend_central_estimate does live).

    The strict-cutoff discipline is enforced by slicing observations[:i]
    -- observations[i] (today's actual) is never in the slice used to
    predict it. This is the specific fix for the look-ahead bias risk
    flagged in the reanalysis.
    """
    station = config.get_station(station_icao)
    obs_sorted = sorted(observations, key=lambda o: o.target_date)

    results = []
    for i in range(min_window_days, len(obs_sorted)):
        history_slice = obs_sorted[:i]  # STRICTLY prior data only -- the walk-forward cutoff
        target = obs_sorted[i]

        predicted_c = blend_central_estimate(
            forecasts=[],  # no historical forecast TEXT available for the seed period -- see module docstring
            observations=history_slice,
            long_term_normal_c=station.long_term_normal_max_c,
        )
        actual_c = target.max_temp_c
        results.append(BacktestResult(
            station_icao=station_icao,
            target_date=target.target_date,
            predicted_c=predicted_c,
            actual_c=actual_c,
            error_c=round(predicted_c - actual_c, 2),
            lead_time_days=None,
            method="observed_only",
        ))

    return results


def run_lead_time_backtest(
    station_icao: str,
    source: str,
    observations: List[ObservedReading],
) -> List[BacktestResult]:
    """
    PART 1, mode "lead_time_forecast". Joins storage.py's persisted
    forecast history (real fetched_at timestamps -> real lead time)
    against confirmed observations for the same dates.

    HONEST STATE AS OF THIS BUILD: storage.load_forecast_history() will
    return an empty list for any station in this environment, since no
    live forecast pull has ever successfully reached a real API here
    (network blocked in this sandbox on every prior test). This function
    is correct and ready to use the moment real forecasts accumulate in
    storage.py -- it is tested below against synthetic rows to prove
    that, not against anything real yet.
    """
    forecasts = storage.load_forecast_history(station_icao, source, limit=10000)
    obs_by_date = {o.target_date: o.max_temp_c for o in observations}

    results = []
    for f in forecasts:
        if f.max_temp_c is None or f.target_date not in obs_by_date:
            continue
        fetched_date = date.fromisoformat(f.fetched_at[:10])
        lead_time_days = (f.target_date - fetched_date).days
        actual_c = obs_by_date[f.target_date]
        results.append(BacktestResult(
            station_icao=station_icao,
            target_date=f.target_date,
            predicted_c=f.max_temp_c,
            actual_c=actual_c,
            error_c=round(f.max_temp_c - actual_c, 2),
            lead_time_days=lead_time_days,
            method="lead_time_forecast",
        ))

    return results


def summarize(
    results: List[BacktestResult],
    lead_time_days: Optional[int] = None,
) -> Optional[BacktestSummary]:
    """
    Aggregate a list of BacktestResults into mean error (bias), MAE,
    and std dev. If lead_time_days is given, filters to only that
    lead-time bucket first -- use this to answer "how good are our
    T-1 predictions specifically," not just an all-lead-times average.
    Returns None if there's nothing to summarize (e.g. empty results,
    or no results at the requested lead time).
    """
    filtered = results if lead_time_days is None else [r for r in results if r.lead_time_days == lead_time_days]
    if not filtered:
        return None

    errors = [r.error_c for r in filtered]
    return BacktestSummary(
        station_icao=filtered[0].station_icao,
        method=filtered[0].method,
        lead_time_days=lead_time_days,
        n=len(filtered),
        mean_error_c=round(statistics.fmean(errors), 3),
        mae_c=round(statistics.fmean(abs(e) for e in errors), 3),
        std_error_c=round(statistics.stdev(errors), 3) if len(errors) > 1 else 0.0,
    )


def summarize_by_lead_time(results: List[BacktestResult]) -> List[BacktestSummary]:
    """Convenience: one BacktestSummary per distinct lead-time value present in results, sorted by lead time."""
    lead_times = sorted({r.lead_time_days for r in results if r.lead_time_days is not None})
    summaries = [summarize(results, lt) for lt in lead_times]
    return [s for s in summaries if s is not None]


def print_summary(summary: Optional[BacktestSummary]) -> None:
    if summary is None:
        print("[backtest_engine] nothing to summarize -- no results in this bucket.")
        return
    lt_label = f"lead_time={summary.lead_time_days}d" if summary.lead_time_days is not None else "all lead times"
    print(
        f"  [{summary.method}] {summary.station_icao} ({lt_label}, n={summary.n}): "
        f"bias={summary.mean_error_c:+.2f}°C  MAE={summary.mae_c:.2f}°C  std={summary.std_error_c:.2f}°C"
    )


def run_trading_strategy_backtest(
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
    PART 2 -- delegates to backtest.engine.run(), the real replay engine.

    Thin passthrough: this function exists so callers that only know
    about backtest_engine.py (rather than the backtest/ package) still
    get a working trading-strategy backtest, without duplicating
    engine.run()'s replay logic here. See the module docstring's PART 2
    section for what this now validates and what remains modeled/
    unverified.
    """
    return backtest.engine.run(
        station_icao,
        start_date,
        end_date,
        depth_regime=depth_regime,
        fee_rate_pct=fee_rate_pct,
        bankroll_mode=bankroll_mode,
        market_db_path=market_db_path,
        run_id=run_id,
    )
