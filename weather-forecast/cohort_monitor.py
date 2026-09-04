"""
cohort_monitor.py

PURPOSE
-------
Re-score the whole closed book on the hold-vs-actual basis, on a rolling
window, and report the one number that would notice this book's edge
disappearing.

WHY IT EXISTS, in one paragraph. config.py's measurement block establishes
that the P&L is a PRICE edge and not a forecasting one: mean entry 0.306
against a 0.344 realised win rate, while the model LOSES to the market on
Brier (0.1930 against 0.1842) and carries about 9 points of overconfidence.
That is why a month of calibration work moved Brier and did not move P&L.
It also means the thing the book actually lives on can decay without any
calibration metric twitching -- so it needs its own instrument, scored the
same way the money is scored, and its own pre-committed threshold. Both of
those are here; the threshold's three constants live in config.py next to
the measurement they are read against.

WHAT IS MEASURED
----------------
Every CLOSED position whose target date has a settled bucket, scored five
ways at the same entry price:

    as_traded   what the book actually booked
    stop_only   take-profit exits replaced by settlement
    take_only   stop exits replaced by settlement
    neither     both price exits replaced by settlement
    held        EVERY row replaced by settlement, including resolution
                closes

held AND neither ARE DIFFERENT QUANTITIES, and the difference is the point.
"neither" leaves resolution-closed rows exactly as the book booked them;
"held" re-values those too. config.py records the pair as +$743.68 and
+$765.33 -- $21.65 apart -- and separately names a $43.74 residual it
cannot explain. Both are the SAME third term, and this module measures it
instead of hypothesising it:

    held - as_traded  ==  stop_cost + take_cost + other_gap        (exact)
    other_gap         ==  held - neither                           (exact)

where each cost is summed over the rows in that exit class only. The
identity closes to the cent by construction, so `other_gap` is a
measurement of how far resolution-closed rows sat from clean settlement
value -- exit fees, and closing at the book quote rather than the
settlement reading (the defect P1-7 addresses). config.py's $43.74 is that
term double-counted: it compares a per-rule sum that already excludes
resolution rows against a total that includes them.

THE DECAY ALARM IS THE PRICE-EDGE LINE, NOT BRIER
-------------------------------------------------
`price_edge()` reports realised win rate minus mean entry price, in
probability points per share, with the entry-side taker fee charged against
it. That is the quantity `config.COHORT_KILL_NET_PRICE_EDGE` is keyed to.
Brier is deliberately absent from this module: promotion_dossier already
computes it, it already says the model is worse than the market, and a good
Brier here would be the single most misleading number this page could
print.

WHY THE BOOTSTRAP CLUSTERS ON STATION-DAYS
------------------------------------------
Every entry taken on one station-day settles off the same weather, so rows
are not independent draws. The measured cohort is 514 rows over 252
station-days -- a factor of two. Resampling rows would report an interval
about sqrt(2) too narrow; resampling station-days is what makes it honest.
The original measurement did this and so does this module, so the two are
comparable.

WHAT THIS MODULE MAY NOT DO
---------------------------
Phase 0: measurement only, zero behaviour change. It is importable with no
import-time side effects, it reads and never writes, and the kill criterion
returns a verdict that NOTHING on a trading path consumes. There is no
`halt()` here and there must not be one -- see config.COHORT_KILL_* for why
the response is an operator decision rather than a constant.

DEPENDENCIES
------------
random, statistics, datetime, typing (standard library)
config.py, ev_engine.py, models.py, storage.py, backtest/resolution.py
"""

import argparse
import random
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import config
import ev_engine
import storage
from backtest import resolution
from models import Position

# The five scenarios, in the order they are reported. "held" sits last
# because it is the baseline the other four are read against, not because it
# matters least -- config.py's $1,038.82 is measured against it.
SCENARIOS: Tuple[str, ...] = ("as_traded", "stop_only", "take_only", "neither", "held")

# EXIT CLASSES, keyed on the status strings models.Position documents as the
# exact set this codebase writes. closed_trailing_stop is a STOP: the
# trailing stop was removed 2026-08-17, but its closed rows are still inside
# the measured window, and filing them anywhere else would move real stop
# cost into the residual this module exists to name.
STOP_STATUSES = frozenset({"closed_stop_loss", "closed_trailing_stop"})
TAKE_STATUSES = frozenset({"closed_take_profit"})

