"""
day_ahead_report.py -- WSSS plan step 6a: is a station priced wrong the evening
before? Binary Brier per bucket (YES side), model vs market, on the LATEST
evening recording per (day, bucket), against the bucket the day settled into.

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
    latest = {}
    for day, bucket, gen, model, market in storage.load_day_ahead_rows(icao):
        key = (day, bucket)
        if key not in latest or gen > latest[key][0]:
            latest[key] = (gen, model, market)
    per_day = {}
    for (day, bucket), (_, model, market) in latest.items():
        if day not in truth:
            continue
        settled = resolution.bucket_for_temp(truth[day], st.bucket_min_c, st.bucket_max_c,
                                             edge_mode=st.bucket_edge_mode)
        hit = 1.0 if bucket == settled else 0.0
        per_day.setdefault(day, []).append(((model - hit) ** 2, (market - hit) ** 2))
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
