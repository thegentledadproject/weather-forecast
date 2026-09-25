"""
lock_score.py -- the gap 5 exam, exactly as pre-registered in
docs/validation/2026-09-24-forward-lock-prereg.md. READ-ONLY, stdlib only.

Unit: a station-day whose FIRST ev_snapshots cycle on the target LOCAL date
lists a YES ask for every bucket, has a settled_buckets row, and (once
LOCK_SHA is filled on 2026-10-05) was priced by config_sha == LOCK_SHA -- EXACT
equality on the full config_fingerprint `<sha>[+dirty]:<mode hash>`, not the
git part: any mode.env / env / argv / dirty-tree change splits the lock.

Forecasters (each a distribution over the unit's listed buckets):
  P    the raw model, YES model_prob renormalised
  M    the YES asks, normalised
  Q    P_robust: m + lambda_robust(D) (p - m), lambda from
       probability_calibration.shrink_for_day(D) (dates < D only), renormalised
  U    uniform
  E30  the station's settled-bucket counts over the prior 30 target dates,
       + 0.5 per listed bucket

Primary metric: multi-class Brier. Secondary: log loss (floor 1e-3), RPS,
10-bin reliability. Comparisons P-E30, P-M, Q-M with a date-cluster bootstrap
(10,000 draws, seed 20261006, 98.33% CI).

    python lock_score.py --start 2026-09-04 --end 2026-09-23
    python lock_score.py --start 2026-10-06 --end 2026-11-02 --locked-read   # not before 2026-11-05
"""
import argparse
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

TEST = (date(2026, 10, 6), date(2026, 11, 2))
READ_DATE = date(2026, 11, 5)
# The FULL config_fingerprint the box stamps (ev_snapshots.config_sha ==
# entry_decisions.config_sha), e.g. '<40-hex sha>:<8 hex>'. Filled on
# 2026-10-05 from the box's own rows, then never edited.
LOCK_SHA = None
DRAWS, SEED, CI = 10_000, 20261006, 0.9833
MIN_DATES, MIN_UNITS, EXTEND_DAYS = 20, 400, 14
LOG_FLOOR = 1e-3
FORECASTERS = ("P", "M", "Q", "U", "E30")
COMPARISONS = (("P", "E30"), ("P", "M"), ("Q", "M"))


# --- scoring (pure) -----------------------------------------------------------

def brier(f, k):
    """Multi-class Brier: sum over buckets of (f_i - [i == k])^2."""
    return sum((x - (1.0 if i == k else 0.0)) ** 2 for i, x in enumerate(f))


def log_loss(f, k):
    return -math.log(max(f[k], LOG_FLOOR))


def rps(f, k):
    """Ranked probability score over the ordered buckets, / (K - 1)."""
    if len(f) < 2:
        return 0.0
    cf = co = s = 0.0
    for i in range(len(f) - 1):
        cf += f[i]
        co += 1.0 if i == k else 0.0
        s += (cf - co) ** 2
    return s / (len(f) - 1)


def normalise(xs):
    t = sum(xs)
    return [x / t for x in xs] if t > 0 else [1.0 / len(xs)] * len(xs)


def e30(history, station, day, buckets):
    """Settled-bucket frequency over the 30 target dates BEFORE `day`, + 0.5
    per listed bucket. `history` = {(station, date): settled bucket}."""
    counts = [0.5] * len(buckets)
    for k in range(1, 31):
        b = history.get((station, day - timedelta(days=k)))
        if b in buckets:
            counts[buckets.index(b)] += 1
    return normalise(counts)


def cluster_bootstrap(per_date, draws=DRAWS, seed=SEED, ci=CI):
    """per_date = {date: (sum of per-unit differences, n units)}. Point = unit
    mean; each draw resamples DATES with replacement. Returns (point, lo, hi)."""
    dates = sorted(per_date)
    point = sum(per_date[d][0] for d in dates) / sum(per_date[d][1] for d in dates)
    rng = random.Random(seed)
    vals = []
    for _ in range(draws):
        s = n = 0.0
        for _ in dates:
            a, c = per_date[dates[rng.randrange(len(dates))]]
            s += a
            n += c
        vals.append(s / n)
    vals.sort()
    tail = (1 - ci) / 2
    return point, vals[int(tail * draws)], vals[min(draws - 1, int((1 - tail) * draws))]


