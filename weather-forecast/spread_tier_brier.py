"""
spread_tier_brier.py -- which WIDTH should the probability step cut buckets
from, and does the tier that supplies it today deserve to?

READ-ONLY WITH RESPECT TO EVERY DATABASE. It reads forecasts, observations
and settled buckets through storage.py and prints. Nothing here is imported
by the live path.

THE QUESTION
------------
calibration.estimate_std_dev() tries "ensemble" FIRST and only falls to
measured_error_spread() if the ensemble fetch came back empty. Measured
2026-08-29 the fetch returns 51 members for every station on every cycle, so
the measured tier is unreachable on the live path -- it has never once been
used. Whether that is a bug depends on which width actually scores better,
and nothing had measured it.

WHY THE EXISTING EVIDENCE COULD NOT DECIDE IT
---------------------------------------------
Two tables already look like they answer this, and neither does:

  - spread_audit.py's 2026-08-20 table (model - truth by entry-price band:
    +0.105 cheap, -0.076 middle) reads as "the distribution is too WIDE".
  - the 2026-08-29 per-station table (model_prob - settled frequency on
    bought buckets: RPLL +0.31, WSSS -0.01) reads as "too NARROW at the
    stations that lose money".

Both are computed over BOUGHT buckets. A bucket is bought when
model_prob > price, so the sample is the upper tail of the model's own
error -- it reads over-confident whichever way the width is actually wrong.
That is a winner's curse, not a measurement.

This module scores every settled station-day and every listed bucket,
selected on nothing. A width that helps here helps on days the system
declined to trade as much as on days it did.

WHAT IT SCORES
--------------
Multi-class Brier, sum over listed buckets of (p_i - y_i)^2, lower better.
Chosen because it is the metric calibration.estimate_std_dev's own docstring
already used to retire the forecast-variance tier ("Brier 0.8040 against
0.7228 for a flat 1.0C"), so the numbers here sit on the same scale as the
decision that shaped the current chain.

HONESTY CONSTRAINTS
-------------------
1. LEAVE-ONE-OUT. The day being scored is excluded from both the bias
   correction and the measured spread applied to it. Without that, every
   tier is scored on a number that saw the answer, and the measured tier --
   which is a statistic OF the errors being scored -- benefits most.
2. LOOKAHEAD. Only forecasts fetched on or before the target date count,
   and only observations from strictly earlier dates.
3. UNCLAMPED GRID, CLAMPED TIERS. The grid sweeps raw widths so the floor
   and ceiling are visible as choices rather than baked in. The tier rows
   apply calibration._clamp_spread exactly as production would, so a tier's
   row is what that tier would really have produced.

WHAT IT CANNOT ANSWER YET
-------------------------
The ensemble tier's DAY-TO-DAY value was never persisted before 2026-08-29
(pipeline.ensemble_spread_for now records it). Until that history exists,
the ensemble row is scored at a single per-station constant and labelled as
such. The grid is what adjudicates the tier ORDER; whether the ensemble's
daily wobble carries information its typical value hides is a separate
question this cannot reach.
"""

import statistics
from collections import namedtuple
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import config
from bucket_axis import BucketAxis
from models import BucketProbability, CalibratedEstimate, PointForecast
from probability import bucket_probabilities

DEFAULT_AXIS = BucketAxis()

# One settled station-day, reduced to exactly what scoring needs: the centre
# the blend produced for it, the bucket it actually settled into, and the
# axis + listed bounds that day's market really had.
ScoredDay = namedtuple(
    "ScoredDay",
    "target_date center_c settled_bucket bucket_min bucket_max axis",
)


