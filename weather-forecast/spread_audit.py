"""
spread_audit.py -- is the spread the probability step cuts buckets from
wider than the spread the trading path is entitled to?

READ-ONLY WITH RESPECT TO EVERY DATABASE. It reads forecasts and
observations from config.DB_PATH through storage.py and prints. It never
writes a forecast, an observation, a position, or a config value. Nothing
here is imported by the live path.

WHY IT EXISTS
-------------
Measured 2026-08-20 over 222 positions joined to settled METAR truth, the
model's bucket probabilities are systematically too high in the tails and
too low in the middle:

    entry band   n    market   model   truth    model - truth
    < 0.15       52   0.084    0.220   0.115         +0.105
    0.15-0.30    53   0.217    0.346   0.245         +0.101
    0.30-0.50    48   0.384    0.487   0.562         -0.076
    0.50-0.70    43   0.584    0.760   0.791         -0.031

The sign flips monotonically with price. That is the signature of ONE
number being too big -- std_dev_c, the width every bucket probability is
cut from -- and config.py's own commentary at SPREAD_FLOOR_C already
names the consequence: "an over-wide distribution underprices the modal
bucket and overprices the tails, which manufactures exactly the fake tail
edges the entry gates then have to veto."

This measures two candidate reasons the width is too big. Both were
hypotheses when this was written; the table is what decides them. IT RAN
ON 2026-08-21 AND ANSWERED BOTH NO -- see WHAT IT MEASURED below before
reading the rest of this as an open question.

  1. LEAD TIME. calibration.measured_error_spread() takes the standard
     deviation of storage.forecast_error_samples(), whose forecast term
     was, when this was written, AVG over every row fetched on or before
     the target date in UTC. That would pool a run issued five days out
     with one issued the same morning. The trading path is not working
     from that pool: entries land in a window a few hours before the
     day's peak and see only the freshest rows. If recency helps, the
     spread being applied is wider than the spread earned.

  2. THE BLEND. The quantity fed to probability.bucket_probabilities() is
     calibration.blend_central_estimate()'s output -- FORECAST_BLEND_WEIGHT
     of the forecast term plus the rest from recent observations -- but the
     spread is measured on the raw forecast mean's error. Those are
     different random variables. If the blend is the better predictor, its
     error distribution is narrower than the one being measured.

WHAT "LEAD" MEANS HERE
----------------------
Hours from a row's fetched_at to the END OF THE TARGET DATE IN THE
STATION'S LOCAL CALENDAR -- the instant the day's maximum stops being
predictable and becomes a fact. Local, not UTC, because the market's day
is local: WSSS at UTC+8 finishes 2026-08-10 at 2026-08-10T16:00Z, so a
row fetched at 18:00Z that day shares a UTC calendar date with the target
and has ALREADY SEEN IT. The existing `date(fetched_at) <= target_date`
guard admits exactly that row. Measuring to the local day end makes
lookahead the same thing as negative lead, so a non-negative cutoff
excludes it by construction rather than by a second rule.

A cutoff of C therefore means "score the forecast set that existed at
least C hours before the day was decided". Larger C = earlier = less
information. The live entry windows sit at roughly C = 8-20h depending on
the station's group, so those rows are the ones to compare against the
wide-open C = 0 row and against what calibration returns today.

WHAT IT MEASURED (first run, 2026-08-21, db snapshot 2026-08-20T15:51Z)
-----------------------------------------------------------------------
BOTH HYPOTHESES ARE DEAD, AND THE FIRST ONE IS UNTESTABLE ON THIS DATA.

1. LEAD DOES NOTHING, because there is no lead variation to have an
   effect. Every stored forecast row, at all 13 stations, sits between
   ~14h and 19.0h of lead -- p10 15.0h, median 17.0h, p90 18.8h, max 19.0h
   everywhere. For WSSS target 2026-08-19 all 84 rows were fetched in UTC
   hours 21/22/23 of the PRIOR UTC day, i.e. 05:00-07:59 local ON the
   target day: a three-hour band inside the entry window.

   So storage.forecast_error_samples()' AVG is not pooling a five-day-old
   run with a fresh one. It is averaging rows fetched within hours of each
   other, and its spread already IS the entry-time spread. The measured
   rows are flat to three decimals (WSSS 0.693 at lead>=0 vs 0.692 at
   lead>=12) and every cutoff at 20h or beyond returns n=0.

   THE SYSTEM NEVER FETCHES A DAY-AHEAD FORECAST. That is the finding
   underneath the null result: the collector only ever asks for the
   current local day. Testing whether lead matters would require
   COLLECTING day-ahead forecasts first; it is not a question this
   database can answer, and no amount of re-slicing changes that.

2. THE BLEND IS NOT NARROWER. Forecast spread vs blend spread, same
   dates: WSSS 0.693/0.660, RCSS 1.207/1.204, WMKK 0.740/0.746, RKSI
   1.156/1.200, RJTT 1.565/1.506, ZSPD 0.828/0.919. Mixed signs, a few
   percent either way. At FORECAST_BLEND_WEIGHT 0.85 the blend is mostly
   the forecast, so this is what it should look like.

Checked at the same time and also NOT supported: that the fitted normal is
the wrong SHAPE. Pooled standardised errors over 161 station-days give
excess kurtosis -0.13, indistinguishable from normal; the per-station
modal-bucket gap ranges +0.237 (RKSI) to -0.175 (ZGSZ) on n=13-19 days,
which is noise. The one weak hint is pooled P(|z|<=0.5) at 0.441 against
the normal's 0.383.

THE BINDING CONSTRAINT IS SAMPLE SIZE: 13-19 scored days per station, and
the days are cross-sectionally correlated because the same weather systems
cross several of these stations. Nothing here can resolve a 0.1C question
about a spread. Whatever explains the 2026-08-20 tail overstatement, this
database cannot currently convict the spread estimator of it.

ONE REAL DEFECT FOUND, SINCE FIXED. 72 of WSSS's 2112 forecast rows (0.36%
of 20195 across the registry, all at WSSS) had NEGATIVE lead -- fetched
after the local day ended, admitted by the UTC `date(fetched_at) <=
target_date` guard because a UTC+8 day ends at 16:00Z and those rows landed
at ~23:50Z. They had seen the day they "forecast". Dropping them moves
WSSS's raw measured spread 0.6437 -> 0.6931 and its bias -0.147 -> -0.311,
i.e. the lookahead was flattering both. Both spreads clamp to
SPREAD_FLOOR_C = 0.7 so the width did not move, but the BIAS did, and the
sign was the dangerous one (lookahead makes the model look more certain
than it is).

FIXED 2026-08-21: storage._forecast_means_in_local_day() now cuts the
sample at config.local_day_bounds_utc() instead, at both ends. WSSS was the
only station affected; the other twelve are byte-identical before and
after. The lower end of that window is also what stops the day-ahead
forecasts now being collected (see below) from being averaged into a bias
the live blend never used.

AND THE COLLECTION GAP IS NOW CLOSING. openmeteo_client keeps all three
days Open-Meteo was already returning, so lead-time data starts accruing
from 2026-08-21. Re-run this tool once ~2 weeks of it exists: the 24h/36h/
48h/72h rows, which return n=0 on every run before that date, are the ones
that finally answer hypothesis 1.

WHAT THIS CAN AND CANNOT CLAIM
------------------------------
It CANNOT claim a P&L improvement. It measures the width of an error
distribution, not what trading on a different width would have earned.
The step from "the spread is 0.3C too wide" to "therefore change
SPREAD_FLOOR_C" runs through the entry gates and the portfolio, and
belongs to a replay, not to this.

It CANNOT separate lead from sample composition on its own. A wide cutoff
drops whole dates -- any day whose only forecasts arrived late leaves the
sample entirely -- so a narrow-lead row and a wide-lead row are not always
scored over the same days. `n` is printed on every row for exactly this
reason, and --common-dates restricts every row to the dates that survive
the WIDEST cutoff, which costs sample size and buys a controlled
comparison. Read both.

It DOES avoid the lookahead that the live path gets away with. On the
trading path pipeline.gather_observations() loads every stored observation
from the first of the target month, the target date included; that is
harmless at 06:00 local when the day's METAR does not exist yet, and it is
poison in a replay, because it feeds settled truth into the estimate being
scored against settled truth. The blend variant here uses observations
STRICTLY BEFORE the target date. tests/test_spread_audit.py pins it with a
poisoning experiment.

It DIVERGES from the live blend in one other way, deliberately. The live
path also passes climate_monitor_client.load_recent_observations(), which
filters this station's static seed_observations against
config.local_today() -- TODAY, not the target date. That window cannot be
reconstructed for a past day without pretending the wall clock was
somewhere it was not, and the seeds are old enough to be outside it for
every date measured here anyway. Only stored observations are used, and
the count behind each blend is reported so a date blended from nothing is
visible rather than silently equal to its forecast.

BIAS IS NOT CORRECTED, AND THAT IS NOT AN OVERSIGHT. The standard
deviation of a distribution is unchanged by subtracting a constant, so
applying forecast_bias_c would move every printed bias to ~0 and leave
every printed spread exactly where it is. Since the bias would have to be
measured over the same window it is applied to, correcting it here would
buy a number that cannot change the answer at the price of lookahead.
"""

