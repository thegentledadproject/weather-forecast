"""
markout_report.py -- how the market moved AFTER each entry (honest fills, P7).

    python markout_report.py [--db PATH] [--market-db PATH] [--by side|band|station|book]

READ-ONLY: both databases are opened with a sqlite `mode=ro` URI, never
through storage._connect() or price_store._connect() (both issue DDL).

For every position (paper, simulation and live) it reads the book from
price_store's `live_snapshot` rows -- `price` is the BID, `ask_price` the ASK,
5-minute fidelity -- at entry and at +15m, +1h, +6h, using the last snapshot
AT OR BEFORE each instant and no older than 2 x fidelity (price_store's own
staleness rule). mid = (bid + ask) / 2, defined only when both sides quote
(bid > 0): an empty bid side has no mid.

Reported per horizon, all in price units (dollars per share):
  mid move      mid(t+h) - mid(entry)         did the market move toward us?
  markout       mid(t+h) - entry_price        what the fill was worth at t+h
  bid / ask     bid(t+h) - bid(entry), ask likewise
and at settlement: payoff (1 if this side won, else 0) minus mid(entry) and
minus entry_price. The winner comes from settled_buckets, else from a
closed_resolution exit price.

Means carry a DATE-clustered bootstrap 95% CI (cohort_monitor._bootstrap_ci,
clustered on target_date: every station on one day shares the weather
regime, so the day, not the row, is the independent unit).
"""
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import config
import cohort_monitor
from backtest import settings as bt_settings

HORIZONS = (("15m", 15 * 60), ("1h", 3600), ("6h", 6 * 3600))
SNAPSHOT_SOURCE = "live_snapshot"
MAX_STALE_S = int(bt_settings.MAX_STALENESS_FACTOR * 5 * 60)
BANDS = ((0.0, 0.15), (0.15, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 1.01))


def _ro(path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def band_of(price: float) -> str:
    for lo, hi in BANDS:
        if lo <= price < hi:
            return f"{lo:.2f}-{min(hi, 1.0):.2f}"
    return "?"


def quote_at(market, token_id: str, ts: int) -> Optional[dict]:
    """bid/ask/mid from the last live snapshot at or before ts, or None if stale/absent."""
    row = market.execute(
        "SELECT price, ask_price, ts FROM price_snapshots "
        "WHERE token_id = ? AND source = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (token_id, SNAPSHOT_SOURCE, int(ts)),
    ).fetchone()
    if row is None or ts - row[2] > MAX_STALE_S:
        return None
    bid, ask = row[0], row[1]
    mid = (bid + ask) / 2 if (ask is not None and bid is not None and bid > 0) else None
    return {"bid": bid, "ask": ask, "mid": mid}


def _diff(a, b):
    return None if a is None or b is None else a - b


def entry_rows(db, market) -> List[dict]:
    settled = {
        (s, d): b for s, d, b in db.execute(
            "SELECT station_icao, target_date, bucket_c FROM settled_buckets")
    }
    out = []
    for (pid, station, target_date, bucket_c, side, entry_price, entry_time, status,
         exit_price, token_id, mode) in db.execute(
            "SELECT position_id, station_icao, target_date, bucket_c, side, entry_price, "
            "entry_time, status, exit_price, token_id, execution_mode FROM positions "
            "WHERE token_id IS NOT NULL"):
        t0 = int(datetime.fromisoformat(entry_time).timestamp())
        q0 = quote_at(market, token_id, t0)
        row = {"position_id": pid, "station": station, "side": side, "book": mode,
               "band": band_of(entry_price), "entry_price": entry_price,
               "cluster": target_date}
        for name, dt in HORIZONS:
            qh = quote_at(market, token_id, t0 + dt)
            for k in ("mid", "bid", "ask"):
                row[f"{k}_{name}"] = _diff(qh and qh[k], q0 and q0[k])
            row[f"markout_{name}"] = _diff(qh and qh["mid"], entry_price)
        winner = settled.get((station, target_date))
        if winner is not None:
            payoff = float((winner == bucket_c) == (side.upper() == "YES"))
        elif status == "closed_resolution" and exit_price is not None:
            payoff = 1.0 if exit_price >= 0.5 else 0.0
        else:
            payoff = None
        row["mid_settle"] = _diff(payoff, q0 and q0["mid"])
        row["markout_settle"] = _diff(payoff, entry_price)
        out.append(row)
    return out


def summarise(rows: List[dict], metric: str) -> Optional[dict]:
    have = [r for r in rows if r.get(metric) is not None]
    if not have:
        return None
    stat = lambda sample: sum(r[metric] for r in sample) / len(sample) if sample else None  # noqa: E731
    ci = cohort_monitor._bootstrap_ci(have, stat)
    return {"n": len(have), "days": len({r["cluster"] for r in have}), "mean": stat(have), "ci": ci}


METRICS = ("mid_15m", "mid_1h", "mid_6h", "mid_settle", "markout_15m", "markout_1h", "markout_6h",
           "markout_settle", "bid_1h", "ask_1h")


def report(rows: List[dict], by: str) -> str:
    groups: Dict[str, List[dict]] = {"ALL": rows}
    for r in rows:
        groups.setdefault(str(r[by]), []).append(r)
    lines = [f"markouts by {by} (price units; mean [95% date-clustered CI], n)"]
    for metric in METRICS:
        lines.append(f"\n  {metric}")
        for key in sorted(groups, key=lambda k: (k != "ALL", k)):
            s = summarise(groups[key], metric)
            if s is None:
                continue
            lo, hi = s["ci"] if s["ci"] else (float("nan"), float("nan"))
            lines.append(f"    {key:<14} {s['mean']:+.4f} [{lo:+.4f}, {hi:+.4f}]  n={s['n']} days={s['days']}")
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--market-db", default=str(bt_settings.MARKET_DATA_DB))
    ap.add_argument("--by", action="append", choices=("side", "band", "station", "book"))
    args = ap.parse_args(argv)
    db, market = _ro(args.db), _ro(args.market_db)
    rows = entry_rows(db, market)
    print(f"{len(rows)} entries; with an entry-time quote: "
          f"{sum(1 for r in rows if r['mid_15m'] is not None or r['bid_15m'] is not None)}")
    for by in args.by or ["side", "band", "station", "book"]:
        print()
        print(report(rows, by))


if __name__ == "__main__":
    main()
