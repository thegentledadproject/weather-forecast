"""
wave1_falsifier.py -- the four Wave 1 checks from the spec (section 1f),
run the day of deploy and appended to memory.

    python wave1_falsifier.py --deploy-ts 2026-09-19T05:00:00+00:00

Opens the database READ-ONLY (mode=ro): this is an operator read on the
live box and must not be able to write, migrate, or take a write lock.
It therefore does not go through storage._connect(), which issues DDL.

  1. entry_decisions WHERE approved=0 grows every entry cycle
     -> printed per cycle_ts, newest last; a flat count across cycles is
        the failure.
  2. positions WHERE calibrated_prob IS NULL AND calibration_source !=
     'uncalibrated' AND entry_time > deploy_ts  -> must be 0. Pre-deploy
     rows have calibration_source NULL and are excluded by the comparison.
  3. entry_decisions WHERE book='paper_shadow'  -> > 0 once a live station
     has run an entry cycle (WSSS/RCSS at 05:00 SGT), wallet funded or not.
     0 is only a failure if check 1 shows live-book rows for that station
     in the same cycle: the shadow screen is the paper table, which has no
     exit fee, so it clears whenever the live screen did.
  4. entries per station-day, 7 days before vs 7 days after deploy, from
     positions -- a record-only wave must not move them. Printed beside
     it: per-station APPROVAL RATE over the same two windows, from
     entry_decisions keyed on cycle_ts, excluding book='paper_shadow' (the
     shadow book's approvals are not real trades and would otherwise
     inflate the rate).

If the target DB predates the Wave 1 migration (missing entry_decisions,
or missing one of the columns this script reads), main() prints one
operator-facing line naming what's missing and exits non-zero instead of
a stack trace -- see SchemaTooOldError / _check_schema below.
"""
import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import config

# Keep the printed refusal-cycle list bounded so a long-running deploy
# (months of cycles) doesn't spam the terminal -- only the tail matters
# for "is this still growing".
MAX_CYCLES_PRINTED = 20

# Tables/columns this script's queries depend on. Checked up front via
# PRAGMA table_info (safe on a read-only connection, never errors on a
# missing table -- it just returns no rows) so a pre-Wave-1 DB fails with
# one clear line instead of a stack trace from the SELECT that hits it.
REQUIRED_SCHEMA = {
    "entry_decisions": ("cycle_ts", "station_icao", "book", "approved"),
    "positions": ("station_icao", "entry_time", "calibrated_prob", "calibration_source"),
}


class SchemaTooOldError(RuntimeError):
    """Raised when the target DB predates a Wave 1 migration this script needs."""


def _check_schema(con: sqlite3.Connection) -> None:
    for table, cols in REQUIRED_SCHEMA.items():
        table_info = con.execute(f"PRAGMA table_info({table})").fetchall()
        if not table_info:
            raise SchemaTooOldError(
                f"schema predates Wave 1: table {table!r} is missing -- run after the migration"
            )
        present = {row[1] for row in table_info}
        missing = [c for c in cols if c not in present]
        if missing:
            raise SchemaTooOldError(
                f"schema predates Wave 1: column(s) {missing} missing from {table!r} table -- "
                f"run after the migration"
            )


def _ro_connect(db_path: str) -> sqlite3.Connection:
    # Path.as_uri() produces the platform-correct file: URI (including the
    # extra slash for a Windows drive letter, e.g. file:///C:/...) on both
    # the Windows dev box and the Linux production box.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _iso_plus_days(ts: str, days: int) -> str:
    return (datetime.fromisoformat(ts) + timedelta(days=days)).isoformat()


