"""
backtest/report.py

PURPOSE
-------
The reporting layer for a finished BacktestRun: one pinned summary
dict, a set of on-disk artifacts, and a console report. Nothing here
decides anything about a trade -- engine.py has already finished by the
time any of this runs. What it does decide is what a reader is shown
FIRST, and that is deliberate: data quality before results.

THE PINNED SCHEMA
-----------------
summarize()'s output shape is a contract. A dashboard is built against
these exact key names, so they are pinned here and mapped onto whatever
engine.py's counters/provenance happen to be called. Where the engine's
own dict already carries strictly more information than the pinned
schema asks for (extra counters, the raw coverage block), the extra keys
are KEPT alongside the pinned ones rather than dropped -- a consumer
reading only the pinned names is unaffected, and nothing is lost.

Mappings actually required (verified against engine.py):
  counters      -- every pinned name (n_cycles, n_entry_cycles, n_entries,
                   n_skipped_no_price, n_skipped_implausible_move,
                   rejections) already exists verbatim in run.counters.
                   Passed through whole; no rename needed.
  provenance    -- the pinned names (n_ticks, pct_live_snapshot,
                   pct_with_depth, median_fidelity_min) live one level
                   down, inside run.provenance["coverage"] (built by
                   price_store.coverage_stats). They are LIFTED to the
                   top of the provenance block here, and the original
                   nested "coverage" key is kept as well.
  params        -- run.params is a superset of the pinned three
                   (depth_regime, fee_rate_pct, bankroll_mode). Passed
                   through whole.

DETERMINISM
-----------
summarize() reads the wall clock exactly once, for "generated_at".
Everything else is a pure function of the BacktestRun (plus a post-hoc
read of the observation store for scoring, which is over-and-done
history by the time a run has finished). Two summaries of the same run
differ only in that one field.

BRIER SCORES, AND WHY THE MARKET ONE IS THE POINT
-------------------------------------------------
brier_model scores the calibrated model's probability for the side
actually taken; brier_market scores the price that was paid for it.
An absolute Brier number means nothing on its own -- on a market whose
buckets are mostly near-certain losers, 0.05 is terrible. The ONLY bar
that matters is brier_model < brier_market: the model has to beat the
price it traded against, because the price is what was already on offer
for free. A run that reports a "good" Brier while losing to the market
baseline has found nothing.

DEPENDENCIES
------------
csv, dataclasses, datetime, json, os, pathlib, typing (standard library)
config.py, storage.py, paper_trading_report.py (local)
backtest/{settings,resolution,engine}.py (local)
"""

import csv
import dataclasses
import io
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import config
import paper_trading_report
import storage

from backtest import engine
from backtest import resolution
from backtest import settings

# How far back to look for the observations that score a run. Generous on
# purpose: this is a post-hoc read of finished history, not a decision
# input, so there is no look-ahead cost to widening it. Mirrors the window
# engine.run() uses when it loads observations for calibration.
_SCORING_OBS_LOOKBACK_DAYS = engine.OBSERVATION_WINDOW_DAYS + 400


# --------------------------------------------------------------------------
# JSON coercion
# --------------------------------------------------------------------------


def _jsonable(value):
    """
    Recursively convert a value into something json.dump can write.

    date/datetime/time become ISO strings (models.Position.target_date is a
    real date object, and dataclasses.asdict does not touch it). Paths
    become strings. Everything else passes through, so a genuinely
    unserialisable value still raises rather than being silently stringified
    into a report someone will later trust.
    """
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def _observations_by_date(run) -> Dict[date, float]:
    """
    {target_date: max_temp_c} for this run's station, from the same two
    sources engine.run() resolves against -- StationConfig.seed_observations
    and storage.observations -- collapsed with engine._pick_observation()'s
    tie-break so a score can never disagree with the resolution that
    produced the P&L it is scoring.

    This is a POST-HOC read. The run is over; observation_visible()'s
    publish lag is a guard on DECISIONS, and applying it here would only
    refuse to score days that have since been published.
    """
    station = config.get_station(run.station_icao)

    candidates: Dict[date, List] = {}
    for reading in engine._seed_observations(station):
        candidates.setdefault(reading.target_date, []).append(reading)
    for reading in storage.load_observations_since(
        run.station_icao, run.start_date - timedelta(days=_SCORING_OBS_LOOKBACK_DAYS)
    ):
        candidates.setdefault(reading.target_date, []).append(reading)

    picked: Dict[date, float] = {}
    for target_date, readings in candidates.items():
        # station's own resolution_grade_source, same D1/D2 reasoning as
        # engine._pick_observation's other caller: a score built on a
        # lower-tier reading (e.g. a proxy METAR for Karachi) is not a
        # score of the settlement truth, so it must not be scoreable at all.
        chosen = engine._pick_observation(readings, station)
        if chosen is not None:
            picked[target_date] = chosen.max_temp_c
    return picked


