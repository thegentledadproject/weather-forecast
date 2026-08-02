"""
backtest/cli.py

PURPOSE
-------
Command-line entry point for the backtest tooling, runnable as
`python -m backtest.cli <cmd>`. Thin argparse wrapper over the real
logic in backtest/engine.py, backtest/price_history_client.py,
backtest/snapshot_collector.py and backtest/report.py -- this module
maps CLI flags onto those functions' real parameter names and does no
simulation or reporting logic itself.

SUBCOMMANDS
-----------
  run      -- replay a station over a date range via backtest.engine.run,
              write artifacts, print the summary.
  fetch    -- backfill historical CLOB prices for a station via
              backtest.price_history_client.backfill_station.
  collect  -- run backtest.snapshot_collector's live snapshot collector,
              either once or looping.
  report   -- reload a previous run's summary.json and print it.
  parity   -- run the entry-parity/gate-census pytest suite.

DEPENDENCIES
------------
argparse, json, sys, subprocess, pathlib (standard library)
backtest.engine, backtest.price_history_client, backtest.snapshot_collector,
backtest.settings, backtest.report (local)
"""

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import backtest.engine as engine
import backtest.price_history_client as price_history_client
import backtest.settings as settings
import backtest.snapshot_collector as snapshot_collector


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _cmd_run(args: argparse.Namespace) -> None:
    import backtest.report as report

    run = engine.run(
        station_icao=args.station,
        start_date=args.from_date,
        end_date=args.to_date,
        depth_regime=args.depth_regime,
        fee_rate_pct=args.fee,
        bankroll_mode=args.bankroll_mode,
        market_db_path=args.market_db,
    )
    artifacts_dir = report.write_artifacts(run, out_dir=args.out)
    report.print_report(report.summarize(run))
    print(artifacts_dir)


def _cmd_fetch(args: argparse.Namespace) -> None:
    results = price_history_client.backfill_station(
        station_icao=args.station,
        start_date=args.from_date,
        end_date=args.to_date,
        fidelity_min=args.fidelity,
    )
    print(f"[backtest.cli] fetch: {len(results)} token(s) backfilled for {args.station}")


def _cmd_collect(args: argparse.Namespace) -> None:
    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()]
    if args.once:
        n_saved = snapshot_collector.collect_once(stations)
        print(f"[backtest.cli] collect: single cycle complete -- {n_saved} snapshots saved")
    else:
        snapshot_collector.run_collector(stations, interval_min=args.interval_min)


def _cmd_report(args: argparse.Namespace) -> None:
    import backtest.report as report

    candidate = Path(args.run)
    run_dir = candidate if candidate.exists() else settings.BACKTESTS_OUT_DIR / args.run

    summary_path = run_dir / "summary.json"
    with open(summary_path, "r", encoding="utf-8") as f:
        summary = json.load(f)
    report.print_report(summary)


def _cmd_parity(args: argparse.Namespace) -> None:
    code_dir = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_parity_entry.py", "tests/test_gate_census.py", "-q"],
        cwd=code_dir,
    )
    sys.exit(result.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backtest.cli", description="Backtest tooling CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_run = subparsers.add_parser("run", help="Replay a station over a date range.")
    p_run.add_argument("--station", required=True, help="Station ICAO code, e.g. WSSS")
    p_run.add_argument("--from", dest="from_date", required=True, type=_parse_date, help="Start date, YYYY-MM-DD")
    p_run.add_argument("--to", dest="to_date", required=True, type=_parse_date, help="End date, YYYY-MM-DD")
    p_run.add_argument(
        "--depth-regime",
        choices=["strict", "observed_median", "pessimistic", "unconstrained"],
        default="strict",
    )
    p_run.add_argument("--fee", dest="fee", type=float, default=0.0, help="Fee rate (fraction)")
    p_run.add_argument(
        "--bankroll-mode",
        dest="bankroll_mode",
        choices=["static", "compounding"],
        default="static",
    )
    p_run.add_argument("--market-db", dest="market_db", default=None, help="Path to market data sqlite db")
    p_run.add_argument("--out", dest="out", default=None, help="Artifacts output directory")
    p_run.set_defaults(func=_cmd_run)

    p_fetch = subparsers.add_parser("fetch", help="Backfill historical CLOB prices for a station.")
    p_fetch.add_argument("--station", required=True, help="Station ICAO code, e.g. WSSS")
    p_fetch.add_argument("--from", dest="from_date", required=True, type=_parse_date, help="Start date, YYYY-MM-DD")
    p_fetch.add_argument("--to", dest="to_date", required=True, type=_parse_date, help="End date, YYYY-MM-DD")
    p_fetch.add_argument("--fidelity", type=int, default=5, help="Requested fidelity in minutes (default 5)")
    p_fetch.set_defaults(func=_cmd_fetch)

    p_collect = subparsers.add_parser("collect", help="Run the live snapshot collector.")
    p_collect.add_argument("--stations", required=True, help="Comma-separated ICAO codes, e.g. WSSS,WMKK")
    p_collect.add_argument("--interval-min", dest="interval_min", type=int, default=5)
    p_collect.add_argument("--once", action="store_true", help="Run a single collection cycle and exit")
    p_collect.set_defaults(func=_cmd_collect)

    p_report = subparsers.add_parser("report", help="Reload and print a previous run's summary.")
    p_report.add_argument("--run", required=True, help="Run id or path to a run's artifacts directory")
    p_report.set_defaults(func=_cmd_report)

    p_parity = subparsers.add_parser("parity", help="Run the entry-parity / gate-census pytest suite.")
    p_parity.set_defaults(func=_cmd_parity)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