def brier_score(probs: Sequence[BucketProbability], settled_bucket: int) -> float:
    """
    Multi-class Brier: sum of (p_i - y_i)^2 over every LISTED bucket.

    Raises when the settled bucket is not among the listed ones. That is a
    scoring error, not a bad day: the tails are folded into the end buckets
    by probability.bucket_probabilities, so a settlement outside the listed
    range means the bounds passed in do not describe the market that
    settled, and silently scoring it would credit the model for mass it
    never had to place anywhere.
    """
    listed = {p.bucket_c for p in probs}
    if settled_bucket not in listed:
        raise ValueError(
            f"settled bucket {settled_bucket} is not among the listed buckets "
            f"{sorted(listed)} -- the bounds do not describe the market that settled"
        )
    return sum(
        (p.probability - (1.0 if p.bucket_c == settled_bucket else 0.0)) ** 2
        for p in probs
    )


def leave_one_out_bias(errors_by_date: Dict[date, float]) -> Dict[date, Optional[float]]:
    """
    {day: mean of the OTHER days' errors}, None where there is no other day.

    The live bias correction takes the mean of every stored error, this day
    included. Scoring with that number would let each day correct itself.
    """
    out = {}
    for d in errors_by_date:
        others = [e for k, e in errors_by_date.items() if k != d]
        out[d] = statistics.mean(others) if others else None
    return out


def leave_one_out_spread(
    errors_by_date: Dict[date, float], min_pairs: int
) -> Dict[date, Optional[float]]:
    """
    {day: stdev of the OTHER days' errors}, None where too few remain.

    Same exclusion as leave_one_out_bias, and it matters more here: the
    measured tier IS a statistic of the errors being scored, so including
    the scored day hands that tier an advantage no live cycle ever has.
    """
    out = {}
    for d in errors_by_date:
        others = [e for k, e in errors_by_date.items() if k != d]
        out[d] = statistics.stdev(others) if len(others) >= max(2, min_pairs) else None
    return out


def _probs_for(day: ScoredDay, width: float) -> List[BucketProbability]:
    estimate = CalibratedEstimate(
        station_icao="",
        target_date=day.target_date,
        central_estimate_c=day.center_c,
        std_dev_c=width,
        monsoon_phase="",
    )
    return bucket_probabilities(
        estimate, day.bucket_min, day.bucket_max, axis=day.axis
    )


def score_width(days: Sequence[ScoredDay], width: float) -> Optional[float]:
    """Mean Brier over `days` at one fixed width, or None for no days."""
    if not days:
        return None
    return statistics.mean(
        brier_score(_probs_for(d, width), d.settled_bucket) for d in days
    )


def score_per_day_widths(
    days: Sequence[ScoredDay], width_by_date: Dict[date, Optional[float]]
) -> Tuple[Optional[float], int]:
    """
    Mean Brier where each day gets its OWN width -- how a tier is really
    scored, since measured/pooled widths move day to day under leave-one-out.

    Returns (mean_brier, days_scored). Days whose width is None are skipped
    and excluded from the count, so a tier that cannot produce a number for
    a day is not credited with one.
    """
    scores = [
        brier_score(_probs_for(d, width_by_date[d.target_date]), d.settled_bucket)
        for d in days
        if width_by_date.get(d.target_date) is not None
    ]
    return (statistics.mean(scores) if scores else None), len(scores)


def sweep_widths(
    days: Sequence[ScoredDay], grid: Sequence[float]
) -> List[Tuple[float, float]]:
    """[(width, mean Brier)] across the grid. Empty when there are no days."""
    if not days:
        return []
    return [(w, score_width(days, w)) for w in grid]


def best_width(
    days: Sequence[ScoredDay], grid: Sequence[float]
) -> Tuple[Optional[float], Optional[float]]:
    """The (width, mean Brier) minimising Brier over the grid."""
    swept = sweep_widths(days, grid)
    if not swept:
        return None, None
    return min(swept, key=lambda wb: wb[1])


# --------------------------------------------------------------------------
# The database layer: stored record -> ScoredDay list
# --------------------------------------------------------------------------

