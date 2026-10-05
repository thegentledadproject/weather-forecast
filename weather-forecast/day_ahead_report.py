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
     python day_ahead_report.py --all      (every station pooled)

--all pools every station's scored days. A date's station-days share one
weather pattern and one set of evening traders, so the bootstrap resamples
DATES, not station-days (35 stations on one date are one draw, not 35).
Same YES rule: model better in BOTH date-halves and P >= 0.95.
"""
import argparse
import random
import statistics
from datetime import date

import bucket_axis
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
                                             axis=bucket_axis.for_station(st))
        total = sum(m for _, _, m in rows)
        if total <= 0 or settled not in {b for b, _, _ in rows}:
            continue
        per_day[day] = [((model - (b == settled)) ** 2, (ask / total - (b == settled)) ** 2)
                        for b, model, ask in rows]
    return {d: (statistics.fmean(m for m, _ in v), statistics.fmean(k for _, k in v))
            for d, v in sorted(per_day.items())}


def pooled():
    """{(icao, day): (model, market)} over every station with day-ahead rows."""
    return {(icao, d): v for icao in sorted(config.STATIONS) for d, v in score(icao).items()}


def pooled_bootstrap(rows, draws=2000, seed=20261005):
    """
    (P(model better), CI95 low, CI95 high, n_dates) on mean(model - market),
    resampling whole dates. None for P/CI below 2 dates.
    """
    by_date = {}
    for (_icao, d), (model, market) in rows.items():
        by_date.setdefault(d, []).append(model - market)
    dates = sorted(by_date)
    if len(dates) < 2:
        return None, None, None, len(dates)
    rng = random.Random(seed)
    means = sorted(statistics.fmean([x for d in rng.choices(dates, k=len(dates)) for x in by_date[d]])
                   for _ in range(draws))
    return (sum(m < 0 for m in means) / draws, means[int(0.025 * draws)],
            means[int(0.975 * draws) - 1], len(dates))


def report_pooled():
    rows = pooled()
    if not rows:
        print("no settled station-days")
        return
    first, second = halves({d for _, d in rows})
    verdict = True
    for label, keep in (("full", None), ("1st", set(first)), ("2nd", set(second))):
        sub = {k: v for k, v in rows.items() if keep is None or k[1] in keep}
        model = statistics.fmean(m for m, _ in sub.values())
        market = statistics.fmean(k for _, k in sub.values())
        p, lo, hi, n_dates = pooled_bootstrap(sub)
        ci = "n/a" if p is None else f"[{lo:+.4f},{hi:+.4f}]"
        print(f"ALL {label}: {len(sub)} station-days / {n_dates} dates / "
              f"{len({s for s, _ in sub})} stations  model {model:.4f} market {market:.4f} "
              f"diff {model - market:+.4f} CI95 {ci} P(model better)={p}")
        verdict = verdict and model < market and (label != "full" or (p or 0) >= 0.95)
    print(f"verdict: {'YES' if verdict else 'NO'} (model better in both halves and P >= 0.95)")
    print("per station (model - market, n days): " + "  ".join(
        f"{s}:{statistics.fmean(m - k for (t, _), (m, k) in rows.items() if t == s):+.3f}"
        f"({sum(t == s for t, _ in rows)})" for s in sorted({s for s, _ in rows})))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--station", default="WSSS")
    ap.add_argument("--all", action="store_true", help="pool every station, date-clustered bootstrap")
    args = ap.parse_args()
    if args.all:
        report_pooled()
        return
    icao = args.station.upper()
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
