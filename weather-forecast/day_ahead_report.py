"""
day_ahead_report.py -- WSSS plan step 6a: is a station priced wrong the evening
before? Binary Brier per bucket (YES side), model vs market, against the
bucket the day settled into. Per day ONE recording is used: the latest
generated_at (never a per-bucket mix of evenings). The market side is the
normalized ask (asks rescaled to sum to 1, overround removed): raw YES asks
across buckets sum to more than 1, which would inflate market Brier and hand
the model a false win. A day is skipped if that recording lacks the settled
bucket or its asks sum to 0.

Answer is YES only if model Brier < market Brier in BOTH halves and paired
bootstrap P >= 0.95 on the full window. Read-only.

Run: python day_ahead_report.py --station WSSS
"""
import argparse
import statistics
from datetime import date

import config
import storage
from backtest import resolution
from backtest.observed_half_life import _paired_bootstrap


def halves(days):
    days = sorted(days)
    return days[:len(days) // 2], days[len(days) // 2:]


def score(icao):
    st = config.get_station(icao)
    truth = {o.target_date: o.max_temp_c
             for o in storage.load_observations_since(icao, date(2000, 1, 1))
             if o.source == st.resolution_grade_source}
    recs = {}  # day -> (generated_at, [(bucket, model, ask)])
    for day, bucket, gen, model, market in storage.load_day_ahead_rows(icao):
        if day not in truth:
            continue
        if day not in recs or gen > recs[day][0]:
            recs[day] = (gen, [])
        if gen == recs[day][0]:
            recs[day][1].append((bucket, model, market))
    per_day = {}
    for day, (_, rows) in recs.items():
        settled = resolution.bucket_for_temp(truth[day], st.bucket_min_c, st.bucket_max_c,
                                             edge_mode=st.bucket_edge_mode)
        total = sum(m for _, _, m in rows)
        if total <= 0 or settled not in {b for b, _, _ in rows}:
            continue
        per_day[day] = [((model - (b == settled)) ** 2, (ask / total - (b == settled)) ** 2)
                        for b, model, ask in rows]
    return {d: (statistics.fmean(m for m, _ in v), statistics.fmean(k for _, k in v))
            for d, v in sorted(per_day.items())}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--station", default="WSSS")
    icao = ap.parse_args().station.upper()
    scored = score(icao)
    first, second = halves(scored)
    for label, days in (("full", list(scored)), ("1st", first), ("2nd", second)):
        if not days:
            print(f"{label}: no settled days")
            continue
        model = {d: scored[d][0] for d in days}
        market = {d: scored[d][1] for d in days}
        p, n = _paired_bootstrap(market, model)
        print(f"{icao} {label}: n={n} model {statistics.fmean(model.values()):.4f} "
              f"market {statistics.fmean(market.values()):.4f} P(model better)={p}")


if __name__ == "__main__":
    main()
