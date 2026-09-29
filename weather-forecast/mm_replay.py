"""
mm_replay.py -- would a small paper market-maker have made money?

PURPOSE
-------
fast_recorder.py records the top of book every 15s; this script never places
an order and never writes anything. It replays those recorded books and asks
a narrow question: if we had continuously quoted a tiny two-sided market
around the observed mid, would the fills we'd honestly have gotten (see
HONEST FILL RULE below) have been profitable, on markout and on settlement?

We are a small maker arriving late to someone else's book, not first in the
queue, so touching the best price is not enough to claim a fill -- the
market has to trade THROUGH our price for us to believe we'd have been
filled. That's why the fill rule below is stricter than "price <= our bid".

YES side only: NO order books mirror YES in these neg-risk markets, so
quoting YES both ways already covers both economic directions.

Reads fast_recorder's DB and (for settlement) the trading DB, both opened
read-only via the `mode=ro` URI convention (see markout_report.py) -- this
script must not be able to corrupt either database.

Liquidity rewards are NOT simulated -- Polymarket's maker fee is 0, but any
rebate program is left out; treat P&L here as a lower bound.
"""
import argparse
import bisect
import math
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
import fast_recorder

MAX_SPREAD = 0.10
MID_BOUNDS = (0.05, 0.95)
QUOTE_SHARES = 10
MAX_INV = 50

HALF_SPREAD_GRID = (0.01, 0.02, 0.03)
PULL_SECONDS_GRID = (0, 120, 600)

BOOTSTRAP_REPS = 2000
BOOTSTRAP_SEED = 0
CI_ALPHA = 0.05

MIN_FILLS = 30