import argparse
import statistics
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import calibration
import config
import storage
from models import ObservedReading, PointForecast


# One scored cutoff. `n` is the number of DATES behind the row, not the
# number of forecast rows -- a thinning sample is the first thing that
# makes a spread look better than it is.
LeadRow = namedtuple(
    "LeadRow",
    "min_lead_h n forecast_spread_c forecast_bias_c blend_spread_c blend_bias_c",
)

# How many days of prior observations feed the blend's observed term.
# Matches pipeline.gather_observations()' climate-monitor window.
OBS_WINDOW_DAYS = 30


def _day_end_utc(station, target_date_iso: str) -> datetime:
    """
    The instant the station's target-date maximum is settled: local
    midnight ENDING that date, expressed in UTC.

    Delegates to config.local_day_bounds_utc(), which storage.py's error
    sample now cuts on as well -- so "lookahead" there and "negative lead"
    here are one rule with one implementation, and a fix to either cannot
    leave the other measuring a different set.
    """
    _, end = config.local_day_bounds_utc(station, date.fromisoformat(target_date_iso))
    return end


def _lead_hours(station, target_date_iso: str, fetched_at: str) -> Optional[float]:
    """
    Hours of lead a forecast row had. Negative means it was fetched after
    the day was decided -- lookahead. None means the timestamp could not
    be parsed, and an unparseable row is DROPPED rather than defaulted:
    guessing a lead is how a lookahead row rejoins the sample.
    """
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (_day_end_utc(station, target_date_iso) - fetched).total_seconds() / 3600.0


