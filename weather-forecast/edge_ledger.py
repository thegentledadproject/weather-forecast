"""
edge_ledger.py -- plan P10A: does more PREDICTED edge buy more REALIZED edge?

READ-ONLY. Opens the database as a `mode=ro` URI (never through storage, so
it can point at a snapshot) and writes nothing.

PER TRADE (every position whose target date has settled), in probability
points PER SHARE of the token bought:

    raw          model_prob - ask
    calibrated   calibrated_prob - ask                (None before Wave 1)
    robust       p_robust - ask                       (None until the column exists)
    executable   model_prob - ask - fee(ask) - slippage_pct * ask
    filled       model_prob - fill - entry_fee_per_share
    exit         exit_price - fill - entry_fee_per_share   (as traded; exit_price is net)
    realized     outcome - fill - entry_fee_per_share      (held to settlement)

`ask` is the decided ask: the latest APPROVED entry_decisions row for the same
station/date/bucket/side and book at or before entry_time (pairing is 1:N --
several cycles can approve one key), else the latest ev_snapshots row at or
before entry_time, else the fill itself (ask_source says which). slippage_pct
is only ever recorded on ev_snapshots; elsewhere it is 0.

PREDECLARED (written before the first run, 2026-09-24; do not tune):
    * the test variable is EXECUTABLE edge on the raw model_prob -- the only
      prediction stored on every row since August;
    * buckets: <0.03, 0.03-0.06, 0.06-0.10, 0.10-0.20, >=0.20 (half-open);
    * per bucket: n, days, mean predicted, mean realized, date-clustered
      bootstrap 95% CI of mean realized;
    * slope: unweighted per-trade OLS of realized on executable edge, with a
      date-clustered (target_date) bootstrap 95% CI;
    * splits: all / paper / live, and target_date before vs from 2026-09-20.
A positive slope whose CI excludes 0 says predicted edge ranks realized edge.

HONEST FRAMING: this is IN-SAMPLE history -- the model, its gates and the
calibration map were all tuned on this same window. It is a diagnostic, not
the locked out-of-sample test P10B asks for.
"""
import argparse
import random
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import config
import risk_manager
from backtest import resolution

BUCKETS = ((0.03, "<0.03"), (0.06, "0.03-0.06"), (0.10, "0.06-0.10"), (0.20, "0.10-0.20"),
           (float("inf"), ">=0.20"))
SPLIT_DATE = date(2026, 9, 20)
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20260924


def bucket_label(edge: float) -> str:
    return next(label for hi, label in BUCKETS if edge < hi)


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def load_ledger(db_path) -> Tuple[List[dict], Dict[str, int]]:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return _ledger(conn)
    finally:
        conn.close()


def _ledger(conn) -> Tuple[List[dict], Dict[str, int]]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ed_cols = _columns(conn, "entry_decisions") if "entry_decisions" in tables else set()
    robust_col = "p_robust" if "p_robust" in ed_cols else "NULL"
    settled = {(s, d): b for s, d, b in conn.execute(
        "SELECT station_icao, target_date, bucket_c FROM settled_buckets")}
    rows, skipped = [], {}
    positions = conn.execute(
        "SELECT position_id, station_icao, target_date, bucket_c, side, entry_price, entry_time, "
        "status, exit_price, execution_mode, model_prob, calibrated_prob, entry_fee_per_share "
        "FROM positions ORDER BY entry_time").fetchall()
    for (pid, st, td, bucket, side, fill, etime, status, exit_px, mode, p, p_cal, fee_in) in positions:
        key = (st, td, bucket, side)
        winner = settled.get((st, td))
        if winner is None:
            skipped["unsettled"] = skipped.get("unsettled", 0) + 1
            continue
        if p is None or not fill:
            skipped["no model_prob"] = skipped.get("no model_prob", 0) + 1
            continue
        ask, ask_source, slip, p_robust = None, "fill", 0.0, None
        if ed_cols:
            d = conn.execute(
                f"SELECT entry_price, calibrated_prob, {robust_col} FROM entry_decisions "
                "WHERE book = ? AND station_icao = ? AND target_date = ? AND bucket_c = ? AND side = ? "
                "AND approved = 1 AND cycle_ts <= ? ORDER BY cycle_ts DESC LIMIT 1",
                (mode, *key, etime)).fetchone()
            if d and d[0]:
                ask, ask_source, p_robust = d[0], "entry_decisions", d[2]
                p_cal = p_cal if p_cal is not None else d[1]
        if "ev_snapshots" in tables:
            s = conn.execute(
                "SELECT market_price, slippage_pct FROM ev_snapshots WHERE station_icao = ? "
                "AND target_date = ? AND bucket_c = ? AND side = ? AND generated_at <= ? "
                "ORDER BY generated_at DESC LIMIT 1", (*key, etime)).fetchone()
            if s:
                slip = s[1] or 0.0
                if ask is None and s[0]:
                    ask, ask_source = s[0], "ev_snapshots"
        if ask is None:
            ask = fill
        fee_fill = fee_in if fee_in is not None else risk_manager.taker_fee_per_share(fill)
        outcome = resolution.resolution_exit_price(side, bucket, winner)
        rows.append({
            "position_id": pid, "station_icao": st, "target_date": date.fromisoformat(td),
            "bucket_c": bucket, "side": side, "book": mode, "status": status,
            "ask": ask, "ask_source": ask_source, "fill": fill, "outcome": outcome,
            "raw_edge": p - ask,
            "calibrated_edge": None if p_cal is None else p_cal - ask,
            "robust_edge": None if p_robust is None else p_robust - ask,
            "executable_edge": p - ask - risk_manager.taker_fee_per_share(ask) - slip * ask,
            "filled_edge": p - fill - fee_fill,
            "exit_edge": None if exit_px is None else exit_px - fill - fee_fill,
            "realized_edge": outcome - fill - fee_fill,
        })
    return rows, skipped


