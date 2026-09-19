"""
wave2_falsifier.py -- the Wave 2 pre-registered reads and the stop condition
(spec: "Wave 2 -- correct the inputs, one day"), run read-only at the
boundary and again at boundary + 14 days, appended to memory.

    python wave2_falsifier.py --boundary 2026-09-21

Opens the database READ-ONLY (mode=ro), never through storage._connect()
(which issues DDL). Every statistic is a PURE function the production path
also runs, applied to rows this script read itself:

  (i)   per station, the sd of the per-date forecast error under the Wave 2
        fetch window (local 04:00-08:00) and under the pre-Wave-2 local-day
        window, from the SAME stored rows -- storage.forecast_rows_in_
        error_sample with window_enabled=True/False. The read: the two
        converge over 14 days as the afternoon rows age out of the sample.
  (ii)  per station, the spread the measured tier prices right now
        (calibration.corrected_error_rmse_from_dated, else
        measured_error_spread_from_errors, then priced_measured_spread),
        the tier in use (the "source" field), how many pairs it was scored
        on, and whether the priced value equals SPREAD_FLOOR_C exactly.
        The read: EDDM / RKPK / WSSS must NOT print 0.700. A station on
        the naive "measured_error" tier with 5-14 pairs is gate-blind
        (kept off the 0.70 floor by the Task 2 ruling) -- printed n makes
        that visible instead of silently trusting the number.
  (iii) held-to-settlement return on PAPER rows (is_paper=1 AND
        execution_mode='paper'), 14 days before vs 14 days after the
        boundary, each with cohort_monitor's station-day-clustered CI, and
        the station-day-clustered CI of the DIFFERENCE (after - before).
        STOP CONDITION: the difference's CI lies entirely below zero ->
        flip ERROR_SAMPLE_FETCH_WINDOW_ENABLED and
        SPREAD_FLOOR_MEASURED_TIERS_EXEMPT off TOGETHER.
  (iv)  entry_decisions: 0a2 refusals with a negative admission_edge (2c
        firing) and kelly_nonpositive rows with a negative admission_edge
        (must be 0 after the boundary), 14 days either side. Calibration-
        failure RETRIES (2d) are journal lines, not rows, and are not
        counted.

A DB that predates Wave 1 (no entry_decisions, or a missing column) is
refused with one operator-facing line and exit 1 -- see wave1_falsifier's
SchemaTooOldError / _ro_connect, reused here rather than re-implemented.
"""
import argparse
import random
import sqlite3
import statistics
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import calibration
import cohort_monitor
import config
import storage
from wave1_falsifier import SchemaTooOldError, _ro_connect

WINDOW_DAYS = 14

# Tables/columns this script's own queries depend on. Deliberately its own
# list rather than wave1_falsifier's REQUIRED_SCHEMA: this script also reads
# forecasts/observations/settled_buckets directly (wave1_falsifier does
# not), so importing that module's _check_schema (bound to ITS schema)
# would silently skip checking the tables this script actually touches.
REQUIRED_SCHEMA = {
    "forecasts": ("station_icao", "source", "target_date", "max_temp_c", "fetched_at"),
    "observations": ("station_icao", "target_date", "max_temp_c", "source"),
    "positions": ("station_icao", "target_date", "status", "is_paper", "execution_mode"),
    "settled_buckets": ("station_icao", "target_date", "bucket_c", "bucket_min_c",
                        "bucket_max_c", "bucket_unit", "bucket_step"),
    "entry_decisions": ("cycle_ts", "book", "rule_id", "admission_edge"),
}


def _check_schema(con: sqlite3.Connection) -> None:
    for table, cols in REQUIRED_SCHEMA.items():
        table_info = con.execute(f"PRAGMA table_info({table})").fetchall()
        if not table_info:
            raise SchemaTooOldError(
                f"schema predates Wave 1: table {table!r} is missing -- run after the migration")
        present = {row[1] for row in table_info}
        missing = [c for c in cols if c not in present]
        if missing:
            raise SchemaTooOldError(
                f"schema predates Wave 1: column(s) {missing} missing from {table!r} -- "
                f"run after the migration")


# --- (i) and (ii): the error sample ----------------------------------------

def _forecast_rows(con, icao: str) -> List[Tuple[str, str, float, str]]:
    """forecast_rows_with_fetch_time()'s rows and exclusion rule, read-only."""
    excluded = set(config.FORECAST_SOURCES_EXCLUDED_BY_STATION.get(icao, ()))
    rows = con.execute(
        "SELECT target_date, fetched_at, max_temp_c, source FROM forecasts "
        "WHERE station_icao = ? AND max_temp_c IS NOT NULL", (icao,),
    ).fetchall()
    return [(str(r[0]), str(r[1]), float(r[2]), str(r[3])) for r in rows if str(r[3]) not in excluded]


def _truth(con, icao: str) -> Dict[str, float]:
    source = config.get_station(icao).resolution_grade_source
    rows = con.execute(
        "SELECT target_date, max_temp_c FROM observations "
        "WHERE station_icao = ? AND source = ? AND max_temp_c IS NOT NULL", (icao, source),
    ).fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