def forecast_means_at_lead(station_icao: str, min_lead_h: float) -> Dict[str, float]:
    """
    Per target date, the mean of this station's forecasts that had at
    least min_lead_h hours of lead. Dates with nothing left after the
    cutoff are ABSENT, never present with a default.
    """
    station = config.get_station(station_icao)
    buckets: Dict[str, List[float]] = {}
    for target_date_iso, fetched_at, temp_c, _source in storage.forecast_rows_with_fetch_time(
        station_icao
    ):
        lead = _lead_hours(station, target_date_iso, fetched_at)
        if lead is None or lead < min_lead_h:
            continue
        buckets.setdefault(target_date_iso, []).append(temp_c)
    return {d: statistics.fmean(v) for d, v in buckets.items()}


def _settlement_truth(station_icao: str) -> Dict[str, float]:
    """Settled maxima by target date, from this station's own settlement source."""
    station = config.get_station(station_icao)
    source = station.resolution_grade_source
    return {
        o.target_date.isoformat(): o.max_temp_c
        for o in storage.load_observations_since(station_icao, date(1900, 1, 1))
        if o.source == source and o.max_temp_c is not None
    }


def forecast_errors_at_lead(station_icao: str, min_lead_h: float) -> Dict[str, float]:
    """
    (forecast mean - settled truth) per target date, over the forecast set
    that existed min_lead_h hours before the day was decided.

    Same statistic as storage.forecast_error_samples(), restricted to a
    lead. At min_lead_h=0 the only remaining difference is that the
    lookahead cut is local rather than UTC.
    """
    truth = _settlement_truth(station_icao)
    means = forecast_means_at_lead(station_icao, min_lead_h)
    return {d: mean - truth[d] for d, mean in means.items() if d in truth}


def blend_errors_at_lead(
    station_icao: str, min_lead_h: float, obs_window_days: int = OBS_WINDOW_DAYS
) -> Dict[str, float]:
    """
    (blended central estimate - settled truth) per target date, over the
    same lead-restricted forecast set.

    The estimate goes through calibration.blend_central_estimate() itself
    rather than reimplementing the weighting -- the blend is the thing
    being scored, so a second copy of it would be scoring the wrong
    function. Observations are those STRICTLY BEFORE the target date; see
    the module docstring on why the live path's window is not reused.
    """
    station = config.get_station(station_icao)
    truth = _settlement_truth(station_icao)
    means = forecast_means_at_lead(station_icao, min_lead_h)

    out: Dict[str, float] = {}
    for target_date_iso, forecast_mean in means.items():
        if target_date_iso not in truth:
            continue
        target = date.fromisoformat(target_date_iso)
        window_start = target - timedelta(days=obs_window_days)
        prior = [
            ObservedReading(
                station_icao=station_icao,
                target_date=date.fromisoformat(d),
                max_temp_c=t,
                source=station.resolution_grade_source,
            )
            for d, t in truth.items()
            if window_start <= date.fromisoformat(d) < target
        ]
        estimate = calibration.blend_central_estimate(
            [
                PointForecast(
                    station_icao=station_icao,
                    source="lead_audit_mean",
                    target_date=target,
                    max_temp_c=forecast_mean,
                    fetched_at="",
                    raw_note="",
                )
            ],
            prior,
            station.long_term_normal_max_c,
        )
        out[target_date_iso] = estimate - truth[target_date_iso]
    return out