def run(db_path: str, deploy_ts: str) -> dict:
    con = _ro_connect(db_path)
    try:
        _check_schema(con)
        by_cycle = con.execute(
            "SELECT cycle_ts, COUNT(*) FROM entry_decisions WHERE approved = 0 "
            "GROUP BY cycle_ts ORDER BY cycle_ts"
        ).fetchall()
        refusals_total = sum(n for _, n in by_cycle)
        calibration_gap_rows = con.execute(
            "SELECT COUNT(*) FROM positions WHERE calibrated_prob IS NULL "
            "AND calibration_source != 'uncalibrated' AND entry_time > ?",
            (deploy_ts,),
        ).fetchone()[0]
        paper_shadow_rows = con.execute(
            "SELECT COUNT(*) FROM entry_decisions WHERE book = 'paper_shadow'"
        ).fetchone()[0]

        def _per_station_day(lo: str, hi: str) -> dict:
            rows = con.execute(
                "SELECT station_icao, substr(entry_time, 1, 10) AS day, COUNT(*) FROM positions "
                "WHERE entry_time >= ? AND entry_time < ? GROUP BY station_icao, day "
                "ORDER BY station_icao, day",
                (lo, hi),
            ).fetchall()
            out: dict = {}
            for icao, day, n in rows:
                out.setdefault(icao, {})[day] = n
            return out

        def _approval_rate_by_station(lo: str, hi: str) -> dict:
            rows = con.execute(
                "SELECT station_icao, SUM(approved), COUNT(*) FROM entry_decisions "
                "WHERE book != 'paper_shadow' AND cycle_ts >= ? AND cycle_ts < ? "
                "GROUP BY station_icao ORDER BY station_icao",
                (lo, hi),
            ).fetchall()
            out: dict = {}
            for icao, approved, total in rows:
                approved = approved or 0
                out[icao] = {
                    "approved": approved,
                    "total": total,
                    "rate": approved / total if total else 0.0,
                }
            return out

        before_lo, after_hi = _iso_plus_days(deploy_ts, -7), _iso_plus_days(deploy_ts, 7)
        entries_before = _per_station_day(before_lo, deploy_ts)
        entries_after = _per_station_day(deploy_ts, after_hi)
        approval_before = _approval_rate_by_station(before_lo, deploy_ts)
        approval_after = _approval_rate_by_station(deploy_ts, after_hi)
    finally:
        con.close()
    return {
        "refusals_by_cycle": [(ts, n) for ts, n in by_cycle],
        "refusals_total": refusals_total,
        "calibration_gap_rows": calibration_gap_rows,
        "paper_shadow_rows": paper_shadow_rows,
        "entries_per_station_day_before": entries_before,
        "entries_per_station_day_after": entries_after,
        "approval_rate_by_station_before": approval_before,
        "approval_rate_by_station_after": approval_after,
    }


def _fmt_rate(rates: dict, icao: str) -> str:
    r = rates.get(icao)
    if r is None:
        return "n/a"
    return f"{r['approved']}/{r['total']} ({r['rate'] * 100:.1f}%)"


def _print(out: dict, deploy_ts: str) -> None:
    print(f"Wave 1 falsifier -- deploy_ts {deploy_ts}\n")
    print(f"1. refused decisions: {out['refusals_total']} total, by cycle (must grow every entry cycle):")
    for ts, n in out["refusals_by_cycle"][-MAX_CYCLES_PRINTED:]:
        print(f"     {ts}  {n}")
    print(f"2. calibration gap rows (calibrated_prob NULL with a calibrated source, after deploy): "
          f"{out['calibration_gap_rows']}  -> must be 0")
    print(f"3. paper_shadow rows: {out['paper_shadow_rows']}  -> > 0 once a live station ran an entry cycle")
    print("4. entries per station-day, 7d before -> 7d after (record-only wave: unchanged), "
          "with per-station APPROVAL RATE over the same windows (excludes book='paper_shadow'):")
    stations = sorted(
        set(out["entries_per_station_day_before"]) | set(out["entries_per_station_day_after"])
        | set(out["approval_rate_by_station_before"]) | set(out["approval_rate_by_station_after"])
    )
    for icao in stations:
        b = out["entries_per_station_day_before"].get(icao, {})
        a = out["entries_per_station_day_after"].get(icao, {})
        rb = _fmt_rate(out["approval_rate_by_station_before"], icao)
        ra = _fmt_rate(out["approval_rate_by_station_after"], icao)
        print(f"     {icao}: before {sum(b.values())} over {len(b)} day(s), after {sum(a.values())} over {len(a)} day(s)"
              f"  |  approval rate before {rb}, after {ra}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deploy-ts", required=True, help="ISO-8601 UTC deploy timestamp, e.g. 2026-09-19T05:00:00+00:00")
    parser.add_argument("--db", default=str(config.DB_PATH), help="database path (opened read-only)")
    args = parser.parse_args(argv)
    try:
        out = run(args.db, args.deploy_ts)
    except SchemaTooOldError as exc:
        print(str(exc))
        return 1
    _print(out, args.deploy_ts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