def refuses(start, end, locked_read, today):
    """A reason string if this run would read TEST outcomes early, else None."""
    if end < TEST[0] or start > TEST[1]:
        return None
    if not locked_read:
        return f"{start}..{end} overlaps TEST {TEST[0]}..{TEST[1]}; pass --locked-read on or after {READ_DATE}"
    if today < READ_DATE:
        return f"TEST is sealed until {READ_DATE} (today {today})"
    if LOCK_SHA is None:
        return "LOCK_SHA is not filled in; the TEST read needs the frozen config fingerprint"
    return None


# --- loading (read-only) --------------------------------------------------------

def load_units(con, start, end, lock_sha=None):
    import config

    has_sha = "config_sha" in {r[1] for r in con.execute("PRAGMA table_info(ev_snapshots)")}
    settled = {(st, date.fromisoformat(td)): b
               for st, td, b in con.execute("SELECT station_icao, target_date, bucket_c FROM settled_buckets")}
    units = []
    for st, td in con.execute(
        "SELECT DISTINCT station_icao, target_date FROM ev_snapshots WHERE target_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall():
        d = date.fromisoformat(td)
        if st not in config.STATIONS or (st, d) not in settled:
            continue
        lo, hi = config.local_day_bounds_utc(st, d)
        gens = sorted(g for (g,) in con.execute(
            "SELECT DISTINCT generated_at FROM ev_snapshots WHERE station_icao=? AND target_date=?", (st, td))
            if lo <= datetime.fromisoformat(g) < hi)
        if not gens:
            continue
        cols = "bucket_c, model_prob, market_price" + (", config_sha" if has_sha else "")
        rows = con.execute(
            f"SELECT {cols} FROM ev_snapshots WHERE station_icao=? AND target_date=? AND generated_at=? "
            "AND side='YES' ORDER BY bucket_c", (st, td, gens[0])).fetchall()
        if not rows or any(r[1] is None or r[2] is None for r in rows):
            continue
        if lock_sha is not None and (not has_sha or any(r[3] != lock_sha for r in rows)):
            continue
        buckets = [r[0] for r in rows]
        if settled[(st, d)] not in buckets:
            continue
        units.append({"station": st, "date": d, "buckets": buckets,
                      "p": [r[1] for r in rows], "m": [r[2] for r in rows],
                      "k": buckets.index(settled[(st, d)])})
    return units, settled


def forecasts(unit, history, lam):
    m = normalise(unit["m"])
    # lambda 0 IS the ask: an exact copy, so float noise cannot fake a Q-M gap.
    q = list(m) if lam == 0 else normalise([mi + lam * (pi - mi) for mi, pi in zip(m, unit["p"])])
    n = len(unit["buckets"])
    return {"P": normalise(unit["p"]), "M": m, "Q": q, "U": [1.0 / n] * n,
            "E30": e30(history, unit["station"], unit["date"], unit["buckets"])}


# --- report ----------------------------------------------------------------------

def score(units, settled, lam_for):
    rows = []
    for u in units:
        f = forecasts(u, settled, lam_for(u["date"]))
        rows.append({"u": u, "f": f,
                     "brier": {k: brier(v, u["k"]) for k, v in f.items()},
                     "log": {k: log_loss(v, u["k"]) for k, v in f.items()},
                     "rps": {k: rps(v, u["k"]) for k, v in f.items()}})
    return rows


def diff_by_date(rows, a, b):
    out = defaultdict(lambda: [0.0, 0])
    for r in rows:
        out[r["u"]["date"]][0] += r["brier"][a] - r["brier"][b]
        out[r["u"]["date"]][1] += 1
    return {d: tuple(v) for d, v in out.items()}


def reliability(rows, name):
    bins = [[0.0, 0.0, 0] for _ in range(10)]
    for r in rows:
        for i, x in enumerate(r["f"][name]):
            b = bins[min(9, int(x * 10))]
            b[0] += x
            b[1] += 1.0 if i == r["u"]["k"] else 0.0
            b[2] += 1
    return [(i / 10, s / n, o / n, n) for i, (s, o, n) in enumerate(bins) if n]


def report(rows, draws=DRAWS):
    n_dates = len({r["u"]["date"] for r in rows})
    print(f"units {len(rows)} station-days over {n_dates} dates, "
          f"{len({r['u']['station'] for r in rows})} stations")
    print(f"\n{'forecaster':<10} {'Brier':>8} {'LogLoss':>8} {'RPS':>8}")
    for k in FORECASTERS:
        mean = lambda key: sum(r[key][k] for r in rows) / len(rows)
        print(f"{k:<10} {mean('brier'):8.4f} {mean('log'):8.4f} {mean('rps'):8.4f}")
    print(f"\nBrier differences (negative = first is better), date-cluster bootstrap "
          f"{draws} draws seed {SEED}, {CI:.2%} CI")
    res = {}
    for a, b in COMPARISONS:
        res[(a, b)] = cluster_bootstrap(diff_by_date(rows, a, b), draws=draws)
        pt, lo, hi = res[(a, b)]
        print(f"  {a}-{b:<4} {pt:+.4f} [{lo:+.4f}, {hi:+.4f}]")
    print("\nreliability (bin: mean forecast / observed / n)")
    for k in ("P", "M", "Q"):
        print(f"  {k}: " + "  ".join(f"{lo:.1f}:{mf:.3f}/{ob:.3f}/{n}" for lo, mf, ob, n in reliability(rows, k)))
    print("\nper-station Q-M (re-arm eligibility needs CI upper < 0)")
    by_st = defaultdict(list)
    for r in rows:
        by_st[r["u"]["station"]].append(r)
    for st in sorted(by_st):
        pt, lo, hi = cluster_bootstrap(diff_by_date(by_st[st], "Q", "M"), draws=draws)
        print(f"  {st:<5} n={len(by_st[st]):3d} {pt:+.4f} [{lo:+.4f}, {hi:+.4f}]{'  ELIGIBLE' if hi < 0 else ''}")
    print("\nDECISIONS (pre-registered)")
    if n_dates < MIN_DATES or len(rows) < MIN_UNITS:
        print(f"  INSUFFICIENT: need >= {MIN_DATES} dates and >= {MIN_UNITS} station-days; "
              f"extend ONCE by {EXTEND_DAYS} dates, then read whatever is there.")
    pe = res[("P", "E30")]
    print("  P-E30: " + ("CI not below 0 -> NO SKILL over climatology; live stays off, work returns to forecast inputs"
                         if pe[2] >= 0 else "CI below 0 -> model beats climatology"))
    qm = res[("Q", "M")]
    print("  Q-M:   " + ("CI upper < 0 -> stations above marked ELIGIBLE go to re-arm review"
                         if qm[2] < 0 else "CI upper >= 0 -> no re-arm"))
    return res


def main(argv=None, today=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--locked-read", action="store_true")
    ap.add_argument("--db", default=None, help="default config.DB_PATH; opened read-only")
    ap.add_argument("--draws", type=int, default=DRAWS)
    a = ap.parse_args(argv)
    why = refuses(a.start, a.end, a.locked_read, today or date.today())
    if why:
        print(f"REFUSED: {why}")
        return 2
    import config

    if a.db:
        config.DB_PATH = a.db
    import probability_calibration as pc  # storage opens read-only (never set_writable)

    con = sqlite3.connect(Path(config.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)
    lock = LOCK_SHA if a.locked_read else None
    if lock is None:
        print("NOTE: LOCK_SHA filter not applied (unset, or not a locked read).")
    units, settled = load_units(con, a.start, a.end, lock)
    if not units:
        print("no scorable units")
        return 1
    lam = {}
    rows = score(units, settled, lambda d: lam.setdefault(d, pc.shrink_for_day(d)[0]))
    print(f"lambda_robust by date: " + ", ".join(f"{d}:{v:.3f}" for d, v in sorted(lam.items())))
    report(rows, draws=a.draws)
    return 0


if __name__ == "__main__":
    sys.exit(main())