def _positions_by_id(run) -> Dict[str, object]:
    """
    Every position this run produced, keyed by position_id -- the join key
    shared with run.entry_records (engine.run() keys entry_records by the
    same Position.position_id it books the fill under).
    """
    index = {}
    for position in list(run.closed_positions) + list(run.unresolved_positions):
        index[position.position_id] = position
    return index


def _brier_scores(run) -> Dict[str, Optional[float]]:
    """
    (brier_model, brier_market) over every entry whose target date has an
    observation available.

    outcome is 1.0 if the SIDE TAKEN won, 0.0 if it lost -- derived by
    handing the observed bucket to resolution.resolution_exit_price(), the
    same function the engine's resolution sweep pays out with, rather than
    re-deriving yes/no win logic here where it could drift.

    run.entry_records["model_prob"] is ALREADY SIDE-ADJUSTED: engine._entry_pass
    computes `side_model_prob = model_prob if side == "YES" else (1 - model_prob)`
    and stores THAT on the EVResult, and _model_prob_for() reads it straight
    back off that row. So it is P(this side wins), directly comparable to a
    0/1 outcome for this side -- no flipping here.

    Both are None when nothing is scorable. Returning None beats returning
    0.0: a Brier of 0.0 is a perfect score, and an empty run must not be
    able to print one.
    """
    # Station's own bucket bounds/edge mode, not the legacy config globals --
    # same reasoning as engine._resolution_sweep: scoring Hong Kong's 0.1C
    # "floor" readings with half-up whole-degree rounding (or WSSS's 27/37
    # book against the stale 25/35 clamp) would silently score against the
    # wrong bucket, which corrupts the Brier number this report leads with.
    station = config.get_station(run.station_icao)

    observations = _observations_by_date(run)
    positions = _positions_by_id(run)

    model_terms: List[float] = []
    market_terms: List[float] = []

    for position_id in sorted(run.entry_records):
        record = run.entry_records[position_id]
        position = positions.get(position_id)
        if position is None:
            continue

        observed_c = observations.get(position.target_date)
        if observed_c is None:
            continue  # not yet published (or never recorded) -- unscorable

        winning_bucket = resolution.bucket_for_temp(
            observed_c, station.bucket_min_c, station.bucket_max_c, station.bucket_edge_mode
        )
        outcome = resolution.resolution_exit_price(
            position.side, position.bucket_c, winning_bucket
        )

        model_prob = record.get("model_prob")
        if model_prob is not None:
            model_terms.append((float(model_prob) - outcome) ** 2)

        market_price = record.get("market_price")
        if market_price is not None:
            market_terms.append((float(market_price) - outcome) ** 2)

    return {
        "brier_model": (sum(model_terms) / len(model_terms)) if model_terms else None,
        "brier_market": (sum(market_terms) / len(market_terms)) if market_terms else None,
    }


def _slippage_sensitivity_usd(run) -> float:
    """
    Total modelled quote-vs-fill slippage cost across every entry.

    IMPORTANT, AND EASY TO MISREAD: this is NOT already subtracted from the
    realized P&L in summary["metrics"]. The engine records the QUOTE price
    as Position.entry_price for parity with live paper mode (which writes
    decision.entry_price, i.e. ev_result.market_price) -- see
    backtest/fill_model.py. So the metrics block reports the paper-track
    number, and this figure is the what-if adjustment you would apply on top
    of it to ask "and what if the fills had actually been achievable?". It is
    a sensitivity, not a correction, and double-counting it against metrics
    would understate the strategy twice.
    """
    total = 0.0
    for record in run.entry_records.values():
        cost = record.get("slippage_cost_usd")
        if cost is not None:
            total += float(cost)
    return total


