"""
backtest/observed_half_life.py

PURPOSE
-------
Choose config.OBSERVED_HALF_LIFE_DAYS by measurement rather than by hand.

The observed term of the central estimate carries 60% of the blend for WSSS
and was a plain unweighted mean, so it could not track a regime: WSSS
settled 33.0 on each of 2026-08-19..25 while the term sat at 32.538, and the
book bought 32:YES and 33:NO against that run for -45.2% over nine
positions. calibration.observed_mean_weighted() adds exponential recency
decay and ships inert (None = the unweighted mean). This module scores
candidate half-lives so the constant can move on evidence.

WHAT IS SCORED, AND WHY IT IS BRIER
-----------------------------------
Multi-class Brier over the whole bucket distribution, against the bucket the
day actually settled into. NEVER RMSE: the 2026-08-10 blend-weight
measurement found RMSE overstated that gain roughly fourfold (-39.2% RMSE
against -10.9% Brier), and "do not tune this system on RMSE" is a standing
conclusion from it. Hit rate (modal bucket correct) is reported alongside
because Brier and hit rate can disagree -- that same measurement found the
central estimate informative (32% vs 9.1% chance) while the distribution was
too diffuse to convert it into Brier skill.

Scored through the REAL production chain -- blend_central_estimate,
estimate_std_dev, probability.bucket_probabilities,
resolution.bucket_for_temp -- so a candidate is judged on what the trading
system would actually have believed, not on a reimplementation of it.

WHAT IS HELD OUT
----------------
Bias is applied LEAVE-ONE-OUT: the bias correction for day D is fitted on
every OTHER day's (forecast - settled truth) pair. Without that a day is
scored against a correction fitted partly on itself, which flatters every
candidate equally but by an unknown amount.

Observations for day D are strictly those with target_date < D, inside
config.OBSERVATION_LOOKBACK_DAYS. Forecasts are the latest stored row per
source for D. A day's own settlement can therefore never inform its own
estimate.

WHAT THIS DOES NOT MEASURE, AND MUST NOT BE READ AS
---------------------------------------------------
1. P&L. A Brier improvement is not a profit claim. backtest/compare.py
   refuses a P&L verdict below MIN_TRADES_FOR_A_VERDICT=30 and that bar is
   not met here.
2. EXPOSURE. A better-centred distribution raises model_prob, hence
   raw_edge, hence Kelly size -- the 2026-08-10 spread change produced 5
   entries against 3, and sizes of $20.93/$85.13 against $1.05/$1.05.
   Any gain here arrives together with more risk, and quantifying that
   needs a full engine replay per candidate. NOT done here; the spec asks
   for it and this module does not supply it. Read the ordering, then run
   the replay before acting.
3. Spread leakage. estimate_std_dev's measured tiers read the whole stored
   error record, which is why backtest/engine.py passes
   allow_measured_spread=False. This module runs the production chain with
   the leak IN, deliberately: it is common-mode across candidates (the same
   spread is handed to every half-life on the same day), so it cannot
   reorder them, and switching it off would score a distribution the live
   system never produces. It does mean the ABSOLUTE Brier here is
   optimistic; only the differences are meaningful.

THE BAR, restated from the spec so it cannot drift
--------------------------------------------------
A candidate earns the constant only if ALL of:
  - paired bootstrap P(candidate better than None) >= 0.95, AND
  - the ordering holds PER STATION rather than being carried by one, AND
  - the gain is not concentrated in regime-reversal days (whipsaw is the
    named risk: WSSS settled 31.0 on 2026-08-26 straight after seven 33s).
Below that bar the constant stays None and the tool is the deliverable --
the outcome stop_sweep.py and take_sweep.py both reached.

DEPENDENCIES
------------
argparse, contextlib, datetime, math, random, statistics (standard library)
config.py, calibration.py, probability.py, storage.py (local)
backtest/resolution.py (local)
"""

import argparse
import random
import statistics
from contextlib import contextmanager
from datetime import date, timedelta

import calibration
import config
import probability
import storage
from backtest import resolution

# Resampling draws for the paired bootstrap. 2000 is enough to separate 0.95
# from 0.94 without the run time mattering at this sample size.
BOOTSTRAP_DRAWS = 2000