def _errors_by_date(station_icao: str) -> Dict[date, float]:
    """{target_date: forecast - settled truth}, the bias correction's own sample."""
    import storage

    station = config.get_station(station_icao)
    return dict(
        storage.forecast_error_samples_dated(
            station_icao, station.resolution_grade_source
        )
    )


def build_days(station_icao: str) -> List[ScoredDay]:
    """
    Every settled station-day that also has a lookahead-clean forecast,
    reduced to what the sweep needs.

    THE CENTRE IS PRODUCTION'S, NOT A COPY OF IT. The blend arithmetic --
    the weight, the bias subtraction, the recency weighting, the rounding --
    comes from calibration.blend_central_estimate() itself, called with this
    day's forecast mean and the observations that preceded it. storage.py's
    own commentary is emphatic about why: two copies of "which forecasts
    count" drift, and a width tuned against a centre production never
    produced is not the number anybody thinks it is.

    Three differences from a live cycle, all of them narrowing:

      - The forecast term is storage.forecast_means_by_date(), which is
        forecast_error_samples()' term lifted out: same sources, same
        exclusion list, same local-day lookahead cut. A day whose only
        forecast came from an excluded source has no term and drops out,
        exactly as config.blendable_forecasts() drops it live.
      - The observed term uses STORED observations from strictly earlier
        dates only. Live also blends climate-monitor seeds, which are
        fetched fresh and never persisted, so they cannot be reconstructed
        for a past day. The observed term therefore sits on the settlement
        record alone.
      - The bias is LEAVE-ONE-OUT. Live uses the mean of every stored
        error including the day in hand; that is fine for trading a day
        that has not happened, and fatal for scoring one that has.
    """
    import calibration
    import storage

    station = config.get_station(station_icao)
    settled = storage.load_settled_buckets(station_icao)
    if not settled:
        return []

    forecast_means = storage.forecast_means_by_date(station_icao)
    loo_bias = leave_one_out_bias(_errors_by_date(station_icao))

    observed_by_date = {
        o.target_date: o
        for o in storage.load_observations_since(
            station_icao, min(settled) - timedelta(days=config.OBSERVATION_LOOKBACK_DAYS)
        )
        if o.source == station.resolution_grade_source
    }

    days = []
    for target_date, (bucket_c, bucket_min, bucket_max, unit, step) in sorted(settled.items()):
        forecast_mean = forecast_means.get(target_date)
        if forecast_mean is None:
            continue

        # STRICTLY earlier: a reading dated the target day is the answer.
        earlier = [
            o for d, o in observed_by_date.items()
            if target_date - timedelta(days=config.OBSERVATION_LOOKBACK_DAYS) <= d < target_date
        ]

        center = calibration.blend_central_estimate(
            [PointForecast(
                station_icao=station_icao, source="blended", target_date=target_date,
                max_temp_c=forecast_mean, fetched_at="",
            )],
            earlier,
            station.long_term_normal_max_c,
            forecast_bias_c=loo_bias.get(target_date) or 0.0,
            forecast_weight=config.forecast_blend_weight(station_icao),
        )

        days.append(ScoredDay(
            target_date=target_date,
            center_c=center,
            settled_bucket=bucket_c,
            bucket_min=bucket_min,
            bucket_max=bucket_max,
            # The axis that day SETTLED on, recorded per row, not today's
            # registry -- see storage.load_settled_buckets().
            axis=BucketAxis(unit=unit, step=step, edge_mode=station.bucket_edge_mode),
        ))
    return days


# --------------------------------------------------------------------------
# Per-station report
# --------------------------------------------------------------------------

# Raw widths, in degrees C. Spans well past SPREAD_FLOOR_C (0.7) and up to
# SPREAD_CEILING_C (2.0) at both ends ON PURPOSE: if the optimum sits at or
# outside a clamp, that clamp is the binding decision and the tier argument
# is downstream of it.
DEFAULT_GRID = [round(0.3 + 0.1 * i, 1) for i in range(23)]  # 0.3 .. 2.5


