"""
bucket_bias.py -- measure a station's forecast bias against the bucket
the MARKET settled into, when its settlement-grade temperature record is not
available yet.

READ-ONLY WITH RESPECT TO EVERY DATABASE. It reads forecasts from
config.DB_PATH through storage.py, reads discovered tokens from
backtest/settings.MARKET_DATA_DB, and fetches resolved price history from
Polymarket. It never writes an observation, a position, or a price row.

THAT LAST POINT IS THE WHOLE DESIGN CONSTRAINT, not a nicety. What this
produces is a COARSER estimate of the same quantity storage.
forecast_error_samples() measures, and it must never be mistaken for the
finer one. Writing a bucket midpoint into `observations` as if it were a
reading would corrupt the station's resolution_grade_source record
permanently, silently graduate it through entry_manager's bias gate, and
leave no way to tell the two kinds of number apart afterwards. So this
prints, and nothing else.

WHY IT EXISTS
-------------
entry_manager's bias gate needs (forecast - settled truth) in degrees C.
For twelve stations the truth is next-day METAR. For VHHH it is the Hong
Kong Observatory's CLMMAXT climate extract, which publishes A WHOLE MONTH
AT A TIME, weeks in arrears -- confirmed 2026-08-20 that all 19 elapsed
days of August returned no rows. VHHH's observations therefore stop at
2026-07-31 while its stored forecasts start 2026-08-06, the two ranges do
not overlap by a single day, and forecast_error_samples() returns an empty
list. Not a small sample: nothing at all. That is what
config.BIAS_GATE_OVERRIDE_STATIONS was opened for on 2026-08-20, and this
is how the number it bypassed gets measured before mid-September.

THE MARKET SETTLES INTO A BUCKET, AND A BUCKET IS A MEASUREMENT
---------------------------------------------------------------
Once one of an event's buckets resolves, its YES token pays 1.0 and every
other bucket pays 0.0. That is a temperature reading quantized to the
bucket width -- 1 degree C here -- and it is available the day after the
market settles rather than six weeks later.

Quantization costs less than it looks like it should. Replacing the exact
reading with the bucket's midpoint adds an error that is uniform over the
bucket's width, sd = 1/sqrt(12) = 0.289 C for 1 C buckets, INDEPENDENT of
the forecast error it is added to. Over n days it contributes
0.289/sqrt(n) to the standard error -- about 0.06 C at n = 26, against the
0.50 C that config.MAX_BIAS_STANDARD_ERROR_C asks for and against measured
per-station error spreads of 0.66 to 2.16 C. It moves the third decimal.

WHAT IT COSTS THAT IS NOT NEGLIGIBLE, AND IS REPORTED RATHER THAN BURIED:

  1. THE EDGE BUCKETS ARE CENSORED, NOT QUANTIZED. Polymarket's lowest and
     highest buckets are catch-alls ("27 or below", "37 or above"), so a
     settlement there says only that the day fell in a half-open interval
     with no midpoint to take. Those dates are DROPPED, and counted in the
     report. Dropping them biases the sample toward ordinary days, which
     matters more the more often the station settles in a tail.

  2. IT IS A DIFFERENT ESTIMATOR FROM THE GATE'S. The gate reads
     forecast_error_samples(); this reads settled buckets. They will not
     agree to the last decimal even on a station that has both, which is
     why --compare exists: run it on a station whose METAR record IS
     current and the two numbers are printed side by side, so the
     approximation is checked rather than asserted.

  3. IT INHERITS THE MARKET'S RESOLUTION, NOT THE OBSERVATORY'S. If
     Polymarket ever settles a bucket against a reading the official
     record later contradicts, this measures the market's answer. For
     scoring trades that is arguably the right authority -- it is what
     paid -- but it is not the same claim as "the forecast was N degrees
     off what the weather did."

WHAT A SETTLED BUCKET HAS TO LOOK LIKE
--------------------------------------
Exactly one bucket at or above WINNER_MIN_PRICE and every other bucket at
or below LOSER_MAX_PRICE. Anything else -- two winners, no winner, a
bucket stuck mid-book, a token whose history the API will not return --
makes the date UNRESOLVED and it is skipped and counted, never guessed at.
This is the same "never guess between 1.0 and 0.0" rule
position_manager._close_resolved_without_price() holds to, for the same
reason: a fabricated settlement goes into the number silently and there is
no way to find it again afterwards.

BOUNDS COME FROM THE EVENT, NEVER FROM config.STATIONS. Whether a bucket
is an edge catch-all depends on the bounds, and config's are documented as
a seasonal cross-check that drifts -- measured 2026-08-14, ten of thirteen
stations had drifted, two of them by 5 C. Using them here would classify
interior buckets as censored and censored ones as interior, on exactly the
days the answer matters. The discovered token map for each date carries the
real bounds, so this derives them per date from market_tokens.

DEPENDENCIES
------------
argparse, datetime, statistics, sqlite3 (standard library)
config.py, storage.py, calibration.py (local)
backtest/price_history_client.py, backtest/settings.py (local)
"""