def _spread_and_bias(errors: Dict[str, float]) -> tuple:
    """(stdev, mean) of an error sample; stdev is None below two dates."""
    values = list(errors.values())
    if not values:
        return None, None
    spread = statistics.stdev(values) if len(values) > 1 else None
    return spread, statistics.fmean(values)


def sweep(
    station_icao: str,
    leads: List[float],
    common_dates: bool = False,
) -> List[LeadRow]:
    """
    One LeadRow per cutoff.

    common_dates restricts every row to the target dates that survive the
    WIDEST cutoff requested, so the rows differ only in which forecasts
    they averaged and not in which days they scored. It costs sample size;
    the uncontrolled view is the default because a thin controlled row can
    be worse evidence than a fat uncontrolled one.
    """
    keep = None
    if common_dates and leads:
        keep = set(forecast_errors_at_lead(station_icao, max(leads)))

    rows = []
    for lead in leads:
        f_err = forecast_errors_at_lead(station_icao, lead)
        b_err = blend_errors_at_lead(station_icao, lead)
        if keep is not None:
            f_err = {d: v for d, v in f_err.items() if d in keep}
            b_err = {d: v for d, v in b_err.items() if d in keep}
        f_spread, f_bias = _spread_and_bias(f_err)
        b_spread, b_bias = _spread_and_bias(b_err)
        rows.append(
            LeadRow(
                min_lead_h=lead,
                n=len(f_err),
                forecast_spread_c=f_spread,
                forecast_bias_c=f_bias,
                blend_spread_c=b_spread,
                blend_bias_c=b_bias,
            )
        )
    return rows


DEFAULT_LEADS = [0, 4, 8, 12, 20, 24, 36, 48, 72]


def _fmt(value: Optional[float], width: int = 7, places: int = 3) -> str:
    return f"{'--':>{width}}" if value is None else f"{value:>{width}.{places}f}"


def report(
    station_icaos: List[str],
    leads: List[float],
    common_dates: bool = False,
) -> None:
    """Print one table per station, plus what calibration returns today."""
    for icao in station_icaos:
        try:
            station = config.get_station(icao)
        except Exception as exc:  # noqa: BLE001 - unregistered station
            print(f"{icao}: not a registered station ({exc})")
            continue

        try:
            in_use, source = calibration.estimate_std_dev([], [], station_icao=icao)
        except Exception as exc:  # noqa: BLE001 - no db / too few pairs
            in_use, source = None, f"unavailable ({exc})"

        print()
        print(f"=== {icao} (UTC+{station.utc_offset_hours}) ===")
        print(
            f"spread in use today: {_fmt(in_use, 5, 2)}C ({source})   "
            f"floor {config.SPREAD_FLOOR_C}C  ceiling {config.SPREAD_CEILING_C}C  "
            f"blend weight {config.FORECAST_BLEND_WEIGHT_DEFAULT}"
        )
        if common_dates:
            print(f"restricted to dates surviving the {max(leads):g}h cutoff")
        print(
            f"{'lead>=h':>8} {'n':>4} {'fc spread':>10} {'fc bias':>9} "
            f"{'blend spread':>13} {'blend bias':>11}"
        )
        for row in sweep(icao, leads, common_dates=common_dates):
            print(
                f"{row.min_lead_h:>8g} {row.n:>4} {_fmt(row.forecast_spread_c, 10)} "
                f"{_fmt(row.forecast_bias_c, 9)} {_fmt(row.blend_spread_c, 13)} "
                f"{_fmt(row.blend_bias_c, 11)}"
            )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Measure forecast/blend error spread as a function of forecast lead. "
            "Read-only; prints and writes nothing."
        )
    )
    parser.add_argument(
        "stations",
        nargs="*",
        help="ICAOs to audit (default: every registered station)",
    )
    parser.add_argument(
        "--leads",
        type=float,
        nargs="+",
        default=DEFAULT_LEADS,
        help=f"lead cutoffs in hours (default: {' '.join(str(x) for x in DEFAULT_LEADS)})",
    )
    parser.add_argument(
        "--common-dates",
        action="store_true",
        help="score every row over only the dates that survive the widest cutoff",
    )
    args = parser.parse_args(argv)

    # config.STATIONS is keyed by ICAO -- iterating it yields the keys,
    # which is exactly the list report() wants.
    stations = args.stations or list(config.STATIONS)
    report(stations, args.leads, common_dates=args.common_dates)


if __name__ == "__main__":
    main()