def station_report(
    station_icao: str,
    grid: Sequence[float] = None,
    ensemble_proxy: Optional[float] = None,
) -> dict:
    """
    One station's tier comparison. Everything here composes the tested
    primitives above; nothing new is decided in this function.

    Tier rows:
      measured  -- leave-one-out stdev of that station's own forecast
                   errors, clamped as production clamps it. This is the
                   tier the ensemble currently pre-empts.
      ensemble  -- the recorded ensemble dispersion for that day, if
                   pipeline.ensemble_spread_for() has stored one. No
                   history exists before 2026-08-29, so ensemble_proxy
                   scores the row at ONE standing width instead, flagged
                   proxy=True. That proxy cannot show what the ensemble's
                   day-to-day movement is worth -- only what its typical
                   value is worth -- and a row that cannot be scored at all
                   stays None rather than blank, so nothing reads as a tie.
      constant  -- config.POOLED_SPREAD_FALLBACK_C, the flat width the
                   chain falls to when nothing is measured. The baseline
                   estimate_std_dev's own docstring scored against.
      floor     -- config.SPREAD_FLOOR_C, because a floor that sits above
                   the optimum is doing more work than the tier order.

    pooled_error is deliberately absent. It is a cross-station statistic,
    so scoring it honestly means excluding this station from its own pool
    day by day -- disproportionate for a row that is not a contender in the
    ensemble-vs-measured question this exists to settle.
    """
    import calibration
    import storage

    grid = list(grid if grid is not None else DEFAULT_GRID)
    days = build_days(station_icao)
    report = {
        "station": station_icao,
        "n_days": len(days),
        "best_grid": (None, None),
        "measured": {"brier": None, "n": 0, "widths": {}},
        "ensemble": {"brier": None, "n": 0, "widths": {}, "proxy": False},
        "constant": {"brier": None, "n": 0, "width": None},
        "floor": {"brier": None, "n": 0, "width": None},
    }
    if not days:
        return report

    report["best_grid"] = best_width(days, grid)

    loo_widths = {
        d: (calibration._clamp_spread(w, station_icao) if w is not None else None)
        for d, w in leave_one_out_spread(
            _errors_by_date(station_icao), config.MIN_SPREAD_PAIRS
        ).items()
    }
    brier, n = score_per_day_widths(days, loo_widths)
    report["measured"] = {
        "brier": brier, "n": n,
        "widths": {d: w for d, w in loo_widths.items() if w is not None},
    }

    recorded = storage.load_ensemble_spreads(station_icao)
    if recorded:
        ens_widths = {
            d: calibration._clamp_spread(sd, station_icao) for d, (sd, _) in recorded.items()
        }
        brier, n = score_per_day_widths(days, ens_widths)
        report["ensemble"] = {"brier": brier, "n": n, "widths": ens_widths, "proxy": False}
    elif ensemble_proxy is not None:
        width = calibration._clamp_spread(ensemble_proxy, station_icao)
        report["ensemble"] = {
            "brier": score_width(days, width), "n": len(days),
            "widths": {d.target_date: width for d in days}, "proxy": True,
        }

    for key, raw in (
        ("constant", config.POOLED_SPREAD_FALLBACK_C),
        ("floor", config.SPREAD_FLOOR_C),
    ):
        width = calibration._clamp_spread(raw, station_icao)
        report[key] = {"brier": score_width(days, width), "n": len(days), "width": width}

    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _fmt(value: Optional[float], places: int = 4, width: int = 8) -> str:
    return f"{value:>{width}.{places}f}" if value is not None else f"{'--':>{width}}"