import argparse
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import calibration
import config
import storage
from backtest import price_history_client, settings as bt_settings

# A resolved YES token pays 1.0 and the losers pay 0.0, but the book is
# quoted in ticks and the observed terminal values are 0.999/0.001 rather
# than exactly 1/0. These thresholds are deliberately far from both the
# tick values AND from anything a live pre-settlement book produces: a
# genuine 0.90 favourite on an unsettled market must not be read as a
# settlement, which is what the LOSER check below enforces jointly.
WINNER_MIN_PRICE = 0.90
LOSER_MAX_PRICE = 0.10

# How long after the target date to look for the settling tick. Resolution
# has been observed landing ~02:00 UTC on target_date + 2 (VHHH 2026-08-15
# settled at 2026-08-17T02:00Z). Three days covers that with room for a
# slow settlement without reaching so far forward that a NEXT event's
# prices could be confused for this one's -- token ids are per event and
# per date, so they cannot be, but the window stays honest anyway.
SETTLEMENT_LOOKAHEAD_DAYS = 3

# Requested granularity for the history fetch, in minutes. Resolved markets
# have been observed returning >= 12h ticks regardless of what is asked for
# (see price_history_client's docstring). That is fine here and nowhere
# near fine for a replay: this reads ONLY the terminal value, which is flat
# from settlement onward, so coarse sampling loses nothing it needs.
FETCH_FIDELITY_MIN = 60

# Source string written into storage.settled_buckets.source. Names the
# AUTHORITY, not the method: this is what the exchange paid out on, which
# is a different claim from what the Observatory later publishes, and a
# reader comparing the two must be able to tell which is which.
SETTLEMENT_SOURCE = "market_settlement"

# How far back an ingest sweep looks for station-days it has no settlement
# for. Unlike HKO_INGEST_LOOKBACK_DAYS this does NOT have to outrun a
# publication lag -- settlements are readable the day after -- so it only
# has to cover a daemon outage. Two weeks is generous for that and keeps a
# cold-start sweep to a few hundred requests.
INGEST_LOOKBACK_DAYS = 14

# Most settlements this sweep will fetch per station, per day.
#
# THIS EXISTS BECAUSE THE INGEST RUNS INSIDE A TRADING CYCLE. Establishing
# one settlement costs 11 requests -- one per bucket -- at a 0.25s floor, so
# roughly 3 seconds. Steady state that is one day per station and nobody
# notices. A COLD START is the problem: 13 stations x 14 missing days is
# ~2000 requests and about nine minutes of a cycle that runs every fifteen,
# measured 2026-08-20. An auxiliary ingest that can stall exits for nine
# minutes is not auxiliary.
#
# Capped, the backlog drains over several days instead of one, and the daily
# throttle above means the drain costs one bounded pass per station per day.
# What is skipped is LOGGED rather than silently dropped -- a coverage cap
# nobody is told about reads as complete coverage.
MAX_NEW_SETTLEMENTS_PER_SWEEP = 3

# Per-station throttle, same shape and reasoning as clients/metar_client.py's
# and hko.py's: one sweep per station per local day is enough, because a
# settlement is immutable once written.
_last_ingest_by_station = {}