def _final_equity_usd(run) -> float:
    """Last marked equity, or the starting bankroll if the run never marked."""
    if run.portfolio.equity_curve:
        return float(run.portfolio.equity_curve[-1][1])
    return float(run.portfolio.initial_bankroll_usd)


# --------------------------------------------------------------------------
# 1. summarize
# --------------------------------------------------------------------------


def summarize(run) -> dict:
    """
    The pinned summary dict for one finished BacktestRun.

    Key names are a contract with the dashboard -- see the module docstring
    for the exact mappings from engine.py's own counter/provenance names.
    `generated_at` is the ONLY wall-clock read in this function; everything
    else is deterministic from `run`.
    """
    counters = dict(run.counters)
    counters["rejections"] = dict(run.counters.get("rejections") or {})

    # Lift price_store.coverage_stats' four fields to the top of the
    # provenance block (that is where the pinned schema expects them) while
    # keeping the engine's own nested "coverage" key intact.
    provenance = dict(run.provenance)
    coverage = dict(run.provenance.get("coverage") or {})
    provenance["n_ticks"] = coverage.get("n_ticks", 0)
    provenance["pct_live_snapshot"] = coverage.get("pct_live_snapshot", 0.0)
    provenance["pct_with_depth"] = coverage.get("pct_with_depth", 0.0)
    provenance["median_fidelity_min"] = coverage.get("median_fidelity_min")

    brier = _brier_scores(run)

    extras = {
        "max_drawdown_pct": float(run.portfolio.max_drawdown_pct),
        "final_equity_usd": _final_equity_usd(run),
        "starting_bankroll_usd": float(run.portfolio.initial_bankroll_usd),
        "brier_model": brier["brier_model"],
        "brier_market": brier["brier_market"],
        "n_unresolved": len(run.unresolved_positions),
    }

    return {
        "run_id": run.run_id,
        "station_icao": run.station_icao,
        "start_date": run.start_date.isoformat(),
        "end_date": run.end_date.isoformat(),
        # run.params is a superset of the pinned three (it also carries
        # station_icao/start_date/end_date/trade_size_screen_usd). Kept whole
        # rather than filtered -- the pinned keys are all present.
        "params": dict(run.params),
        "metrics": paper_trading_report.summarize_positions(list(run.closed_positions)),
        "extras": extras,
        "counters": counters,
        "provenance": provenance,
        # Top level, NOT inside extras: a what-if adjustment on top of
        # metrics, not one of metrics' own figures. See
        # _slippage_sensitivity_usd() for why it is not already netted off.
        "slippage_sensitivity": _slippage_sensitivity_usd(run),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# 2. write_artifacts
# --------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """
    Write `text` to `path` via a .tmp sibling and os.replace().

    os.replace() is atomic on both POSIX and Windows, so a reader (or a
    dashboard polling the directory) can never observe a half-written
    summary.json. A crash mid-write leaves the old file intact and a stray
    .tmp behind, which is the failure mode worth having.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp_path, path)


def _position_row(position, entry_record: Optional[dict]) -> dict:
    """
    One positions.jsonl row: every models.Position field, ISO-ified, merged
    with the four entry-side extras the engine records alongside the fill.

    Join key is position_id -- engine.run() keys entry_records by exactly the
    Position.position_id it opens the position under, so the match is exact
    and never heuristic.
    """
    row = _jsonable(dataclasses.asdict(position))
    if entry_record:
        for key in ("model_prob", "market_price", "net_ev", "slippage_cost_usd"):
            if key in entry_record:
                row[key] = _jsonable(entry_record[key])
    return row