def _dated_errors(icao: str, rows, truth, window_enabled: Optional[bool]) -> List[Tuple[date, float]]:
    """forecast_error_samples_dated()'s pairing over rows already in hand."""
    by_date = storage.forecast_rows_in_error_sample(icao, rows, window_enabled=window_enabled)
    return sorted(
        (d, sum(t for _, t in fc) / len(fc) - truth[d.isoformat()])
        for d, fc in by_date.items() if d.isoformat() in truth
    )


def error_sd_by_window(con, icao: str) -> dict:
    rows, truth = _forecast_rows(con, icao), _truth(con, icao)
    out = {}
    for label, enabled in (("morning", True), ("all_day", False)):
        errors = [e for _, e in _dated_errors(icao, rows, truth, enabled)]
        out[label] = {"n": len(errors), "sd": statistics.stdev(errors) if len(errors) >= 2 else None}
    return out


def priced_spread(con, icao: str) -> dict:
    """What the measured tier would price for this station NOW, under the
    production flag, and whether it sits on SPREAD_FLOOR_C exactly."""
    dated = _dated_errors(icao, _forecast_rows(con, icao), _truth(con, icao), None)
    value, n = calibration.corrected_error_rmse_from_dated(dated)
    source = "corrected_error"
    if value is None:
        value, n = calibration.measured_error_spread_from_errors([e for _, e in dated])
        source = "measured_error"
    if value is None:
        return {"source": None, "measured": None, "priced": None, "n": n, "pinned_to_floor": None}
    priced = calibration.priced_measured_spread(value, icao)
    return {"source": source, "measured": value, "priced": priced, "n": n,
            "pinned_to_floor": priced == config.SPREAD_FLOOR_C}


# --- (iii): held-to-settlement, 14 days either side ------------------------

def _paper_rows(con, boundary: date) -> Tuple[List[dict], List[dict]]:
    before, after = [], []
    since, until = boundary - timedelta(days=WINDOW_DAYS), boundary + timedelta(days=WINDOW_DAYS - 1)
    for icao in sorted(config.STATIONS):
        positions = [storage._row_to_position(r) for r in con.execute(
            "SELECT * FROM positions WHERE station_icao = ? AND status != 'open' "
            "AND is_paper = 1 AND execution_mode = 'paper'", (icao,))]
        settled = {
            date.fromisoformat(str(r[0])): (int(r[1]), int(r[2]), int(r[3]), str(r[4]), int(r[5]))
            for r in con.execute(
                "SELECT target_date, bucket_c, bucket_min_c, bucket_max_c, bucket_unit, bucket_step "
                "FROM settled_buckets WHERE station_icao = ?", (icao,))
        }
        rows, _ = cohort_monitor.cohort_rows(positions, settled, since=since, until=until)
        before.extend(r for r in rows if r["target_date"] < boundary)
        after.extend(r for r in rows if r["target_date"] >= boundary)
    return before, after


def _held(rows: List[dict]) -> dict:
    return {
        "n": len(rows),
        "n_days": len(cohort_monitor._clusters(rows)),
        "return_pct": cohort_monitor._return_pct(rows, "held"),
        "ci": cohort_monitor._bootstrap_ci(rows, lambda r: cohort_monitor._return_pct(r, "held")),
    }


def held_difference_ci(before: List[dict], after: List[dict]) -> Optional[Tuple[float, float]]:
    """Station-day-clustered bootstrap CI of held(after) - held(before),
    resampling each side's clusters independently. Same seed, iterations
    and alpha as cohort_monitor._bootstrap_ci."""
    if not before or not after:
        return None
    b_clusters = list(cohort_monitor._clusters(before).values())
    a_clusters = list(cohort_monitor._clusters(after).values())
    rng = random.Random(cohort_monitor.BOOTSTRAP_SEED)
    draws: List[float] = []
    for _ in range(cohort_monitor.BOOTSTRAP_ITERATIONS):
        b = [row for _ in b_clusters for row in rng.choice(b_clusters)]
        a = [row for _ in a_clusters for row in rng.choice(a_clusters)]
        rb = cohort_monitor._return_pct(b, "held")
        ra = cohort_monitor._return_pct(a, "held")
        if rb is not None and ra is not None:
            draws.append(ra - rb)
    if not draws:
        return None
    draws.sort()
    lo = int(cohort_monitor.CI_ALPHA / 2 * (len(draws) - 1))
    hi = int((1 - cohort_monitor.CI_ALPHA / 2) * (len(draws) - 1))
    return draws[lo], draws[hi]


def stop_verdict(before: dict, after: dict, diff_ci) -> str:
    if before["return_pct"] is None or after["return_pct"] is None or diff_ci is None:
        return "NO VERDICT"
    if diff_ci[1] < 0:
        return ("STOP -- post-boundary held return is worse by more than the day-clustered CI: "
                "set ERROR_SAMPLE_FETCH_WINDOW_ENABLED and SPREAD_FLOOR_MEASURED_TIERS_EXEMPT "
                "to False TOGETHER")
    return "holding"