@contextmanager
def _half_life(value):
    """Set the observed half-life for the duration, then put it back."""
    old = config.OBSERVED_HALF_LIFE_DAYS
    config.OBSERVED_HALF_LIFE_DAYS = value
    try:
        yield
    finally:
        config.OBSERVED_HALF_LIFE_DAYS = old


def _sources_for(station):
    """
    The forecast sources this station actually stores.

    Taken from engine.py's constants rather than re-derived, so the set this
    scores over cannot drift from the set the replay reads -- a station whose
    official client key is missing there silently loses its official forecast
    in BOTH places, which is at least consistent and is a known gap recorded
    at OFFICIAL_SOURCE_BY_CLIENT_KEY.
    """
    from backtest import engine

    sources = list(engine.OPEN_METEO_SOURCES)
    official = engine.OFFICIAL_SOURCE_BY_CLIENT_KEY.get(station.official_client_key)
    if official:
        sources.append(official)
    return sources


def _forecasts_by_day(station):
    """{target_date: [PointForecast]} -- the latest stored row per source."""
    out = {}
    for source in _sources_for(station):
        for f in storage.load_forecast_history(station.icao, source, limit=10000):
            if f.max_temp_c is None:
                continue
            slot = out.setdefault(f.target_date, {})
            prev = slot.get(f.source)
            # Latest fetch wins: live calibrates on the newest row per source.
            if prev is None or (f.fetched_at or "") >= (prev.fetched_at or ""):
                slot[f.source] = f
    return {d: list(by_source.values()) for d, by_source in out.items()}


def score_station(station_icao, half_life):
    """
    [(target_date, brier, hit)] for every scorable day at this half-life.

    A day is scorable when it has settlement-grade truth, at least one
    forecast, and at least one earlier observation -- i.e. when the live
    system would have had something to calibrate on.
    """
    station = config.get_station(station_icao)
    truth = {
        o.target_date: o.max_temp_c
        for o in storage.load_observations_since(station_icao, date(2000, 1, 1))
        if o.source == station.resolution_grade_source
    }
    if not truth:
        return []

    forecasts_by_day = _forecasts_by_day(station)
    dated_errors = storage.forecast_error_samples_dated(
        station_icao, station.resolution_grade_source
    )
    all_obs = [
        o for o in storage.load_observations_since(station_icao, date(2000, 1, 1))
        if o.source == station.resolution_grade_source
    ]

    rows = []
    for day in sorted(truth):
        forecasts = forecasts_by_day.get(day, [])
        if not forecasts:
            continue
        window_start = day - timedelta(days=config.OBSERVATION_LOOKBACK_DAYS)
        observations = [
            o for o in all_obs if window_start <= o.target_date < day
        ]
        if not observations:
            continue

        # LEAVE-ONE-OUT: this day's own pair never informs its own correction.
        loo = [e for d, e in dated_errors if d != day]
        bias, _, _ = calibration.bias_stats(loo)

        with _half_life(half_life):
            estimate = calibration.calibrate(
                station=station,
                target_date=day,
                forecasts=forecasts,
                observations=observations,
                forecast_bias_c=bias or 0.0,
            )
        buckets = probability.bucket_probabilities(
            estimate, station.bucket_min_c, station.bucket_max_c,
            edge_mode=station.bucket_edge_mode,
        )
        settled = resolution.bucket_for_temp(
            truth[day], station.bucket_min_c, station.bucket_max_c,
            edge_mode=station.bucket_edge_mode,
        )

        # Multi-class Brier: sum over buckets of (p - outcome)^2.
        brier = sum(
            (b.probability - (1.0 if b.bucket_c == settled else 0.0)) ** 2
            for b in buckets
        )
        modal = max(buckets, key=lambda b: b.probability).bucket_c
        rows.append((day, brier, modal == settled))
    return rows


def sweep(stations, candidates):
    """{half_life: {station: [(day, brier, hit)]}}."""
    out = {}
    for hl in candidates:
        per_station = {}
        for icao in stations:
            try:
                scored = score_station(icao, hl)
            except Exception as exc:  # noqa: BLE001 -- one station must not void the sweep
                print(f"  [observed_half_life] {icao} failed at hl={hl}: "
                      f"{type(exc).__name__}: {str(exc)[:100]}")
                continue
            if scored:
                per_station[icao] = scored
        out[hl] = per_station
    return out