def write_artifacts(run, out_dir=None) -> Path:
    """
    Write this run's artifact set and return the directory it landed in.

    Layout: <out_dir or settings.BACKTESTS_OUT_DIR>/<run_id>/ containing
    summary.json, manifest.json, equity.csv, positions.jsonl and
    decisions.jsonl. run_id is a hash of the run PARAMETERS (see
    engine._make_run_id), so re-running the same experiment overwrites its
    own directory instead of accumulating near-duplicates.

    Every file is written atomically.
    """
    base_dir = Path(out_dir) if out_dir is not None else Path(settings.BACKTESTS_OUT_DIR)
    run_dir = base_dir / run.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # --- summary.json ------------------------------------------------------
    _atomic_write(
        run_dir / "summary.json",
        json.dumps(_jsonable(summarize(run)), indent=2, sort_keys=True) + "\n",
    )

    # --- manifest.json -----------------------------------------------------
    _atomic_write(
        run_dir / "manifest.json",
        json.dumps(_jsonable(run.manifest), indent=2, sort_keys=True) + "\n",
    )

    # --- equity.csv --------------------------------------------------------
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["ts", "equity"])
    for ts, equity in run.portfolio.equity_curve:
        writer.writerow([int(ts), equity])
    _atomic_write(run_dir / "equity.csv", buffer.getvalue())

    # --- positions.jsonl ---------------------------------------------------
    lines = [
        json.dumps(_position_row(p, run.entry_records.get(p.position_id)), sort_keys=True)
        for p in run.closed_positions
    ]
    _atomic_write(run_dir / "positions.jsonl", "".join(line + "\n" for line in lines))

    # --- decisions.jsonl ---------------------------------------------------
    lines = [json.dumps(_jsonable(entry), sort_keys=True) for entry in run.decisions_log]
    _atomic_write(run_dir / "decisions.jsonl", "".join(line + "\n" for line in lines))

    return run_dir


# --------------------------------------------------------------------------
# 3. print_report
# --------------------------------------------------------------------------


def _fmt_pct_of_fraction(value: Optional[float], places: int = 1, signed: bool = False) -> str:
    """
    Render a FRACTION as a percentage. summarize_positions() returns
    fractions (0.42 means 42%), and printing one raw is how a report ends up
    claiming a 0.42% win rate. Python's "%" format spec multiplies by 100 and
    appends the sign, which is exactly the conversion wanted here, and is the
    same idiom paper_trading_report.print_report() uses -- so the two reports
    print the same number the same way.
    """
    if value is None:
        return "n/a"
    spec = f"{'+' if signed else ''}.{places}%"
    return format(float(value), spec)


def _fmt_float(value: Optional[float], places: int = 4) -> str:
    return "n/a" if value is None else f"{float(value):.{places}f}"


