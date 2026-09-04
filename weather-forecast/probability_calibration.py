"""
probability_calibration.py

PURPOSE
-------
Map the model's stated probability onto the rate that actually came true, so
the sizing path can bet on a calibrated number instead of correcting for a
known bias twice.

WHY IT EXISTS. entry_manager sizes with `f* = raw_edge / (1 - price)` on
`model_prob`, then applies KELLY_FRACTION (0.25) AND
entry_manager.gap_risk_haircut(). config.py already names the problem:
quarter-Kelly is the DECLARED buffer for the model's overconfidence, and the
haircut has been an UNDECLARED second one. Two stacked corrections for a bias
that can be measured directly is worse than one correction fitted to the
measurement -- and the bias is measured: mean `model_prob` 0.432 against a
0.344 realised win rate, about 9 points overconfident.

WHAT IT DOES NOT DO
-------------------
It does not touch `model_prob`. That value is what the EV table, the stored
snapshots and every stored row MEAN, and P0-1 scores against it; overwriting
it would silently redefine the record this correction was derived from. The
calibrated value travels beside it with its own provenance.

It feeds the SIZING path only. Whether the edge gate should also move onto a
calibrated probability is a separate decision with a different risk profile,
and bundling them makes the result unattributable.

PURE PYTHON, DELIBERATELY
-------------------------
numpy and scipy are not installed in the deployed venv and no module in this
repo imports either -- requirements.txt is requests, beautifulsoup4 and
py-clob-client-v2. Pool-adjacent-violators is thirty lines and fully
deterministic, so adding a numerical dependency to a trading daemon for one
function is the worse trade. `sklearn.isotonic` would have broken the deploy
rather than the tests.

OUT OF SAMPLE, PER DAY
----------------------
A map fitted on the whole record and applied retrospectively is the same leak
`calibration.estimate_std_dev(allow_measured=False)` refuses for the backtest,
and the same one `corrected_error_rmse()` already avoids for the bias
correction. `fit_for_day()` uses STRICTLY EARLIER days only.

THREE TIERS, mirroring calibration.estimate_std_dev's chain on purpose:

    station_isotonic   this station has enough prior rows of its own
    pooled_isotonic    the book does, this station does not
    uncalibrated       neither -- the map is not estimable, so say so

"Measured for this station", "measured across the book" and "not measured" are
three different claims, and a caller that cannot tell them apart will treat the
weakest as the strongest.

PREREQUISITE B IS ANSWERED HERE, CONDITIONALLY
-----------------------------------------------
`haircut_applies()` retires gap_risk_haircut on a stopless book ONLY where the
probability being sized on is actually calibrated. On such a book there is no
trigger to gap through and no exit spread to pay, so the arithmetically correct
haircut is 1.0 -- but the reason it shipped False anyway was that the
conservatism was standing in for the model's overconfidence. Correct that once,
at the probability, and the arithmetic answer becomes the right one. Fall back
to an uncalibrated probability and it does not, because then nothing has
measured the bias the second buffer was absorbing.

DEPENDENCIES
------------
bisect, datetime, typing (standard library)
config.py (local)
"""

import bisect
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

import cohort_monitor
import config

# The provenance strings, in descending order of what they claim.
STATION_TIER = "station_isotonic"
POOLED_TIER = "pooled_isotonic"
NO_TIER = "uncalibrated"

CALIBRATED_TIERS = frozenset({STATION_TIER, POOLED_TIER})


def _pava(values: Sequence[float]) -> List[float]:
    """
    Pool-adjacent-violators: the nearest non-decreasing sequence to `values`,
    in the least-squares sense.

    Walks left to right maintaining a stack of (sum, count) blocks. Whenever a
    new block's mean is below the one before it the two are merged, which is
    the only operation the algorithm has -- so the result is monotone by
    construction and O(n).

    IT IS A PROJECTION, so the total is preserved: pooling replaces a run of
    values with their own mean and never invents or destroys mass. The test
    asserting that is what catches an off-by-one in the merge.
    """
    blocks: List[List[float]] = []  # [sum, count]
    for value in values:
        blocks.append([float(value), 1.0])
        while len(blocks) > 1 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            total, count = blocks.pop()
            blocks[-1][0] += total
            blocks[-1][1] += count
    out: List[float] = []
    for total, count in blocks:
        out.extend([total / count] * int(count))
    return out