# WHICH ROWS EACH SCENARIO RE-VALUES AT SETTLEMENT. Stated as data rather
# than as four branches so a new exit class cannot be silently handled
# differently by one scenario than by another.
_REVALUED_CLASSES: Dict[str, frozenset] = {
    "as_traded": frozenset(),
    "stop_only": frozenset({"take"}),
    "take_only": frozenset({"stop"}),
    "neither": frozenset({"stop", "take"}),
    "held": frozenset({"stop", "take", "other"}),
}

# THE PUBLISHED MEASUREMENT, from config.py's block. ONE copy, and
# tests/test_cohort_monitor.py checks it against that block -- a second
# transcription is how a monitor ends up faithfully reproducing a typo.
#
# "held" appeared only in tests/test_hold_to_settlement_modes.py until
# 2026-09-03; it is the baseline the +$1,038.82 headline is computed
# against, and its absence from config.py is what made that headline look
# unreconcilable for a day.
PUBLISHED_TOTALS_USD: Dict[str, float] = {
    "as_traded": -295.15,
    "stop_only": 186.81,
    "take_only": 283.37,
    "neither": 765.33,
    "held": 743.68,
}
PUBLISHED_STAKED_USD = 4049.93
PUBLISHED_N = 514
PUBLISHED_N_DAYS = 252
PUBLISHED_WINDOW = (date(2026, 8, 3), date(2026, 9, 1))

# "To the cent", as the acceptance criterion words it. Half a cent, so a
# figure that rounds to the published one passes and one that does not,
# fails.
CENT = 0.005

# Bootstrap settings. Seeded, because an interval that moves when you re-run
# it is an interval an operator learns to ignore.
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20260903
CI_ALPHA = 0.05

# Trailing windows reported alongside all-time. config.COHORT_KILL_WINDOW_DAYS
# must be one of these or kill_criterion() has nothing to read.
#
# 14 IS HERE BECAUSE THE OTHER TWO CANNOT DISCRIMINATE YET. Measured
# 2026-09-04: the entire closed book runs 2026-08-06..09-03, so all-time,
# trailing-60 and trailing-30 are the SAME 572 rows and print the same
# number. The 30-day window only starts to mean anything after ~09-06 and
# the 60-day one not until October. 14 is calibration_panel's RECENT_DAYS,
# picked there for this same reason, and it is the only window on this list
# that can currently show a trend rather than a level.
#
# The kill criterion is still keyed to 30 (config.COHORT_KILL_WINDOW_DAYS),
# deliberately: 14 days is a thin enough sample that it will cross zero on
# noise, and a threshold that trips on noise gets ignored.
WINDOW_DAYS: Tuple[int, ...] = (14, 30, 60)

# How many closed rows to pull per station. HISTORY_LIMIT in
# promotion_dossier is 500 for a per-station dossier; the cohort is the
# whole book across every station and has to reach back past the start of
# the published window, so this is deliberately generous.
HISTORY_LIMIT = 5000


def exit_class(status: str) -> str:
    """
    "stop", "take" or "other" for a closed position's status.

    "other" is overwhelmingly closed_resolution, and is NOT a residual
    bucket in the pejorative sense -- it is the class whose gap against
    clean settlement value this module reports as `other_gap`.
    """
    if status in STOP_STATUSES:
        return "stop"
    if status in TAKE_STATUSES:
        return "take"
    return "other"