def print_report(summary: dict) -> None:
    """
    Console report for a summarize() dict.

    Section order is not cosmetic. Data quality goes FIRST, before a single
    P&L figure, because a result built on 12% coverage is not a weaker
    version of the same finding -- it is a different and much smaller claim,
    and a reader who has already seen the headline return will discount the
    caveat rather than the number.

    Plain print(), matching paper_trading_report.print_report() and
    backtest_engine.py -- this codebase's reports go to stdout, not to a
    logging framework.
    """
    metrics = summary.get("metrics")
    extras = summary.get("extras") or {}
    counters = summary.get("counters") or {}
    provenance = summary.get("provenance") or {}

    print(f"\n=== Backtest Report — {summary.get('station_icao')} ===")
    print(f"run_id:            {summary.get('run_id')}")
    print(f"Window:            {summary.get('start_date')} .. {summary.get('end_date')}")
    params = summary.get("params") or {}
    print(
        f"Params:            depth_regime={params.get('depth_regime')}  "
        f"fee_rate_pct={params.get('fee_rate_pct')}  "
        f"bankroll_mode={params.get('bankroll_mode')}"
    )

    # --- 1. provenance / data quality, deliberately before any result ------
    print("\n-- Data quality (read this before the P&L) --")
    print(f"  Price ticks in window:   {provenance.get('n_ticks', 0)}")
    print(f"  From live order books:   {_fmt_pct_of_fraction(provenance.get('pct_live_snapshot'))}")
    print(f"  Carrying real depth:     {_fmt_pct_of_fraction(provenance.get('pct_with_depth'))}")
    median_fidelity = provenance.get("median_fidelity_min")
    print(
        "  Median sampling:         "
        + ("unknown" if median_fidelity is None else f"{median_fidelity} min")
    )
    if provenance.get("depth_coverage_ok") is False:
        print(
            "  WARNING: depth coverage is below "
            f"{provenance.get('min_depth_coverage_required')} -- a depth-constrained "
            "result on this data is not worth reporting."
        )

    # --- 2. headline metrics (FRACTIONS from summarize_positions) ----------
    print("\n-- Headline --")
    if metrics is None:
        print("  No closed positions -- nothing to summarize.")
    else:
        print(f"  Trades closed:           {metrics['n_trades']}")
        print(f"  Win rate:                {_fmt_pct_of_fraction(metrics['win_rate'])}")
        print(
            f"  Mean return/trade:       {_fmt_pct_of_fraction(metrics['mean_return_pct'], signed=True)}"
            f"  (std {_fmt_pct_of_fraction(metrics['std_return_pct'])})"
        )
        print(f"  Mean winning trade:      {_fmt_pct_of_fraction(metrics['mean_win_pct'], signed=True)}")
        print(f"  Mean losing trade:       {_fmt_pct_of_fraction(metrics['mean_loss_pct'], signed=True)}")
        print(
            f"  Sum of returns (naive):  "
            f"{_fmt_pct_of_fraction(metrics['total_return_pct_sum'], signed=True)}"
            "   <- not compounded"
        )

    print(
        f"  Equity:                  ${extras.get('starting_bankroll_usd', 0.0):,.2f} -> "
        f"${extras.get('final_equity_usd', 0.0):,.2f}"
    )
    print(f"  Max drawdown:            {_fmt_pct_of_fraction(extras.get('max_drawdown_pct'))}")

    # --- 3. exit breakdown --------------------------------------------------
    print("\n-- Exit breakdown --")
    by_reason = (metrics or {}).get("by_exit_reason") or {}
    if not by_reason:
        print("  (none)")
    else:
        for reason in sorted(by_reason):
            stats = by_reason[reason]
            print(
                f"  {reason:22s} n={stats['n']:4d}  "
                f"mean_return={_fmt_pct_of_fraction(stats['mean_return_pct'], signed=True)}"
            )

    # --- 4. funnel ----------------------------------------------------------
    print("\n-- Funnel --")
    print(f"  Cycles:                  {counters.get('n_cycles', 0)}")
    print(f"  Entry cycles:            {counters.get('n_entry_cycles', 0)}")
    print(f"  Entries opened:          {counters.get('n_entries', 0)}")
    print(f"  Skipped, no price:       {counters.get('n_skipped_no_price', 0)}")
    print(f"  Skipped, implausible:    {counters.get('n_skipped_implausible_move', 0)}")
    rejections = counters.get("rejections") or {}
    if not rejections:
        print("  Rejections:              (none)")
    else:
        print("  Rejections:")
        for reason in sorted(rejections):
            print(f"    {reason:24s} {rejections[reason]}")

    # --- 5. brier vs the market baseline ------------------------------------
    print("\n-- Calibration (Brier, lower is better) --")
    brier_model = extras.get("brier_model")
    brier_market = extras.get("brier_market")
    print(f"  Model:                   {_fmt_float(brier_model)}")
    print(f"  Market baseline:         {_fmt_float(brier_market)}")
    if brier_model is None or brier_market is None:
        print("  No scorable entries yet -- no observation available for the entered days.")
    elif brier_model < brier_market:
        print(
            f"  Model BEATS the market by {brier_market - brier_model:.4f}. "
            "That gap, not the absolute number, is the edge."
        )
    elif brier_model > brier_market:
        print(
            f"  Model LOSES to the market by {brier_model - brier_market:.4f} -- "
            "the price was the better forecast; there is no edge here."
        )
    else:
        print("  Model exactly matches the market baseline -- no edge either way.")

    # --- 6. slippage sensitivity --------------------------------------------
    print("\n-- Slippage sensitivity --")
    print(f"  Modelled quote-vs-fill cost: ${summary.get('slippage_sensitivity', 0.0):,.2f}")
    print(
        "  NOT already deducted above: the engine books quote-price fills for\n"
        "  parity with live paper mode, so this is a what-if adjustment on top of\n"
        "  the headline P&L, not a correction to it."
    )

    # --- 7. unresolved -------------------------------------------------------
    print("\n-- Unresolved --")
    print(
        f"  Positions still open at end of window: {extras.get('n_unresolved', 0)}"
        "   (left open on purpose -- no visible observation to settle them)"
    )
    print()