def print_report(reports: List[dict]) -> None:
    header = (
        f"{'stn':6s}{'n':>4s}{'bestW':>7s}{'bestB':>8s}"
        f"{'measW':>7s}{'measB':>8s}{'ensW':>7s}{'ensB':>8s}"
        f"{'constB':>8s}{'floorB':>8s}{'meas-ens':>9s}{'meas-best':>10s}"
    )
    print(header)
    print("-" * len(header))
    for r in reports:
        if not r["n_days"]:
            continue
        mw = r["measured"]["widths"]
        ew = r["ensemble"]["widths"]
        mean_mw = statistics.mean(mw.values()) if mw else None
        mean_ew = statistics.mean(ew.values()) if ew else None
        best_w, best_b = r["best_grid"]
        meas_b, ens_b = r["measured"]["brier"], r["ensemble"]["brier"]
        gap = meas_b - best_b if meas_b is not None and best_b is not None else None
        # NEGATIVE means the measured tier scores better than the ensemble.
        # This is the column the tier order turns on.
        vs_ens = meas_b - ens_b if meas_b is not None and ens_b is not None else None
        mark = "*" if r["ensemble"].get("proxy") else " "
        print(
            f"{r['station']:6s}{r['n_days']:4d}{_fmt(best_w, 1, 7)}{_fmt(best_b)}"
            f"{_fmt(mean_mw, 2, 7)}{_fmt(meas_b)}"
            f"{_fmt(mean_ew, 2, 6)}{mark}{_fmt(ens_b)}"
            f"{_fmt(r['constant']['brier'])}{_fmt(r['floor']['brier'])}"
            f"{_fmt(vs_ens, 4, 9)}{_fmt(gap, 4, 10)}"
        )


def main(argv=None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--station", action="append", dest="stations",
                        help="ICAO to score; repeatable. Default: every registered station.")
    parser.add_argument(
        "--ensemble-proxy", action="append", dest="proxies", metavar="ICAO=WIDTH",
        help="Score the ensemble row at ONE standing width for this station, for use "
             "until pipeline.ensemble_spread_for has built real history. Repeatable. "
             "A proxied row is marked * and says only what the ensemble's TYPICAL "
             "value is worth, never what its day-to-day movement is worth.",
    )
    args = parser.parse_args(argv)

    proxies = {}
    for item in args.proxies or []:
        icao, _, width = item.partition("=")
        proxies[icao.strip().upper()] = float(width)

    stations = args.stations or sorted(config.STATIONS)
    reports = [station_report(s, ensemble_proxy=proxies.get(s)) for s in stations]
    scored = [r for r in reports if r["n_days"]]

    print_report(reports)

    total_days = sum(r["n_days"] for r in scored)
    ens_days = sum(r["ensemble"]["n"] for r in scored)
    print()
    print(f"{len(scored)} stations, {total_days} settled station-days scored.")
    print(
        "bestW/bestB: the grid width minimising mean Brier, and that Brier. "
        "measW/measB: the measured-error tier under leave-one-out. "
        "ensW/ensB: the RECORDED ensemble dispersion."
    )
    proxied = [r["station"] for r in scored if r["ensemble"].get("proxy")]
    if proxied:
        print()
        print(
            f"* ensemble row is a ONE-WIDTH PROXY for {len(proxied)} station(s): "
            f"{', '.join(proxied)}. It scores what that ensemble's typical value is "
            f"worth, not what its day-to-day movement is worth -- the latter needs "
            f"the history pipeline.ensemble_spread_for is now collecting."
        )
        print(
            "meas-ens: NEGATIVE means the measured tier scores better than the "
            "ensemble tier that currently pre-empts it."
        )
    if ens_days == 0 and not proxied:
        print(
            "\nENSEMBLE COLUMN IS EMPTY, and that is a data gap, not a tie: the "
            "ensemble spread was discarded on every cycle before 2026-08-29. "
            "Read the tier order off measW vs bestW -- how far the measured tier "
            "sits from the width that actually scored best -- and settle the "
            "ensemble's day-to-day claim once pipeline.ensemble_spread_for has "
            "built a record."
        )


if __name__ == "__main__":
    main()