def cohort_rows(
    positions: Sequence[Position],
    settled: Dict[date, Tuple[int, int, int, str, int]],
    since: Optional[date] = None,
    until: Optional[date] = None,
) -> Tuple[List[dict], Dict[str, int]]:
    """
    ([row, ...], {reason: n_skipped}) over positions held IN MEMORY.

    Pure -- no I/O of any kind, for the same reason
    promotion_dossier.score_entries is separated from scorable_entries: the
    arithmetic that decides whether the book still has an edge has to be
    testable without a database, and there must be exactly one copy of it.

    A ROW IS IN THE COHORT WHEN IT IS CLOSED AT A PRICE AND ITS DAY HAS A
    SETTLED BUCKET. Note what is NOT required: a stored `model_prob`.
    promotion_dossier needs one because Brier does; this does not, and
    demanding one would drop 156 of the 514 measured rows -- real money,
    entered before the column existed -- out of a P&L measurement. Every
    absence is counted rather than dropped silently.

    THE WINNING BUCKET IS READ, NOT DERIVED, exactly as score_entries reads
    it: settled_buckets stores the bucket the market settled into together
    with that day's live-derived bounds, so no rounding rule is re-applied
    and no day is scored against a bucket map it did not trade under.
    """
    rows: List[dict] = []
    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for position in positions:
        if since is not None and position.target_date < since:
            skip("outside window")
            continue
        if until is not None and position.target_date > until:
            skip("outside window")
            continue
        if not position.status.startswith("closed"):
            skip("still open")
            continue
        if position.exit_price is None:
            skip("closed with no exit price")
            continue
        if not position.entry_price or position.entry_price <= 0:
            skip("no usable entry price")
            continue
        if not position.size_usd:
            skip("no stake recorded")
            continue

        settlement = settled.get(position.target_date)
        if settlement is None:
            skip("target date not settled yet")
            continue
        winning_bucket = settlement[0]

        try:
            outcome = resolution.resolution_exit_price(
                position.side, position.bucket_c, winning_bucket
            )
        except ValueError:
            # One corrupt row must not take down the whole monitor, the same
            # way an unreadable entry is an UNSCORED entry in
            # promotion_dossier rather than an exception.
            skip("unrecognised side")
            continue

        # `size_usd / entry_price` is storage.position_economics'
        # `notional_shares`: what the stake bought at the entry ask. A UNIT
        # OF ACCOUNT, true for every execution mode -- deliberately not
        # `size_shares`, which is NULL for paper and is a real holding.
        shares = position.size_usd / position.entry_price

        rows.append({
            "station_icao": position.station_icao,
            "target_date": position.target_date,
            "bucket_c": position.bucket_c,
            "side": position.side,
            "status": position.status,
            "exit_class": exit_class(position.status),
            "entry_price": float(position.entry_price),
            "exit_price": float(position.exit_price),
            "size_usd": float(position.size_usd),
            "shares": shares,
            "outcome": outcome,
            "model_prob": position.model_prob,
            "execution_mode": position.execution_mode,
            "cluster": (position.station_icao, position.target_date),
        })

    return rows, skipped


def scenario_pnl_usd(row: dict, scenario: str) -> float:
    """
    This row's P&L in dollars under one scenario.

    The as-traded arithmetic is storage.position_economics'
    `realized_pnl_usd` -- (exit - entry) x notional_shares -- so a figure
    from this module and a figure from that view can be compared without a
    translation step. It is therefore GROSS OF THE ENTRY-SIDE FEE, exactly
    as the stored ledger is; the fee enters only in price_edge() below,
    where it is the quantity being tested.
    """
    if scenario not in _REVALUED_CLASSES:
        raise ValueError(f"Unknown scenario '{scenario}' -- expected one of {SCENARIOS}.")
    revalued = _REVALUED_CLASSES[scenario]
    exit_value = row["outcome"] if row["exit_class"] in revalued else row["exit_price"]
    return (exit_value - row["entry_price"]) * row["shares"]


def _totals(rows: Sequence[dict], scenario: str) -> Tuple[float, float]:
    """(pnl_usd, staked_usd) for one scenario."""
    pnl = sum(scenario_pnl_usd(row, scenario) for row in rows)
    staked = sum(row["size_usd"] for row in rows)
    return pnl, staked


def _clusters(rows: Sequence[dict]) -> Dict[Tuple[str, date], List[dict]]:
    """Rows grouped by station-day -- the unit of independent weather."""
    grouped: Dict[Tuple[str, date], List[dict]] = {}
    for row in rows:
        grouped.setdefault(row["cluster"], []).append(row)
    return grouped


def _bootstrap_ci(rows: Sequence[dict], statistic) -> Optional[Tuple[float, float]]:
    """
    Day-clustered bootstrap interval for `statistic(rows) -> float or None`.

    Resamples STATION-DAYS with replacement, not rows. With a single cluster
    every resample is the same cluster, so the interval collapses onto the
    point estimate -- which is the honest answer for one day of weather, and
    is what a row-level bootstrap would hide behind a comfortable-looking
    width.
    """
    if not rows:
        return None
    clusters = list(_clusters(rows).values())
    rng = random.Random(BOOTSTRAP_SEED)
    draws: List[float] = []
    for _ in range(BOOTSTRAP_ITERATIONS):
        sample: List[dict] = []
        for _ in range(len(clusters)):
            sample.extend(rng.choice(clusters))
        value = statistic(sample)
        if value is not None:
            draws.append(value)
    if not draws:
        return None
    draws.sort()
    low_index = int(CI_ALPHA / 2 * (len(draws) - 1))
    high_index = int((1 - CI_ALPHA / 2) * (len(draws) - 1))
    return draws[low_index], draws[high_index]