def fit_map(rows: Sequence[dict]) -> Optional[Tuple[List[float], List[float]]]:
    """
    Fit an isotonic map from `model_prob` to realised outcome.

    Returns (knot_x, knot_y) -- the distinct stated probabilities in ascending
    order, and the calibrated value at each -- or None for an empty sample.

    Rows are sorted by `model_prob` and their 0/1 outcomes projected onto the
    nearest non-decreasing sequence. Ties are averaged first so that every row
    at the same stated probability gets the same calibrated answer, which is
    what makes the map a function.

    Rows with no stored `model_prob` are dropped. NULL is the honest value on
    rows written before the column existed and on manual_trigger rows that
    bypassed the model; "no model ran" is not "the model said 0".
    """
    usable = [r for r in rows if r.get("model_prob") is not None]
    if not usable:
        return None

    usable = sorted(usable, key=lambda r: float(r["model_prob"]))
    xs = [float(r["model_prob"]) for r in usable]
    ys = [float(r["outcome"]) for r in usable]

    fitted = _pava(ys)

    # Collapse to one knot per distinct stated probability. Without this a map
    # would be multi-valued at any probability the book quoted twice, and
    # apply_map's interpolation would depend on which duplicate it landed on.
    knot_x: List[float] = []
    knot_y: List[float] = []
    index = 0
    while index < len(xs):
        end = index
        while end + 1 < len(xs) and xs[end + 1] == xs[index]:
            end += 1
        knot_x.append(xs[index])
        knot_y.append(sum(fitted[index:end + 1]) / (end + 1 - index))
        index = end + 1
    return knot_x, knot_y


def apply_map(fitted: Optional[Tuple[List[float], List[float]]], model_prob: float) -> float:
    """
    The calibrated probability for `model_prob`, by linear interpolation
    between knots.

    CLAMPED, NEVER EXTRAPOLATED. Past either end of the fitted support there is
    no measurement, and continuing the last slope would invent calibration
    where none exists -- most damagingly at the extremes, which is exactly
    where the sizing path is most sensitive. Returns the end knot instead.

    An unfitted map returns the input unchanged, so a caller that ignores the
    provenance still gets today's behaviour rather than a silent zero.
    """
    if not fitted:
        return model_prob
    knot_x, knot_y = fitted
    if model_prob <= knot_x[0]:
        return knot_y[0]
    if model_prob >= knot_x[-1]:
        return knot_y[-1]
    i = bisect.bisect_right(knot_x, model_prob)
    x0, x1 = knot_x[i - 1], knot_x[i]
    y0, y1 = knot_y[i - 1], knot_y[i]
    if x1 == x0:
        return y1
    return y0 + (y1 - y0) * (model_prob - x0) / (x1 - x0)


def fit_for_day(
    rows: Sequence[dict],
    target_day: date,
    station_icao: str,
) -> Tuple[Optional[Tuple[List[float], List[float]]], str, int]:
    """
    (map, provenance, n) for `station_icao` on `target_day`.

    STRICTLY EARLIER DAYS ONLY. A map that has seen the day it is pricing is
    scored against an answer it already knows, which flatters every station and
    flatters a drifting one most -- the leak
    calibration.estimate_std_dev(allow_measured=False) refuses for the backtest
    and corrected_error_rmse() already avoids for the bias correction.

    Tiers in order, falling through on sample size alone:

      station_isotonic  this station's own prior rows, if there are at least
                        config.MIN_CALIBRATION_SAMPLES of them
      pooled_isotonic   every station's prior rows, on the same bar
      uncalibrated      neither clears it -- (None, NO_TIER, n)

    `n` is always the sample the returned tier was fitted on, so a caller can
    report what the number rests on rather than only what it is.
    """
    minimum = config.MIN_CALIBRATION_SAMPLES
    prior = [
        r for r in rows
        if r["target_date"] < target_day and r.get("model_prob") is not None
    ]

    own = [r for r in prior if r["station_icao"] == station_icao]
    if len(own) >= minimum:
        return fit_map(own), STATION_TIER, len(own)

    if len(prior) >= minimum:
        return fit_map(prior), POOLED_TIER, len(prior)

    return None, NO_TIER, len(prior)