# sd of a uniform distribution over one bucket width, in bucket widths.
# 1/sqrt(12). The quantization penalty reported below is this times the
# bucket width, divided by sqrt(n).
_UNIFORM_SD_IN_WIDTHS = 0.2886751345948129


class UnresolvedDate(Exception):
    """One target date whose settled bucket could not be established."""


def _market_db() -> sqlite3.Connection:
    conn = sqlite3.connect(bt_settings.MARKET_DATA_DB)
    conn.row_factory = sqlite3.Row
    return conn


def yes_tokens_for_date(
    station_icao: str, target_date: date, conn: sqlite3.Connection
) -> Dict[int, str]:
    """
    {bucket_c: token_id} for this station-day's YES side, from the token map
    discovery actually recorded. Empty if the day was never discovered.
    """
    rows = conn.execute(
        "SELECT bucket_c, token_id FROM market_tokens "
        "WHERE station_icao = ? AND target_date = ? AND lower(side) = 'yes'",
        (station_icao, target_date.isoformat()),
    ).fetchall()
    return {int(r["bucket_c"]): r["token_id"] for r in rows}


def terminal_price(token_id: str, target_date: date) -> Optional[float]:
    """
    The last price this token traded at in the settlement window, or None
    if the endpoint returned nothing for it.

    None is NOT zero. A token the API has no history for is a token whose
    outcome is unknown, and the caller treats the whole date as unresolved
    rather than quietly counting the silence as a loss -- which would make
    every bucket a loser and every date look like it had no winner.
    """
    start = int(datetime.combine(target_date, time.min, tzinfo=timezone.utc).timestamp())
    end = int(
        datetime.combine(
            target_date + timedelta(days=SETTLEMENT_LOOKAHEAD_DAYS),
            time.min,
            tzinfo=timezone.utc,
        ).timestamp()
    )
    points = price_history_client.fetch_history(token_id, start, end, FETCH_FIDELITY_MIN)
    if not points:
        return None
    return float(max(points, key=lambda p: p[0])[1])


def settled_bucket(
    station_icao: str,
    target_date: date,
    conn: sqlite3.Connection,
    price_fn=terminal_price,
) -> Tuple[int, Tuple[int, int]]:
    """
    (winning_bucket, (bucket_min, bucket_max)) for one station-day.

    Raises UnresolvedDate -- never returns a guess -- when the day was
    never discovered, when any token's history is missing, or when the
    terminal prices do not describe exactly one winner and all losers.

    The all-losers half of that test is what stops an unsettled market
    being read as a settled one: a 0.93 favourite the day before
    resolution has other buckets well above LOSER_MAX_PRICE, so it fails
    here rather than contributing a fictional settlement.
    """
    tokens = yes_tokens_for_date(station_icao, target_date, conn)
    if not tokens:
        raise UnresolvedDate("no discovered token map for this date")

    prices: Dict[int, float] = {}
    for bucket_c, token_id in sorted(tokens.items()):
        price = price_fn(token_id, target_date)
        if price is None:
            raise UnresolvedDate(f"no price history for bucket {bucket_c}")
        prices[bucket_c] = price

    winners = [b for b, p in prices.items() if p >= WINNER_MIN_PRICE]
    losers = [b for b, p in prices.items() if p <= LOSER_MAX_PRICE]
    if len(winners) != 1 or len(losers) != len(prices) - 1:
        shown = ", ".join(f"{b}:{prices[b]:.3f}" for b in sorted(prices))
        raise UnresolvedDate(f"not exactly one settled bucket ({shown})")

    return winners[0], (min(prices), max(prices))


def bucket_midpoint_c(
    bucket_c: int, bounds: Tuple[int, int], edge_mode: str
) -> Optional[float]:
    """
    The midpoint temperature of a settled bucket, or None if the bucket is
    one of the event's CENSORED edge catch-alls.

    Interval semantics mirror backtest.resolution.bucket_for_temp exactly,
    because a midpoint taken under the other convention is off by half a
    bucket -- half the effect being measured:

      "floor"   bucket b covers [b, b + 1)      -> midpoint b + 0.5
      "half_up" bucket b covers [b - 0.5, b + 0.5) -> midpoint b

    None for the edges is the honest answer, not a limitation to work
    around. "37 or above" has no midpoint; substituting 37.5 would invent a
    ceiling the market does not have, and on a genuinely hot day that
    invention runs one direction only.
    """
    bucket_min, bucket_max = bounds
    if bucket_c <= bucket_min or bucket_c >= bucket_max:
        return None
    return bucket_c + 0.5 if edge_mode == "floor" else float(bucket_c)