def _return_pct(rows: Sequence[dict], scenario: str) -> Optional[float]:
    pnl, staked = _totals(rows, scenario)
    if not staked:
        return None
    return pnl / staked


def price_edge(rows: Sequence[dict]) -> Optional[dict]:
    """
    The decay alarm: realised win rate minus mean entry price, per share.

    None on an empty cohort rather than zeros. A price edge of 0.0 is a
    book with no edge at all, and a dashboard that prints it for "no data"
    has told the operator the opposite of the truth.

    THE MEANS ARE UNWEIGHTED, which is what makes this comparable to
    config.py's 0.306 / 0.344 pair: those are simple means over rows, and a
    stake-weighted version of the same quantity is a different number.
    Position size is Kelly-driven and correlates with model_prob, so
    weighting would fold a sizing question into a pricing measurement.

    NET charges the ENTRY-side taker fee and nothing else. A position held
    to settlement pays 0.05 x (1-p) x p per share going in and pays nothing
    coming out -- redeeming a resolved token is not a trade, as
    risk_manager.taker_fee_per_share records. This is the quantity
    config.COHORT_KILL_NET_PRICE_EDGE is keyed to.
    """
    if not rows:
        return None
    entries = [row["entry_price"] for row in rows]
    outcomes = [row["outcome"] for row in rows]
    fees = [
        ev_engine.taker_fee_pct_of_notional(row["entry_price"]) * row["entry_price"]
        for row in rows
    ]
    mean_entry = statistics.fmean(entries)
    win_rate = statistics.fmean(outcomes)
    mean_fee = statistics.fmean(fees)
    gross = win_rate - mean_entry
    return {
        "n": len(rows),
        "n_days": len(_clusters(rows)),
        "mean_entry_price": mean_entry,
        "win_rate": win_rate,
        "price_edge": gross,
        "mean_fee_per_share": mean_fee,
        "net_price_edge": gross - mean_fee,
        "ci_net_price_edge": _bootstrap_ci(rows, _net_price_edge_statistic),
    }


def _net_price_edge_statistic(rows: Sequence[dict]) -> Optional[float]:
    """price_edge()'s net figure, as a bare statistic for the bootstrap."""
    if not rows:
        return None
    return statistics.fmean(
        row["outcome"]
        - row["entry_price"]
        - ev_engine.taker_fee_pct_of_notional(row["entry_price"]) * row["entry_price"]
        for row in rows
    )