def _ro(path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _floor_cents(x: float) -> float:
    return math.floor(round(x * 100, 6)) / 100


def _ceil_cents(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def load_books(conn) -> Dict[Tuple[str, str, int], List[dict]]:
    """{(icao, target_date, bucket): [row, ...]} in seen_at order, YES side
    only. Each row: seen_at (str), dt (parsed), best_bid, best_ask,
    last_trade."""
    rows = conn.execute(
        "SELECT icao, target_date, bucket, seen_at, best_bid, best_ask, last_trade "
        "FROM books WHERE side = 'YES' ORDER BY icao, target_date, bucket, seen_at"
    ).fetchall()
    out: Dict[Tuple[str, str, int], List[dict]] = defaultdict(list)
    for icao, target_date, bucket, seen_at, best_bid, best_ask, last_trade in rows:
        out[(icao, target_date, bucket)].append({
            "seen_at": seen_at,
            "dt": _parse_dt(seen_at),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "last_trade": last_trade,
        })
    return out


def load_metar_times(conn) -> Dict[str, List[datetime]]:
    """{icao: [first_seen, ...]} ascending -- when quotes get pulled."""
    rows = conn.execute("SELECT icao, first_seen FROM metar ORDER BY icao, first_seen").fetchall()
    out: Dict[str, List[datetime]] = defaultdict(list)
    for icao, first_seen in rows:
        out[icao].append(_parse_dt(first_seen))
    return out


def load_settled(conn) -> Dict[Tuple[str, str], int]:
    """{(station_icao, target_date): bucket_c} -- the winning bucket."""
    try:
        rows = conn.execute("SELECT station_icao, target_date, bucket_c FROM settled_buckets").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {(icao, target_date): bucket_c for icao, target_date, bucket_c in rows}


def _pulled(start: datetime, end: datetime, metar_times: List[datetime], pull_s: int) -> bool:
    """True if any metar.first_seen for this icao lands in [start - pull_s, end]:
    the quote posted at `start` was pulled before it could fill at `end`.
    Checking only `start` missed a METAR landing BETWEEN two book rows --
    rows are change-only, so that gap can be many minutes, and the replay
    credited fills a pulling maker would never have taken. Assumes we react
    the moment we SEE the METAR (first_seen, 30s poll): optimistic by up to
    one poll, which pull_s=0 (never pull) brackets from the other side."""
    if pull_s <= 0 or not metar_times:
        return False
    idx = bisect.bisect_right(metar_times, end) - 1
    return idx >= 0 and metar_times[idx] >= start - timedelta(seconds=pull_s)


def simulate(rows: List[dict], metar_times: List[datetime], half_spread: float, pull_s: int) -> List[dict]:
    """Walk one token's recorded states in order, quoting a tiny two-sided
    market around the mid, and return the fills an honest (behind-the-queue)
    maker would have gotten. See module docstring for the fill rule."""
    fills: List[dict] = []
    inv = 0
    for i in range(len(rows) - 1):
        cur, nxt = rows[i], rows[i + 1]
        bid, ask = cur["best_bid"], cur["best_ask"]
        if bid is None or ask is None:
            continue
        if ask - bid > MAX_SPREAD:
            continue
        mid = (bid + ask) / 2
        if not (MID_BOUNDS[0] <= mid <= MID_BOUNDS[1]):
            continue
        if _pulled(cur["dt"], nxt["dt"], metar_times, pull_s):
            continue

        our_bid = _floor_cents(mid - half_spread)
        our_ask = _ceil_cents(mid + half_spread)
        our_bid = min(our_bid, ask - 0.01)
        our_ask = max(our_ask, bid + 0.01)

        nxt_ask, nxt_bid, nxt_trade = nxt["best_ask"], nxt["best_bid"], nxt["last_trade"]
        trade_changed = nxt_trade is not None and nxt_trade != cur["last_trade"]

        if inv < MAX_INV:
            through = (nxt_ask is not None and nxt_ask < our_bid) or (trade_changed and nxt_trade < our_bid)
            if through:
                fills.append({
                    "time": nxt["seen_at"], "dt": nxt["dt"], "side": "buy",
                    "price": our_bid, "shares": QUOTE_SHARES,
                })
                inv += QUOTE_SHARES

        if inv > -MAX_INV:
            through = (nxt_bid is not None and nxt_bid > our_ask) or (trade_changed and nxt_trade > our_ask)
            if through:
                fills.append({
                    "time": nxt["seen_at"], "dt": nxt["dt"], "side": "sell",
                    "price": our_ask, "shares": QUOTE_SHARES,
                })
                inv -= QUOTE_SHARES

    return fills


def _mid_at(rows: List[dict], dts: List[datetime], target: datetime) -> Optional[float]:
    """The mid of the state in force at `target`, or None if no state yet
    exists or the book isn't two-sided then."""
    idx = bisect.bisect_right(dts, target) - 1
    if idx < 0:
        return None
    r = rows[idx]
    if r["best_bid"] is None or r["best_ask"] is None:
        return None
    return (r["best_bid"] + r["best_ask"]) / 2


def score(fills: List[dict], settled: Dict[Tuple[str, str], int]) -> dict:
    """Markout + settlement P&L over a set of already-token-tagged fills.
    Each fill needs: dt, price, side, shares, icao, target_date, bucket,
    _rows, _dts (that token's full row list, for markout/settlement mids)."""
    markout_60 = markout_600 = 0.0
    settlement_pnl = 0.0
    pnl_by_day: Dict[Tuple[str, str], float] = defaultdict(float)
    unsettled = 0

    for f in fills:
        sign = 1 if f["side"] == "buy" else -1

        m60 = _mid_at(f["_rows"], f["_dts"], f["dt"] + timedelta(seconds=60))
        if m60 is not None:
            markout_60 += sign * (m60 - f["price"]) * f["shares"]

        m600 = _mid_at(f["_rows"], f["_dts"], f["dt"] + timedelta(seconds=600))
        if m600 is not None:
            markout_600 += sign * (m600 - f["price"]) * f["shares"]

        day_key = (f["icao"], f["target_date"])
        winner = settled.get(day_key)
        if winner is None:
            unsettled += 1
            continue
        payoff = 1.0 if f["bucket"] == winner else 0.0
        pnl = sign * (payoff - f["price"]) * f["shares"]
        settlement_pnl += pnl
        pnl_by_day[day_key] += pnl

    return {
        "fills": len(fills),
        "markout_60": markout_60,
        "markout_600": markout_600,
        "settlement_pnl": settlement_pnl,
        "unsettled": unsettled,
        "pnl_by_day": dict(pnl_by_day),
    }


def bootstrap_ci(pnl_by_day: Dict[Tuple[str, str], float], reps: int, rng: random.Random) -> Optional[Tuple[float, float]]:
    """95% CI on total settlement P&L from a station-day clustered
    bootstrap: resample station-days with replacement, sum each resample."""
    values = list(pnl_by_day.values())
    if not values:
        return None
    n = len(values)
    draws = sorted(sum(rng.choice(values) for _ in range(n)) for _ in range(reps))
    low_index = int(CI_ALPHA / 2 * (len(draws) - 1))
    high_index = int((1 - CI_ALPHA / 2) * (len(draws) - 1))
    return draws[low_index], draws[high_index]


def best_day_share(pnl_by_day: Dict[Tuple[str, str], float], total: float) -> Optional[float]:
    """What fraction of total settlement P&L came from the single best (or
    worst) day -- a concentration check, not a mean check."""
    if not pnl_by_day:
        return None
    if total == 0:
        return 1.0
    return max(abs(v) for v in pnl_by_day.values()) / abs(total)


def verdict(cell: dict) -> str:
    if cell["fills"] < MIN_FILLS:
        return "NOT ENOUGH FILLS"
    if cell["markout_60"] <= 0:
        return "STOP: adverse selection"
    ci = cell["ci"]
    share = cell["best_day_share"]
    if ci is not None and ci[0] > 0 and share is not None and share < 0.5:
        return "GO"
    return "NOT PROVEN"


def run_grid(groups, metar_times, settled) -> List[dict]:
    dts_by_token = {token: [r["dt"] for r in rows] for token, rows in groups.items()}
    cells = []
    for half_spread in HALF_SPREAD_GRID:
        for pull_s in PULL_SECONDS_GRID:
            fills = []
            for token, rows in groups.items():
                icao, target_date, bucket = token
                for f in simulate(rows, metar_times.get(icao, []), half_spread, pull_s):
                    f.update(icao=icao, target_date=target_date, bucket=bucket,
                             _rows=rows, _dts=dts_by_token[token])
                    fills.append(f)

            s = score(fills, settled)
            rng = random.Random(BOOTSTRAP_SEED)
            ci = bootstrap_ci(s["pnl_by_day"], BOOTSTRAP_REPS, rng)
            share = best_day_share(s["pnl_by_day"], s["settlement_pnl"])
            cell = {
                "half_spread": half_spread, "pull_s": pull_s,
                "fills": s["fills"], "markout_60": s["markout_60"], "markout_600": s["markout_600"],
                "settlement_pnl": s["settlement_pnl"], "unsettled": s["unsettled"],
                "station_days": len(s["pnl_by_day"]), "ci": ci, "best_day_share": share,
            }
            cell["verdict"] = verdict(cell)
            cells.append(cell)
    return cells


def report(cells: List[dict]) -> str:
    lines = ["maker fee is 0 on Polymarket; liquidity rewards NOT simulated -- treat P&L as a lower bound.", ""]
    for c in cells:
        ci = f"[{c['ci'][0]:+.2f}, {c['ci'][1]:+.2f}]" if c["ci"] else "[n/a]"
        share = f"{c['best_day_share']:.2f}" if c["best_day_share"] is not None else "n/a"
        lines.append(
            f"half_spread={c['half_spread']:.2f} pull_s={c['pull_s']:>3}  "
            f"fills={c['fills']:>4} markout_60=${c['markout_60']:+.2f} markout_600=${c['markout_600']:+.2f} "
            f"settle_pnl=${c['settlement_pnl']:+.2f} ci={ci} station_days={c['station_days']} "
            f"best_day_share={share}"
        )
        lines.append(f"    unsettled fills excluded from settlement P&L: {c['unsettled']}")
        if c["fills"] < MIN_FILLS:
            lines.append(f"    min_fills warning: only {c['fills']} fills (< {MIN_FILLS})")
        lines.append(f"    VERDICT: {c['verdict']}")
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1] if __doc__ else "")
    ap.add_argument("--recorder-db", default=str(fast_recorder.DB_PATH))
    ap.add_argument("--trading-db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)

    recorder_conn = _ro(args.recorder_db)
    trading_conn = _ro(args.trading_db)

    groups = load_books(recorder_conn)
    metar_times = load_metar_times(recorder_conn)
    settled = load_settled(trading_conn)

    print(f"{len(groups)} tokens, {len(metar_times)} stations with METAR, "
          f"{len(settled)} settled station-days")

    cells = run_grid(groups, metar_times, settled)
    print()
    print(report(cells))


if __name__ == "__main__":
    main()
