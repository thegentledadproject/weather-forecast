"""Asia NO calibration gap diagnostic (2026-09-24): breaks the NO-side miss down by
station, period, map sample size, price band; compares today's NO map and the
entry_decisions funnel. Read-only. Usage: python asia_no_gap.py"""
import random, sqlite3, statistics as st
from collections import defaultdict
from datetime import date
import config, cohort_monitor, storage, probability_calibration as pc

region_of = {i: s.region for i, s in config.STATIONS.items()}
rows, _ = cohort_monitor.load_cohort()
rows = [r for r in rows if r.get("model_prob") is not None and r.get("outcome") is not None]

T = []
cache = {}
for r in rows:
    if region_of.get(r["station_icao"]) != "asia" or r["side"] != "NO":
        continue
    k = (r["station_icao"], r["target_date"])
    if k not in cache:
        cache[k] = pc.fit_for_day(rows, r["target_date"], r["station_icao"], "NO")
    m, tier, n = cache[k]
    if tier == pc.NO_TIER:
        continue
    T.append(dict(r, cal=pc.apply_map(m, float(r["model_prob"])), tier=tier, n=n,
                  pnl=(float(r["outcome"]) - r["entry_price"]) * r["shares"]))

def show(label, xs):
    if not xs:
        return
    f = lambda k: sum(float(x[k]) for x in xs) / len(xs)
    b = lambda k: sum((float(x[k]) - float(x["outcome"])) ** 2 for x in xs) / len(xs)
    print(f"  {label:<24} n={len(xs):<3} d={len({x['target_date'] for x in xs}):<3} raw={f('model_prob'):.3f} cal={f('cal'):.3f} "
          f"ask={f('entry_price'):.3f} won={f('outcome'):.3f}  Bcal={b('cal'):.4f} Bask={b('entry_price'):.4f}  "
          f"pnl=${sum(x['pnl'] for x in xs):+.2f} on ${sum(x['size_usd'] for x in xs):.0f}")

print(f"Asia NO scored {len(T)}")
show("ALL", T)
print("by station:")
for s in sorted({x["station_icao"] for x in T}):
    show(s, [x for x in T if x["station_icao"] == s])
print("by period:")
for a, b_, lab in [(date(2000,1,1), date(2026,9,1), "< 09-01"), (date(2026,9,1), date(2026,9,10), "09-01..09"),
                   (date(2026,9,10), date(2026,9,20), "09-10..19"), (date(2026,9,20), date(2100,1,1), ">= 09-20")]:
    show(lab, [x for x in T if a <= x["target_date"] < b_])
print("by map tier / map sample n:")
show("station tier", [x for x in T if x["tier"] == pc.STATION_TIER])
show("pooled tier", [x for x in T if x["tier"] == pc.POOLED_TIER])
show("map n < 100", [x for x in T if x["n"] < 100])
show("map n >= 100", [x for x in T if x["n"] >= 100])
print("by raw model_prob band:")
for lo, hi in [(0, .6), (.6, .7), (.7, .8), (.8, 1.01)]:
    show(f"raw {lo:.1f}-{hi:.1f}", [x for x in T if lo <= float(x["model_prob"]) < hi])
print("by ask band:")
for lo, hi in [(0, .4), (.4, .55), (.55, .7), (.7, 1.01)]:
    show(f"ask {lo:.2f}-{hi:.2f}", [x for x in T if lo <= x["entry_price"] < hi])
print("by exec mode:")
for m in sorted({str(x["execution_mode"]) for x in T}):
    show(m, [x for x in T if str(x["execution_mode"]) == m])

# Other regions' NO, same prod map, for contrast
print("NO in other regions (prod map):")
for reg in ("europe", "americas"):
    xs = []
    for r in rows:
        if region_of.get(r["station_icao"]) != reg or r["side"] != "NO":
            continue
        m, tier, n = pc.fit_for_day(rows, r["target_date"], r["station_icao"], "NO")
        if tier != pc.NO_TIER:
            xs.append(dict(r, cal=pc.apply_map(m, float(r["model_prob"])), pnl=(float(r["outcome"]) - r["entry_price"]) * r["shares"]))
    show(reg, xs)

# Today's NO map applied to the same tickets (in-sample: does the CURRENT map fix it?)
today = date.max  # every settled row: the map production uses now
m_now, t_now, n_now = pc.fit_for_day(rows, today, "WSSS", "NO")
print(f"\ntoday's pooled NO map: tier={t_now} n={n_now}")
for p in (.55, .6, .65, .7, .75, .8, .85, .9):
    print(f"  raw {p:.2f} -> {pc.apply_map(m_now, p):.3f}")
Tn = [dict(x, cal=pc.apply_map(m_now, float(x["model_prob"]))) for x in T]
show("Asia NO, today's map", Tn)

# Entry funnel since Wave 1: Asia NO decisions, settled, approved vs refused
db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
settled = {}
for s in config.STATIONS:
    if region_of[s] == "asia":
        for d, v in storage.load_settled_buckets(s).items():
            settled[(s, d.isoformat())] = v[0]
q = db.execute("""SELECT station_icao, target_date, bucket_c, approved, rule_id, entry_price, model_prob, calibrated_prob, cycle_ts
                  FROM entry_decisions WHERE side='NO' AND book='paper'""").fetchall()
first = {}
for s, d, bkt, ap, rule, px, mp, cp, ts in q:
    if region_of.get(s) != "asia" or (s, d) not in settled or px is None:
        continue
    k = (s, d, bkt)
    if k not in first or ts < first[k][-1]:
        first[k] = (ap, rule, px, mp, cp, 0.0 if settled[(s, d)] == bkt else 1.0, ts)
print(f"\nentry_decisions Asia NO paper, settled, first decision per bucket: {len(first)}")
by = defaultdict(list)
for k, v in first.items():
    by["approved" if v[0] else "refused"].append(v)
    if not v[0]:
        by["refused:" + v[1]].append(v)
for lab, vs in sorted(by.items()):
    won = sum(v[5] for v in vs) / len(vs)
    ask = sum(v[2] for v in vs) / len(vs)
    cp = [v[4] for v in vs if v[4] is not None]
    mp = [v[3] for v in vs if v[3] is not None]
    print(f"  {lab:<36} n={len(vs):<4} ask={ask:.3f} won={won:.3f} raw={st.mean(mp) if mp else float('nan'):.3f} cal={st.mean(cp) if cp else float('nan'):.3f}")
