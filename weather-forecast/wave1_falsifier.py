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
     positions -- a record-only wave must not move them.
"""
import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import config


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

        before = _per_station_day(_iso_plus_days(deploy_ts, -7), deploy_ts)
        after = _per_station_day(deploy_ts, _iso_plus_days(deploy_ts, 7))
    finally:
        con.close()
    return {
        "refusals_by_cycle": [(ts, n) for ts, n in by_cycle],
        "refusals_total": refusals_total,
        "calibration_gap_rows": calibration_gap_rows,
        "paper_shadow_rows": paper_shadow_rows,
        "entries_per_station_day_before": before,
        "entries_per_station_day_after": after,
    }


def _print(out: dict, deploy_ts: str) -> None:
    print(f"Wave 1 falsifier -- deploy_ts {deploy_ts}\n")
    print(f"1. refused decisions: {out['refusals_total']} total, by cycle (must grow every entry cycle):")
    for ts, n in out["refusals_by_cycle"][-20:]:
        print(f"     {ts}  {n}")
    print(f"2. calibration gap rows (calibrated_prob NULL with a calibrated source, after deploy): "
          f"{out['calibration_gap_rows']}  -> must be 0")
    print(f"3. paper_shadow rows: {out['paper_shadow_rows']}  -> > 0 once a live station ran an entry cycle")
    print("4. entries per station-day, 7d before -> 7d after (record-only wave: unchanged):")
    stations = sorted(set(out["entries_per_station_day_before"]) | set(out["entries_per_station_day_after"]))
    for icao in stations:
        b = out["entries_per_station_day_before"].get(icao, {})
        a = out["entries_per_station_day_after"].get(icao, {})
        print(f"     {icao}: before {sum(b.values())} over {len(b)} day(s), after {sum(a.values())} over {len(a)} day(s)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deploy-ts", required=True, help="ISO-8601 UTC deploy timestamp, e.g. 2026-09-19T05:00:00+00:00")
    parser.add_argument("--db", default=str(config.DB_PATH), help="database path (opened read-only)")
    args = parser.parse_args(argv)
    _print(run(args.db, args.deploy_ts), args.deploy_ts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