def bucket_bias_samples(
    station_icao: str,
    dates: Optional[List[date]] = None,
) -> Tuple[List[float], List[Tuple[date, str]], List[Tuple[date, int, float, float]]]:
    """
    (errors, skipped, detail) for one station, FROM STORAGE ONLY -- no
    network, no market-data database, cheap enough to call every cycle.

    THE SPLIT IS THE POINT. ingest_settled_buckets() does the fetching and
    the settlement judgement once per day and writes the bucket; everything
    downstream reads that row. A bias measurement that had to make 165 HTTP
    requests could not run on the live path at all, and a second
    implementation computing it a slightly different way is exactly the
    drift storage.forecast_means_by_date()' docstring argues against.

    errors are (forecast_mean - settled midpoint) in degrees C: the SAME
    signed quantity, the same order of subtraction, and the same forecast
    term as storage.forecast_error_samples(), so calibration.bias_stats()
    reduces them without knowing which truth they were measured against.
    Positive means the forecasts ran WARM of what settled.

    skipped carries (date, reason) for every date that produced no sample,
    so a report can say what it did not measure instead of only what it did.
    """
    station = config.get_station(station_icao)
    edge_mode = getattr(station, "bucket_edge_mode", "half_up")
    forecast_means = storage.forecast_means_by_date(station_icao)
    settled = storage.load_settled_buckets(station_icao)

    candidates = sorted(dates if dates is not None else set(forecast_means) | set(settled))

    errors = []
    skipped = []
    detail = []

    for target_date in candidates:
        forecast_mean = forecast_means.get(target_date)
        if forecast_mean is None:
            skipped.append((target_date, "no blendable forecast stored for this date"))
            continue
        record = settled.get(target_date)
        if record is None:
            skipped.append((target_date, "no settled bucket recorded yet"))
            continue

        bucket_c, bucket_min, bucket_max = record
        midpoint = bucket_midpoint_c(bucket_c, (bucket_min, bucket_max), edge_mode)
        if midpoint is None:
            skipped.append((
                target_date,
                f"settled in edge bucket {bucket_c} of {bucket_min}..{bucket_max} "
                f"-- censored, no midpoint",
            ))
            continue

        errors.append(forecast_mean - midpoint)
        detail.append((target_date, bucket_c, midpoint, forecast_mean))

    return errors, skipped, detail


def derived_bias_stats(station_icao: str) -> tuple:
    """
    This station's forecast bias as (bias_c, n, stderr_c), measured against
    recorded market settlements instead of against a settlement-grade
    temperature record.

    Returns (None, 0, None) when nothing has been recorded yet -- what
    calibration.bias_stats() already produces for an empty sample, and what
    every caller downstream reads as "unmeasured, therefore not graduated".

    THE STATISTIC IS calibration.bias_stats(), NOT A LOCAL COPY. That
    function is documented as shared on purpose so the live path and the
    backtest cannot compute it differently; this is the third caller and
    must not become the third implementation.
    """
    errors, _, detail = bucket_bias_samples(station_icao)
    if not errors:
        return calibration.bias_stats(errors)
    # Same recency weighting as the preferred path in
    # entry_manager.forecast_bias_stats. The two estimators already differ
    # in their TRUTH (a reading versus a settled bucket); letting them also
    # differ in how they age samples would make the fallback incomparable
    # to the thing it stands in for.
    dated = [(row[0], err) for row, err in zip(detail, errors)]
    return calibration.bias_stats_weighted(
        dated, config.local_today(config.get_station(station_icao)),
        config.BIAS_HALF_LIFE_DAYS)


