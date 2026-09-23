"""Chronological replay: for each day D, fit on days < D, score Brier on D.
Read-only. Usage: python calibration_shrink_replay.py. Re-run before moving config.CALIBRATION_SHRINK_DAYS."""
import random, sys
from collections import defaultdict
import config, cohort_monitor, probability_calibration as pc

ASIA = {i for i, s in config.STATIONS.items() if s.region == "asia"}
rows, _ = cohort_monitor.load_cohort()
rows = [r for r in rows if r.get("model_prob") is not None and r.get("outcome") is not None]
days = sorted({r["target_date"] for r in rows})
print(f"rows {len(rows)}  days {len(days)}  {days[0]}..{days[-1]}")

GRID = [0, 2, 5, 10, 20, 40, 80, 10**9]
# per K: list of (day, station, sq_err, tier, is_asia)
scored = defaultdict(list)
for K in GRID:
    config.CALIBRATION_SHRINK_DAYS = K
    fits = {}
    for r in rows:
        key = (r["station_icao"], r["target_date"])
        if key not in fits:
            fits[key] = pc.fit_for_day(rows, r["target_date"], r["station_icao"])
        m, tier, _ = fits[key]
        if tier == pc.NO_TIER:
            continue
        p = pc.apply_map(m, float(r["model_prob"]))
        scored[K].append((r["target_date"], (p - float(r["outcome"])) ** 2, tier, r["station_icao"] in ASIA))

raw = [(r["target_date"], (float(r["model_prob"]) - float(r["outcome"])) ** 2) for r in rows]

def brier(xs, pred=lambda x: True):
    v = [x[1] for x in xs if pred(x)]
    return sum(v) / len(v) if v else float("nan"), len(v)

def boot_diff(a, b, pred, n=2000):
    """day-cluster bootstrap of mean(a) - mean(b) over the same tickets."""
    byday = defaultdict(list)
    for x, y in zip(a, b):
        if pred(x):
            byday[x[0]].append(x[1] - y[1])
    ds = list(byday)
    rng = random.Random(0)
    out = []
    for _ in range(n):
        s = [v for d in (rng.choice(ds) for _ in ds) for v in byday[d]]
        out.append(sum(s) / len(s))
    out.sort()
    return out[int(.025 * n)], out[int(.975 * n)]

station = lambda x: x[2] == pc.STATION_TIER
asia_station = lambda x: x[2] == pc.STATION_TIER and x[3]
print(f"{'K':>10} {'all':>14} {'station tier':>20} {'asia station':>20}  diff vs K=0 (station tier, 95% day-cluster CI)")
for K in GRID:
    a, s, asi = brier(scored[K]), brier(scored[K], station), brier(scored[K], asia_station)
    ci = boot_diff(scored[K], scored[0], station) if K else (0, 0)
    print(f"{K:>10} {a[0]:.4f} n={a[1]:<5} {s[0]:.4f} n={s[1]:<5} {asi[0]:.4f} n={asi[1]:<5}  [{ci[0]:+.4f}, {ci[1]:+.4f}]")
print("raw model_prob Brier (all rows):", round(brier(raw)[0], 4))