# --- (iv): refusal rows ----------------------------------------------------

def refusal_counts(con, boundary: date) -> dict:
    lo = (boundary - timedelta(days=WINDOW_DAYS)).isoformat()
    mid = boundary.isoformat()
    hi = (boundary + timedelta(days=WINDOW_DAYS)).isoformat()

    def count(rule_id: str, start: str, end: str) -> int:
        return con.execute(
            "SELECT COUNT(*) FROM entry_decisions WHERE rule_id = ? AND admission_edge < 0 "
            "AND book != 'paper_shadow' AND cycle_ts >= ? AND cycle_ts < ?",
            (rule_id, start, end),
        ).fetchone()[0]

    return {
        "0a2_negative_before": count("0a2", lo, mid),
        "0a2_negative_after": count("0a2", mid, hi),
        "kelly_negative_before": count("kelly_nonpositive", lo, mid),
        "kelly_negative_after": count("kelly_nonpositive", mid, hi),
    }


# --- assembly ----------------------------------------------------------------

def run(db_path: str, boundary_iso: str) -> dict:
    boundary = date.fromisoformat(boundary_iso)
    con = _ro_connect(db_path)
    try:
        _check_schema(con)
        stations = sorted(config.STATIONS)
        error_sd = {icao: error_sd_by_window(con, icao) for icao in stations}
        priced = {icao: priced_spread(con, icao) for icao in stations}
        before_rows, after_rows = _paper_rows(con, boundary)
        refusals = refusal_counts(con, boundary)
    finally:
        con.close()
    before, after = _held(before_rows), _held(after_rows)
    diff_ci = held_difference_ci(before_rows, after_rows)
    return {
        "boundary": boundary_iso,
        "error_sd_by_station": error_sd,
        "priced_spread_by_station": priced,
        "held_before": before,
        "held_after": after,
        "held_difference_ci": diff_ci,
        "stop_verdict": stop_verdict(before, after, diff_ci),
        "refusals": refusals,
    }


def _fmt(value, spec: str = ".3f") -> str:
    return "--" if value is None else format(value, spec)


def _print(out: dict) -> None:
    print(f"Wave 2 falsifier -- boundary {out['boundary']}, windows +/-{WINDOW_DAYS}d\n")
    print("(i) forecast error sd, morning window (04-08 local) vs all-day, per station:")
    for icao, sd in out["error_sd_by_station"].items():
        if sd["morning"]["n"] < 2 and sd["all_day"]["n"] < 2:
            continue
        print(f"     {icao}: morning {_fmt(sd['morning']['sd'])} (n={sd['morning']['n']})  "
              f"all-day {_fmt(sd['all_day']['sd'])} (n={sd['all_day']['n']})")
    print(f"\n(ii) priced measured spread per station -- tier in use, n scored pairs, "
          f"pinned == SPREAD_FLOOR_C {config.SPREAD_FLOOR_C:.3f}?:")
    for icao, ps in out["priced_spread_by_station"].items():
        if ps["source"] is None:
            continue
        flag = "  <- PINNED TO THE FLOOR" if ps["pinned_to_floor"] else ""
        print(f"     {icao}: tier={ps['source']} measured {ps['measured']:.3f} priced {ps['priced']:.2f} "
              f"(n={ps['n']} scored pairs){flag}")
    print("\n(iii) held-to-settlement on paper rows:")
    for label in ("held_before", "held_after"):
        h = out[label]
        ci = "" if not h["ci"] else f"  CI [{h['ci'][0]:+.3f}, {h['ci'][1]:+.3f}]"
        print(f"     {label:<11} n={h['n']} over {h['n_days']} station-days  "
              f"return {_fmt(h['return_pct'], '+.3f')}{ci}")
    d = out["held_difference_ci"]
    print(f"     after - before CI: {'--' if d is None else f'[{d[0]:+.3f}, {d[1]:+.3f}]'}")
    print(f"     STOP CONDITION: {out['stop_verdict']}")
    r = out["refusals"]
    print("\n(iv) entry_decisions with admission_edge < 0 (excl. paper_shadow):")
    print(f"     0a2               before {r['0a2_negative_before']}  after {r['0a2_negative_after']}")
    print(f"     kelly_nonpositive before {r['kelly_negative_before']}  after {r['kelly_negative_after']}  -> after must be 0 (2c)")
    print("     calibration-failure retries (2d): not counted -- journal lines, not rows")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--boundary", required=True, help="the Wave 2 deploy date, YYYY-MM-DD (config.REGIME_BOUNDARIES[-1])")
    parser.add_argument("--db", default=str(config.DB_PATH), help="database path (opened read-only)")
    args = parser.parse_args(argv)
    try:
        out = run(args.db, args.boundary)
    except SchemaTooOldError as exc:
        print(str(exc))
        return 1
    _print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