def ingest_settled_buckets(
    station_icaos,
    market_conn=None,
    price_fn=None,
    max_lookback_days: int = INGEST_LOOKBACK_DAYS,
    max_per_sweep: int = MAX_NEW_SETTLEMENTS_PER_SWEEP,
) -> int:
    """
    Record the settled bucket for any recent station-day that does not have
    one yet. Returns how many rows were written.

    Shaped like clients/official/hko.py's ingest_missing_recent() because it
    has the same job and the same hazards: it self-throttles to once per
    station per local day, it treats an unsettled day as an ordinary gap
    rather than an error, and it never raises into a trading cycle. A market
    that has not resolved yet is not a failure -- it is tomorrow's row.

    TODAY IS NEVER INGESTED. The local day is not over, and a market that is
    merely CERTAIN late in the day is indistinguishable from one that has
    SETTLED, by price alone -- one bucket at 0.999 and the rest at 0.001
    looks identical either way. Waiting a day costs nothing and removes the
    only case where the settlement test can be fooled by a live book.
    """
    if price_fn is None:
        price_fn = terminal_price

    owns_conn = market_conn is None
    saved = 0
    try:
        if owns_conn:
            market_conn = _market_db()
    except Exception as exc:  # noqa: BLE001 - the store may not exist yet
        print(f"[bucket_bias] market-data store unavailable, no settlements ingested: {exc}")
        return 0

    try:
        for icao in station_icaos:
            try:
                station = config.get_station(icao)
                today = config.local_today(station)
                if _last_ingest_by_station.get(icao) == today:
                    continue
                _last_ingest_by_station[icao] = today

                have = storage.settled_bucket_dates(icao)
                wanted = [today - timedelta(days=i) for i in range(1, max_lookback_days + 1)]
                missing = [d for d in wanted if d not in have]

                # Newest first: a bounded sweep should spend its budget on
                # the days most likely to have settled and most likely to
                # matter, not on the far end of the lookback.
                unresolved = 0
                recorded = 0
                deferred = 0
                for target_date in sorted(missing, reverse=True):
                    if recorded >= max_per_sweep:
                        deferred = len(missing) - unresolved - recorded
                        break
                    try:
                        bucket_c, bounds = settled_bucket(
                            icao, target_date, market_conn, price_fn)
                    except UnresolvedDate:
                        unresolved += 1
                        continue
                    storage.save_settled_bucket(
                        icao, target_date, bucket_c, bounds[0], bounds[1],
                        source=SETTLEMENT_SOURCE)
                    recorded += 1
                    saved += 1

                if unresolved:
                    print(
                        f"[bucket_bias] {icao}: {unresolved} day(s) in the last "
                        f"{max_lookback_days} have no readable settlement yet -- not saving."
                    )
                if deferred > 0:
                    print(
                        f"[bucket_bias] {icao}: capped at {max_per_sweep} settlement(s) this "
                        f"sweep, {deferred} still missing -- they are NOT covered by the bias "
                        f"below until a later sweep picks them up."
                    )
            except Exception as exc:  # noqa: BLE001 - one station must not stop the rest
                print(f"[bucket_bias] settlement ingest failed for {icao} (continuing): {exc}")
    finally:
        if owns_conn and market_conn is not None:
            market_conn.close()
    return saved


def quantization_stderr_c(n: int, bucket_width_c: float = 1.0) -> Optional[float]:
    """
    The standard error this method adds purely by using bucket midpoints
    instead of readings, over n samples. Independent of the forecast error,
    so it adds in quadrature rather than linearly -- reported separately so
    the reader can see how much of the total precision is method noise.
    """
    if n < 1:
        return None
    return (_UNIFORM_SD_IN_WIDTHS * bucket_width_c) / (n ** 0.5)