def _paired_bootstrap(baseline, candidate, draws=BOOTSTRAP_DRAWS, seed=20260828):
    """
    P(candidate's mean Brier < baseline's), resampling the SHARED station-days
    with replacement.

    Paired on the day, not on the two samples independently: both candidates
    scored the same days, and pairing is what removes day-to-day difficulty
    from the comparison.
    """
    common = sorted(set(baseline) & set(candidate))
    if len(common) < 2:
        return None, len(common)
    deltas = [candidate[d] - baseline[d] for d in common]
    rng = random.Random(seed)
    wins = 0
    for _ in range(draws):
        resampled = [deltas[rng.randrange(len(deltas))] for _ in deltas]
        if statistics.fmean(resampled) < 0:
            wins += 1
    return wins / draws, len(common)


def _flatten(per_station):
    """{(station, day): brier} across every station in one candidate."""
    return {
        (icao, day): brier
        for icao, rows in per_station.items()
        for day, brier, _ in rows
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stations", default=None,
                    help="Comma-separated ICAOs (default: every station in the registry)")
    ap.add_argument("--half-lives", default="none,14,7,5,3,2",
                    help="Comma-separated candidate half-lives in days; "
                         "'none' is the shipped no-decay baseline")
    args = ap.parse_args()

    stations = ([s.strip().upper() for s in args.stations.split(",")]
                if args.stations else list(config.STATIONS))
    candidates = [None if c.strip().lower() == "none" else float(c)
                  for c in args.half_lives.split(",")]
    if None not in candidates:
        candidates.insert(0, None)

    print(f"observed half-life sweep: {len(stations)} station(s), "
          f"{len(candidates)} candidate(s)")
    print(f"lookback {config.OBSERVATION_LOOKBACK_DAYS}d; shipped value is "
          f"{config.OBSERVED_HALF_LIFE_DAYS}; scored on multi-class Brier "
          f"(lower better), bias leave-one-out\n")

    results = sweep(stations, candidates)
    flat = {hl: _flatten(per_station) for hl, per_station in results.items()}
    baseline = flat[None]

    print(f"{'half-life':>10} {'n':>5} {'Brier':>9} {'vs none':>9} {'hit rate':>9} "
          f"{'P(better)':>10}")
    for hl in candidates:
        days = flat[hl]
        if not days:
            print(f"{str(hl):>10} {0:5}  -- no scorable station-days --")
            continue
        briers = list(days.values())
        hits = [h for per in results[hl].values() for _, _, h in per]
        mean_b = statistics.fmean(briers)
        delta = mean_b - statistics.fmean(baseline.values()) if baseline else 0.0
        if hl is None:
            p_txt = "baseline"
        else:
            p, _ = _paired_bootstrap(baseline, days)
            p_txt = "n/a" if p is None else f"{p:.3f}"
        label = "none" if hl is None else f"{hl:g}"
        print(f"{label:>10} {len(briers):5} {mean_b:9.4f} {delta:+9.4f} "
              f"{statistics.fmean(hits)*100:8.1f}% {p_txt:>10}")

    # PER STATION, because the bar requires the ordering to hold there and
    # not be carried by one station. The 2026-08-18 stop sweep looked clean
    # in aggregate and scattered per station; that is what killed it.
    print(f"\nPER-STATION mean Brier (n in brackets):")
    header = "".join(f"{('none' if hl is None else f'{hl:g}'):>10}" for hl in candidates)
    print(f"{'station':<8}{'n':>5}{header}")
    for icao in stations:
        if icao not in results[None]:
            continue
        n = len(results[None][icao])
        cells = ""
        for hl in candidates:
            rows = results[hl].get(icao)
            cells += f"{statistics.fmean([b for _, b, _ in rows]):10.4f}" if rows else f"{'--':>10}"
        print(f"{icao:<8}{n:5}{cells}")

    print(f"\nBrier only. This says nothing about P&L or about EXPOSURE -- a "
          f"better-centred\ndistribution raises model_prob, hence Kelly size, so any "
          f"gain here arrives with\nmore risk. Quantifying that needs an engine replay "
          f"per candidate (not run here).\nThe bar for moving the constant is in this "
          f"module's docstring: P >= 0.95 AND the\nordering holding per station AND the "
          f"gain not coming from reversal days.")


if __name__ == "__main__":
    main()
