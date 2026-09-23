"""Chronological replay: is a side-specific calibration map better on NO?
For each ticket on day D, maps are fitted on days < D only. Read-only."""
import random
from collections import defaultdict
import config, cohort_monitor, probability_calibration as pc

rows, _ = cohort_monitor.load_cohort()
rows = [r for r in rows if r.get("model_prob") is not None and r.get("outcome") is not None]
print(f"rows {len(rows)}  NO {sum(r['side']=='NO' for r in rows)}  YES {sum(r['side']=='YES' for r in rows)}")

def variant_combined(r, cache={}):
    k = (r["station_icao"], r["target_date"])
    if k not in cache:
        cache[k] = pc.fit_for_day(rows, r["target_date"], r["station_icao"])
    m, t, _ = cache[k]
    return None if t == pc.NO_TIER else pc.apply_map(m, float(r["model_prob"]))

def make_side(shrink_to_combined_days):
    cache = {}
    def f(r):
        k = (r["station_icao"], r["target_date"], r["side"])
        if k not in cache:
            same = [x for x in rows if x["side"] == r["side"]]
            ms, ts, _ = pc.fit_for_day(same, r["target_date"], r["station_icao"])
            mc, tc, _ = pc.fit_for_day(rows, r["target_date"], r["station_icao"])
            if ts == pc.NO_TIER:
                cache[k] = (mc, tc)
            elif shrink_to_combined_days is None or tc == pc.NO_TIER:
                cache[k] = (ms, ts)
            else:
                d = len({x["target_date"] for x in same if x["target_date"] < r["target_date"]})
                w = d / (d + shrink_to_combined_days)
                cache[k] = (pc.blend_maps(ms, mc, w), ts)
        m, t = cache[k]
        return None if t == pc.NO_TIER else pc.apply_map(m, float(r["model_prob"]))
    return f

variants = {
    "raw model_prob": lambda r: float(r["model_prob"]),
    "combined (prod)": variant_combined,
    "side-only": make_side(None),
    "side shrunk K=10": make_side(10),
    "side shrunk K=40": make_side(40),
}
preds = {name: [f(r) for r in rows] for name, f in variants.items()}
ok = [i for i in range(len(rows)) if all(preds[n][i] is not None for n in variants)]
print(f"scored (every variant calibrated): {len(ok)}")

def summary(side):
    idx = [i for i in ok if rows[i]["side"] == side]
    won = sum(float(rows[i]["outcome"]) for i in idx) / len(idx)
    ask = [rows[i].get("entry_price") for i in idx]
    ask = sum(a for a in ask if a is not None) / max(1, sum(a is not None for a in ask))
    print(f"\n{side}: n={len(idx)} days={len({rows[i]['target_date'] for i in idx})} won={won:.3f} ask={ask:.3f}")
    base = [(preds['combined (prod)'][i] - float(rows[i]['outcome'])) ** 2 for i in idx]
    for n in variants:
        p = [preds[n][i] for i in idx]
        se = [(p[j] - float(rows[i]["outcome"])) ** 2 for j, i in enumerate(idx)]
        byday = defaultdict(list)
        for j, i in enumerate(idx):
            byday[rows[i]["target_date"]].append(se[j] - base[j])
        rng, ds, bs = random.Random(0), list(byday), []
        for _ in range(2000):
            s = [v for d in (rng.choice(ds) for _ in ds) for v in byday[d]]
            bs.append(sum(s) / len(s))
        bs.sort()
        print(f"  {n:<18} mean p={sum(p)/len(p):.3f}  Brier={sum(se)/len(se):.4f}  vs prod [{bs[50]:+.4f}, {bs[1949]:+.4f}]")

summary("NO"); summary("YES")