def summarize(rows: Sequence[dict]) -> Optional[dict]:
    """
    The whole measurement for one cohort, or None for an empty one.

    n AND n_days ALWAYS TRAVEL TOGETHER, as calibration_panel's second
    reporting rule requires: rows on one station-day are one draw of the
    weather, so n overstates the evidence and n_days is the honest ceiling.
    """
    if not rows:
        return None

    staked = sum(row["size_usd"] for row in rows)
    scenarios = {}
    for scenario in SCENARIOS:
        pnl, _ = _totals(rows, scenario)
        scenarios[scenario] = {
            "pnl_usd": pnl,
            "return_pct": (pnl / staked) if staked else None,
        }

    def _cost(group: Sequence[dict]) -> float:
        """
        What holding this group would have been worth, minus what it booked.

        SIGN CONVENTION, and it carries a finding: POSITIVE means the rule
        cost money against holding, NEGATIVE means it earned money. Both
        occur -- see by_status below.
        """
        return sum(
            scenario_pnl_usd(row, "held") - scenario_pnl_usd(row, "as_traded") for row in group
        )

    by_class: Dict[str, dict] = {}
    for name in ("stop", "take", "other"):
        class_rows = [row for row in rows if row["exit_class"] == name]
        by_class[name] = {"n": len(class_rows), "cost_usd": _cost(class_rows)}

    # THE SAME DOLLARS, GROUPED BY EXACT STATUS, because the three-class
    # view hides a sign change. Measured 2026-09-04 over the published
    # window: closed_stop_loss cost +$600.61 over 222 rows while
    # closed_trailing_stop EARNED $22.09 over 15, and that $22.09 is
    # precisely what made config.py's residual look unexplained -- its
    # "222 fires" stop figure excluded the trailing rows, while the table's
    # "take only" column included them. A breakout that could not show two
    # stop rules disagreeing would have left that undiscoverable.
    by_status: Dict[str, dict] = {}
    for status in sorted({row["status"] for row in rows}):
        status_rows = [row for row in rows if row["status"] == status]
        by_status[status] = {"n": len(status_rows), "cost_usd": _cost(status_rows)}

    held_minus_as_traded = scenarios["held"]["pnl_usd"] - scenarios["as_traded"]["pnl_usd"]
    decomposed = sum(entry["cost_usd"] for entry in by_class.values())

    return {
        "n": len(rows),
        "n_days": len(_clusters(rows)),
        "staked_usd": staked,
        "first_date": min(row["target_date"] for row in rows),
        "last_date": max(row["target_date"] for row in rows),
        "scenarios": scenarios,
        "by_exit_class": by_class,
        "by_status": by_status,
        "reconciliation": {
            "stop_cost_usd": by_class["stop"]["cost_usd"],
            "take_cost_usd": by_class["take"]["cost_usd"],
            # THE TERM config.py COULD NOT NAME. How far resolution-closed
            # rows sat from clean settlement value, in aggregate. Equal to
            # held - neither, by construction.
            "other_gap_usd": by_class["other"]["cost_usd"],
            "held_minus_as_traded_usd": held_minus_as_traded,
            "closes": abs(decomposed - held_minus_as_traded) <= CENT,
        },
        "price_edge": price_edge(rows),
        "ci": {
            "held_return_pct": _bootstrap_ci(rows, lambda r: _return_pct(r, "held")),
            "as_traded_return_pct": _bootstrap_ci(rows, lambda r: _return_pct(r, "as_traded")),
        },
    }


