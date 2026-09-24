"""Chronological replay: does a REGIONAL calibration map beat production on America?
Every ticket on day D is scored with maps fitted on days < D only, with production's
side handling (NO tickets fit on NO rows). Read-only.
Usage: python calibration_region_replay.py [region]   (default americas)"""
import random, sys
from collections import defaultdict
import config, cohort_monitor, probability_calibration as pc

REGION = sys.argv[1] if len(sys.argv) > 1 else "americas"
region_of = {i: s.region for i, s in config.STATIONS.items()}
K, MIN = config.CALIBRATION_SHRINK_DAYS, config.MIN_CALIBRATION_SAMPLES

rows, _ = cohort_monitor.load_cohort()
rows = [r for r in rows if r.get("model_prob") is not None and r.get("outcome") is not None]
target = [r for r in rows if region_of.get(r["station_icao"]) == REGION]
print(f"rows {len(rows)}  {REGION} {len(target)}  days {len({r['target_date'] for r in target})}")

def ndays(rs):
    return len({r["target_date"] for r in rs})

def shrink(child, parent):
    """child map shrunk toward parent by child's independent days; None-safe."""
    if len(child) < MIN:
        return parent
    m = pc.fit_map(child)
    if parent is None:
        return m
    d = ndays(child)
    return pc.blend_maps(m, parent, d / (d + K))

def maps(r):
    prior = [x for x in rows if x["target_date"] < r["target_date"]]
    if r["side"] == "NO" and config.CALIBRATE_NO_SIDE_SEPARATELY:
        prior = [x for x in prior if x["side"] == "NO"]
    reg = [x for x in prior if region_of.get(x["station_icao"]) == REGION]
    own = [x for x in reg if x["station_icao"] == r["station_icao"]]
    glob = pc.fit_map(prior) if len(prior) >= MIN else None
    region_flat = pc.fit_map(reg) if len(reg) >= MIN else glob
    region_shrunk = shrink(reg, glob)
    return {
        "prod (station->global)": pc.fit_for_day(rows, r["target_date"], r["station_icao"], r["side"])[0],
        "global only": glob,
        "region only": region_flat,
        "region shrunk->global": region_shrunk,
        "station->region": shrink(own, region_flat),
        "station->region->global": shrink(own, region_shrunk),
    }

cache, preds = {}, defaultdict(list)
for r in target:
    key = (r["station_icao"], r["target_date"], r["side"])
    if key not in cache:
        cache[key] = maps(r)
    for name, m in cache[key].items():
        preds[name].append(None if m is None else pc.apply_map(m, float(r["model_prob"])))
    preds["raw model_prob"].append(float(r["model_prob"]))
    preds["ask"].append(None if r.get("entry_price") is None else float(r["entry_price"]))

ok = [i for i in range(len(target)) if all(v[i] is not None for v in preds.values())]
BASE = "prod (station->global)"

def report(label, idx):
    if not idx:
        return
    y = [float(target[i]["outcome"]) for i in idx]
    print(f"\n{label}: n={len(idx)} days={len({target[i]['target_date'] for i in idx})} won={sum(y)/len(y):.3f}")
    base = [(preds[BASE][i] - float(target[i]["outcome"])) ** 2 for i in idx]
    for name in preds:
        se = [(preds[name][i] - float(target[i]["outcome"])) ** 2 for i in idx]
        byday = defaultdict(list)
        for j, i in enumerate(idx):
            byday[target[i]["target_date"]].append(se[j] - base[j])
        rng, ds, bs = random.Random(0), list(byday), []
        for _ in range(2000):
            s = [v for d in (rng.choice(ds) for _ in ds) for v in byday[d]]
            bs.append(sum(s) / len(s))
        bs.sort()
        p = [preds[name][i] for i in idx]
        print(f"  {name:<25} mean p={sum(p)/len(p):.3f}  Brier={sum(se)/len(se):.4f}  vs prod [{bs[50]:+.4f}, {bs[1949]:+.4f}]")

print(f"scored (every variant calibrated): {len(ok)} of {len(target)}")
report(f"ALL {REGION}", ok)
report("excluding KATL", [i for i in ok if target[i]["station_icao"] != "KATL"])
report("KATL only", [i for i in ok if target[i]["station_icao"] == "KATL"])
report("YES", [i for i in ok if target[i]["side"] == "YES"])
report("NO", [i for i in ok if target[i]["side"] == "NO"])