def _date_bootstrap(rows: Sequence[dict], stat) -> Optional[Tuple[float, float]]:
    by_day: Dict[date, list] = {}
    for r in rows:
        by_day.setdefault(r["target_date"], []).append(r)
    days = list(by_day.values())
    if not days:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    draws = []
    for _ in range(BOOTSTRAP_ITERATIONS):
        sample = [r for _ in days for r in rng.choice(days)]
        v = stat(sample)
        if v is not None:
            draws.append(v)
    if not draws:
        return None
    draws.sort()
    return draws[int(0.025 * (len(draws) - 1))], draws[int(0.975 * (len(draws) - 1))]


def _mean(rows, k):
    return sum(r[k] for r in rows) / len(rows) if rows else None


def _ols(rows, x, y) -> Optional[float]:
    n = len(rows)
    if n < 2:
        return None
    mx, my = _mean(rows, x), _mean(rows, y)
    sxx = sum((r[x] - mx) ** 2 for r in rows)
    if sxx == 0:
        return None
    return sum((r[x] - mx) * (r[y] - my) for r in rows) / sxx


def slope(rows, x="executable_edge", y="realized_edge") -> dict:
    return {"slope": _ols(rows, x, y), "ci": _date_bootstrap(rows, lambda s: _ols(s, x, y)),
            "n": len(rows), "n_days": len({r["target_date"] for r in rows})}


def bucket_table(rows, x="executable_edge", y="realized_edge") -> List[dict]:
    out = []
    for _, label in BUCKETS:
        b = [r for r in rows if bucket_label(r[x]) == label]
        out.append({"bucket": label, "n": len(b), "n_days": len({r["target_date"] for r in b}),
                    "pred": _mean(b, x), "real": _mean(b, y),
                    "ci": _date_bootstrap(b, lambda s: _mean(s, y))})
    return out


def _f(v, fmt="{:+.3f}"):
    return "   n/a" if v is None else fmt.format(v)


def _print_group(name: str, rows: List[dict]) -> None:
    print(f"\n== {name}: {len(rows)} trades, {len({r['target_date'] for r in rows})} days ==")
    print(f"  {'bucket':<10} {'n':>4} {'days':>4} {'pred':>7} {'real':>7}  95% CI (date-clustered)")
    for b in bucket_table(rows):
        ci = "" if b["ci"] is None else f"[{b['ci'][0]:+.3f}, {b['ci'][1]:+.3f}]"
        print(f"  {b['bucket']:<10} {b['n']:>4} {b['n_days']:>4} {_f(b['pred']):>7} {_f(b['real']):>7}  {ci}")
    s = slope(rows)
    ci = "" if s["ci"] is None else f"[{s['ci'][0]:+.3f}, {s['ci'][1]:+.3f}]"
    print(f"  slope realized~executable: {_f(s['slope'])}  95% CI {ci}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)
    rows, skipped = load_ledger(args.db)
    print(f"edge ledger (IN-SAMPLE, not the locked test): {len(rows)} settled trades; skipped {skipped}")
    srcs = {}
    for r in rows:
        srcs[r["ask_source"]] = srcs.get(r["ask_source"], 0) + 1
    print(f"ask source: {srcs}")
    groups = [("all", rows), ("paper", [r for r in rows if r["book"] == "paper"]),
              ("live", [r for r in rows if r["book"] == "live"]),
              (f"all, before {SPLIT_DATE}", [r for r in rows if r["target_date"] < SPLIT_DATE]),
              (f"all, from {SPLIT_DATE}", [r for r in rows if r["target_date"] >= SPLIT_DATE])]
    for name, g in groups:
        _print_group(name, g)
    return 0


if __name__ == "__main__":
    sys.exit(main())