def windows(
    rows: Sequence[dict],
    as_of: Optional[date] = None,
    window_days: Sequence[int] = WINDOW_DAYS,
) -> Dict[str, Optional[dict]]:
    """
    {"all_time": summary, "trailing_30d": summary or None, ...}

    A window with no rows is None, not a summary of nothing -- the same rule
    as summarize(). Decay has to show up as a TREND and not only as a level,
    which is the whole reason the trailing windows exist: an all-time figure
    dominated by August cannot fall fast enough to warn anybody.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc).date()
    out: Dict[str, Optional[dict]] = {"all_time": summarize(rows)}
    for days in window_days:
        cutoff = as_of - timedelta(days=days)
        out[f"trailing_{days}d"] = summarize(
            [row for row in rows if row["target_date"] >= cutoff]
        )
    return out


def kill_criterion(window_summaries: Dict[str, Optional[dict]]) -> dict:
    """
    Has the price edge failed its pre-committed level?

    fired is True, False, or None for "not enough evidence" -- never False
    on a thin sample, because "we cannot tell" and "it is fine" are the two
    things an operator most needs kept apart during a drawdown.

    Reads config at CALL time, so the constants stay a config question.

    THERE IS NO ACTION IN THE RETURN VALUE and there must not be. What
    firing means -- halt the station, halt the book, drop to paper -- is an
    operator decision recorded in config.COHORT_KILL_* and in the
    remediation plan; encoding one here would turn a Phase 0 measurement
    into a live trading behaviour.
    """
    window_days = config.COHORT_KILL_WINDOW_DAYS
    minimum = config.COHORT_KILL_MIN_STATION_DAYS
    level = config.COHORT_KILL_NET_PRICE_EDGE
    summary = window_summaries.get(f"trailing_{window_days}d")

    status = {
        "window_days": window_days,
        "level": level,
        "min_station_days": minimum,
        "n": summary["n"] if summary else 0,
        "n_days": summary["n_days"] if summary else 0,
        "net_price_edge": None,
        "fired": None,
    }
    if summary is None:
        return status
    edge = summary["price_edge"]
    if edge is None:
        return status
    status["net_price_edge"] = edge["net_price_edge"]
    if status["n_days"] < minimum:
        return status
    status["fired"] = edge["net_price_edge"] <= level
    return status


def reproduction_check(summary: Optional[dict]) -> dict:
    """
    Does this cohort reproduce the published measurement, to the cent?

    The acceptance criterion for this module, and the reason it can fail is
    the reason it is worth anything: a check that could only pass would
    satisfy the wording and none of the intent. A mismatch IS the finding --
    per config.py's own instruction, the discrepancy has to be resolved
    before the module is trusted.
    """
    by_scenario: Dict[str, dict] = {}
    for scenario, published in PUBLISHED_TOTALS_USD.items():
        measured = None
        if summary is not None:
            measured = summary["scenarios"][scenario]["pnl_usd"]
        delta = None if measured is None else measured - published
        by_scenario[scenario] = {
            "published": published,
            "measured": measured,
            "delta": delta,
            "matches": delta is not None and abs(delta) <= CENT,
        }

    staked_measured = summary["staked_usd"] if summary else None
    staked_delta = None if staked_measured is None else staked_measured - PUBLISHED_STAKED_USD
    staked = {
        "published": PUBLISHED_STAKED_USD,
        "measured": staked_measured,
        "delta": staked_delta,
        "matches": staked_delta is not None and abs(staked_delta) <= CENT,
    }
    counts = {
        "published_n": PUBLISHED_N,
        "measured_n": summary["n"] if summary else 0,
        "published_n_days": PUBLISHED_N_DAYS,
        "measured_n_days": summary["n_days"] if summary else 0,
    }
    return {
        "window": PUBLISHED_WINDOW,
        "by_scenario": by_scenario,
        "staked": staked,
        "counts": counts,
        "matches": all(entry["matches"] for entry in by_scenario.values()) and staked["matches"],
    }


# ---------------------------------------------------------------------------
# The I/O half
# ---------------------------------------------------------------------------

def load_cohort(
    stations: Optional[Sequence[str]] = None,
    since: Optional[date] = None,
    until: Optional[date] = None,
    limit: int = HISTORY_LIMIT,
) -> Tuple[List[dict], Dict[str, int]]:
    """
    cohort_rows() against the stored book, across every configured station.

    READS ONLY. storage.load_position_history returns CLOSED positions, and
    load_settled_buckets is a plain select, so nothing here can alter a
    trading decision or a stored row.
    """
    all_rows: List[dict] = []
    all_skipped: Dict[str, int] = {}
    for station_icao in (stations if stations is not None else sorted(config.STATIONS)):
        rows, skipped = cohort_rows(
            storage.load_position_history(station_icao, limit=limit),
            storage.load_settled_buckets(station_icao),
            since=since,
            until=until,
        )
        all_rows.extend(rows)
        for reason, count in skipped.items():
            all_skipped[reason] = all_skipped.get(reason, 0) + count
    return all_rows, all_skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_usd(value: Optional[float]) -> str:
    return "--" if value is None else f"{value:+,.2f}"


def _fmt_pct(value: Optional[float]) -> str:
    return "--" if value is None else f"{value * 100:+.1f}%"


def _print_summary(label: str, summary: Optional[dict]) -> None:
    print(f"\n{label}")
    print("-" * len(label))
    if summary is None:
        print("  no rows in this window")
        return
    print(
        f"  {summary['n']} rows over {summary['n_days']} station-days, "
        f"${summary['staked_usd']:,.2f} staked, "
        f"{summary['first_date']}..{summary['last_date']}"
    )
    for scenario in SCENARIOS:
        cell = summary["scenarios"][scenario]
        print(f"    {scenario:<12} {_fmt_usd(cell['pnl_usd']):>12}   {_fmt_pct(cell['return_pct']):>8}")

    ci = summary["ci"]["held_return_pct"]
    if ci:
        print(f"    held CI (station-day clustered)   [{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}]")
    ci = summary["ci"]["as_traded_return_pct"]
    if ci:
        print(f"    as-traded CI                      [{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}]")

    rec = summary["reconciliation"]
    print("  reconciliation")
    print(f"    stop cost      {_fmt_usd(rec['stop_cost_usd']):>12}  ({summary['by_exit_class']['stop']['n']} rows)")
    print(f"    take cost      {_fmt_usd(rec['take_cost_usd']):>12}  ({summary['by_exit_class']['take']['n']} rows)")
    print(f"    other gap      {_fmt_usd(rec['other_gap_usd']):>12}  ({summary['by_exit_class']['other']['n']} rows)")
    print(f"    held-as traded {_fmt_usd(rec['held_minus_as_traded_usd']):>12}  closes={rec['closes']}")

    edge = summary["price_edge"]
    print("  price edge (THE decay alarm -- not Brier)")
    print(f"    mean entry {edge['mean_entry_price']:.3f} vs win rate {edge['win_rate']:.3f}")
    print(f"    gross {edge['price_edge']:+.4f}  fee {edge['mean_fee_per_share']:.4f}  net {edge['net_price_edge']:+.4f}")
    if edge["ci_net_price_edge"]:
        low, high = edge["ci_net_price_edge"]
        print(f"    net CI [{low:+.4f}, {high:+.4f}]")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hold-vs-actual cohort monitor (P0-5). Read-only.",
    )
    parser.add_argument("--station", action="append", dest="stations",
                        help="limit to this station (repeatable)")
    parser.add_argument("--since", help="earliest target date, YYYY-MM-DD")
    parser.add_argument("--until", help="latest target date, YYYY-MM-DD")
    parser.add_argument("--as-of", help="reference date for the trailing windows, YYYY-MM-DD")
    parser.add_argument("--reproduce", action="store_true",
                        help=f"score the published window {PUBLISHED_WINDOW[0]}..{PUBLISHED_WINDOW[1]} "
                             "and check it against the published totals")
    args = parser.parse_args(argv)

    since = date.fromisoformat(args.since) if args.since else None
    until = date.fromisoformat(args.until) if args.until else None
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    if args.reproduce:
        since, until = PUBLISHED_WINDOW

    rows, skipped = load_cohort(stations=args.stations, since=since, until=until)
    if skipped:
        print("skipped:")
        for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>6}  {reason}")

    if args.reproduce:
        summary = summarize(rows)
        _print_summary(f"published window {PUBLISHED_WINDOW[0]}..{PUBLISHED_WINDOW[1]}", summary)
        report = reproduction_check(summary)
        print("\nreproduction check (to the cent)")
        print("-------------------------------")
        for scenario, cell in report["by_scenario"].items():
            mark = "OK  " if cell["matches"] else "MISS"
            print(
                f"  {mark} {scenario:<12} published {cell['published']:>+10.2f}  "
                f"measured {_fmt_usd(cell['measured']):>12}  delta {_fmt_usd(cell['delta']):>10}"
            )
        staked = report["staked"]
        mark = "OK  " if staked["matches"] else "MISS"
        print(
            f"  {mark} {'staked':<12} published {staked['published']:>10.2f}  "
            f"measured {_fmt_usd(staked['measured']):>12}  delta {_fmt_usd(staked['delta']):>10}"
        )
        counts = report["counts"]
        print(
            f"       rows {counts['measured_n']} vs {counts['published_n']} published, "
            f"station-days {counts['measured_n_days']} vs {counts['published_n_days']}"
        )
        print(f"\n  MATCHES: {report['matches']}")
        if not report["matches"]:
            print("  The discrepancy IS the finding -- resolve it before trusting the module.")
        return 0 if report["matches"] else 1

    window_summaries = windows(rows, as_of=as_of)
    _print_summary("all time", window_summaries["all_time"])
    for days in WINDOW_DAYS:
        _print_summary(f"trailing {days} days", window_summaries[f"trailing_{days}d"])

    status = kill_criterion(window_summaries)
    print("\nkill criterion (config.COHORT_KILL_*)")
    print("-------------------------------------")
    verdict = {None: "NO VERDICT -- sample below the minimum", True: "FIRED", False: "holding"}[
        status["fired"]
    ]
    measured_edge = status["net_price_edge"]
    edge_text = "--" if measured_edge is None else f"{measured_edge:+.4f}"
    print(
        f"  net price edge {edge_text} vs level {status['level']:+.4f} "
        f"on trailing {status['window_days']}d"
    )
    print(f"  {status['n_days']} station-days (minimum {status['min_station_days']})")
    print(f"  {verdict}")
    print("\n  Firing implies no action here. Phase 0 is measurement only; see")
    print("  config.COHORT_KILL_NET_PRICE_EDGE for why the response is an")
    print("  operator decision rather than a constant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