def print_report(
    station_icao: str,
    detail: bool = True,
    compare: bool = False,
) -> None:
    station = config.get_station(station_icao)
    source = station.resolution_grade_source
    errors, skipped, rows = bucket_bias_samples(station_icao)
    bias, n, stderr = calibration.bias_stats(errors)

    print(f"\n=== {station_icao} ({station.display_name}) forecast bias from SETTLED BUCKETS ===")
    print(f"    settlement-grade source for this station: {source}")
    print(f"    bucket edge mode: {getattr(station, 'bucket_edge_mode', 'half_up')}\n")

    if detail and rows:
        print(f"    {'date':<12}{'bucket':>7}{'midpoint':>10}{'forecast':>10}{'error':>9}")
        for target_date, bucket_c, midpoint, forecast_mean in rows:
            print(f"    {target_date.isoformat():<12}{bucket_c:>7}{midpoint:>10.1f}"
                  f"{forecast_mean:>10.2f}{forecast_mean - midpoint:>+9.2f}")
        print()

    if n == 0:
        print("    NO SAMPLES. Nothing was measured.")
    else:
        q = quantization_stderr_c(n)
        print(f"    bias      {bias:+.3f} C over n={n}  "
              f"({'forecasts run WARM' if bias > 0 else 'forecasts run COOL'} of settlement)")
        if stderr is None:
            print("    stderr    unknown -- one sample has a mean but no measurable precision")
        else:
            print(f"    stderr    {stderr:.3f} C   "
                  f"(gate wants <= {config.MAX_BIAS_STANDARD_ERROR_C:.2f} C)")
            print(f"              of which {q:.3f} C is bucket quantization, the rest is real "
                  f"day-to-day spread")
        print(f"    pairs     {n} (gate wants >= {config.MIN_BIAS_PAIRS_BEFORE_ENTRY})")

    if skipped:
        print(f"\n    {len(skipped)} date(s) produced no sample:")
        for target_date, reason in skipped:
            print(f"      {target_date.isoformat()}  {reason}")

    if compare:
        official = storage.forecast_error_samples(station_icao, source)
        o_bias, o_n, o_stderr = calibration.bias_stats(official)
        print(f"\n    --- against the gate's own estimator ({source}) ---")
        if o_n == 0:
            print("    n=0. The settlement-grade record has no date in common with the "
                  "stored forecasts, which is the whole reason this script exists.")
        else:
            shown = "n/a" if o_stderr is None else f"{o_stderr:.3f}"
            print(f"    settlement-grade  bias {o_bias:+.3f} C  n={o_n}  stderr {shown}")
            if n:
                print(f"    settled buckets   bias {bias:+.3f} C  n={n}  "
                      f"stderr {'n/a' if stderr is None else f'{stderr:.3f}'}")
                print(f"    difference        {bias - o_bias:+.3f} C")
                print("    These are different estimators over possibly different date sets. "
                      "Read the difference as a check on the approximation, not as an error bar.")

    print("\n    NO OBSERVATION WAS WRITTEN. --ingest records settled BUCKETS in their own")
    print("    table; the midpoint above is derived at read time and is never stored as a")
    print("    settlement-grade temperature.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Measure a station's forecast bias against the bucket the market settled "
                    "into, for stations whose settlement-grade temperature record lags.")
    parser.add_argument("--station", default="VHHH",
                        help="ICAO to measure (default: VHHH, the station this exists for).")
    parser.add_argument("--ingest", action="store_true",
                        help="Fetch and record any missing settlements first. Without this the "
                             "report reads only what the daemon has already stored.")
    parser.add_argument("--max-per-sweep", type=int, default=MAX_NEW_SETTLEMENTS_PER_SWEEP,
                        help=f"Settlements to fetch per station with --ingest (default "
                             f"{MAX_NEW_SETTLEMENTS_PER_SWEEP}, which is sized for running "
                             f"inside a trading cycle). Raise it to drain a cold start by hand.")
    parser.add_argument("--lookback", type=int, default=INGEST_LOOKBACK_DAYS,
                        help=f"Days back to sweep with --ingest (default {INGEST_LOOKBACK_DAYS}). "
                             f"Raise it once, for a cold start.")
    parser.add_argument("--no-detail", dest="detail", action="store_false",
                        help="Summary only -- skip the per-date table.")
    parser.add_argument("--compare", action="store_true",
                        help="Also print the gate's own settlement-grade estimate, to check "
                             "this approximation against it. Meaningful only on a station "
                             "whose settlement-grade record is actually current.")
    args = parser.parse_args()

    if args.ingest:
        written = ingest_settled_buckets([args.station], max_lookback_days=args.lookback,
                                         max_per_sweep=args.max_per_sweep)
        print(f"[bucket_bias] recorded {written} new settlement(s) for {args.station}.")

    print_report(args.station, detail=args.detail, compare=args.compare)