def haircut_applies(has_stop: bool, calibration_source: str) -> bool:
    """
    Whether entry_manager.gap_risk_haircut() should still scale this position.

    PREREQUISITE B, ANSWERED CONDITIONALLY. The plan asked whether to set
    SIZE_STOPLESS_BOOKS_ON_PURE_KELLY True. The arithmetic said yes -- the
    haircut scales a position so that a STOP-OUT costs what Kelly was sized
    against, and a book with no stop has no trigger to gap through and no exit
    spread to pay, so the correct factor is 1.0. The reason it shipped False
    anyway is that the conservatism was doing real work for a reason absent
    from the haircut's own docstring: Kelly takes the model's probability at
    face value and this model is measurably overconfident.

    So the answer is not a flag. It is: retire the haircut on a stopless book
    WHERE THE PROBABILITY IS CALIBRATED, because then the bias it was silently
    absorbing has been corrected once, at the source. Where the map is not
    estimable, keep it -- there the second buffer is still standing in for
    something nothing has measured.

    A book WITH a stop always keeps it. Calibration says nothing about gap
    risk: a stop that can be gapped through still costs more than its trigger,
    whatever the probability was.
    """
    if has_stop:
        return True
    return calibration_source not in CALIBRATED_TIERS


# ---------------------------------------------------------------------------
# The I/O half, and its cache
# ---------------------------------------------------------------------------

# {(station_icao, target_day): (map, source, n)}. compute_ev_table runs as
# often as every ten minutes in the primary window and the cohort read is two
# storage queries PER STATION, so refitting per cycle would be pure waste for a
# map that cannot change until the next day's rows settle.
#
# Keyed on the day, so the cache expires by construction rather than on a TTL:
# a new day is a new key and the old ones are simply never asked for again. The
# process restarts daily-ish and the dict is bounded by stations x days seen,
# so it is not worth evicting.
_CACHE: Dict[Tuple[str, date], Tuple[Optional[Tuple[List[float], List[float]]], str, int]] = {}


def clear_cache() -> None:
    """Drop the fitted maps. For tests, and for a long-lived process that
    wants to pick up rows written since it last fitted."""
    _CACHE.clear()


def calibration_for(station_icao: str, target_day: date):
    """
    (map, provenance, n) for this station on this day, from the stored book.

    THE COHORT COMES FROM cohort_monitor, not from a loader written here. That
    module already defines "a closed row with a settled outcome", reproduces
    the published totals to the cent, and deliberately does NOT require a
    stored model_prob -- fit_map drops the rows without one, so the two agree
    about the record and disagree about nothing.

    DEGRADES TO UNCALIBRATED ON ANY FAILURE. This runs on the entry path, and
    "uncalibrated" is precisely today's sizing behaviour, double buffer
    included -- so a storage error costs the correction, not the cycle.
    """
    key = (station_icao, target_day)
    if key in _CACHE:
        return _CACHE[key]

    try:
        rows, _ = cohort_monitor.load_cohort(until=target_day)
        result = fit_for_day(rows, target_day, station_icao)
    except Exception as exc:  # noqa: BLE001 -- must not take the entry path down
        print(
            f"[probability_calibration] could not fit a map for {station_icao} "
            f"on {target_day} ({exc}) -- sizing on the raw model_prob with the "
            f"double buffer, which is the previous behaviour."
        )
        result = (None, NO_TIER, 0)

    _CACHE[key] = result
    return result
