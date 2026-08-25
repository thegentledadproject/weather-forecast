"""
promotion_dossier.py

PURPOSE
-------
Everything the decision to promote ONE station needs, assembled in one run,
by one set of rules.

TWO PROMOTIONS EXIST AND THEY ARE NOT THE SAME DECISION
-------------------------------------------------------
  1. INTO config.LIVE_TRADING_STATIONS. This authorises `--mode simulation`,
     which submits nothing and spends nothing, and is the ONLY way to earn
     the resolved simulated orders MATURITY_MIN_SIMULATED_ORDERS counts.
     config.live_mode_is_permitted() deliberately does not gate simulation on
     maturity, precisely so a station is never barred from the activity that
     produces its own evidence.
  2. INTO REAL MONEY, which needs BOTH the allowlist AND
     station_maturity() == "mature".

Conflating them is the expensive mistake, because (1) is cheap and reversible
and (2) is neither. The dossier reports them separately for that reason.

This assembly was done twice by hand -- WSSS, then RCSS on 2026-08-19 --
by reading config.print_maturity_report(), paper_trading_report.py, the
latest backtest summary and stop_loss_audit.py side by side and reconciling
them. Reconciling them is the part that goes wrong: the four sources count
different things (entries taken vs entries scored, closed positions vs
settled days, paper rows vs simulation rows), and the RCSS note in
config.MATURITY_OVERRIDE records a figure that was already ten days stale
when it was acted on.

IT RECOMMENDS NOTHING, DELIBERATELY
-----------------------------------
Both promotions so far were operator decisions taken AGAINST the measured
criteria and recorded as such -- "buying execution-path evidence, not edge",
with config.MATURITY_OVERRIDE's own note conceding that "by this system's own
best estimate the EV of these trades is negative". A tool that printed a
verdict would launder that judgement into what looks like an arithmetic
result. This prints what is measured, what is missing, and what promotion
would authorise. The decision stays where it was.

THE NEW MEASUREMENT: beats_market, OFF THE LIVE BOOK
----------------------------------------------------
config.calibration_vs_market() -- the criterion its own comment calls the one
that "most deserves the word mature" -- reads the latest BACKTEST summary. The
reason given there is that scoring it needs point-in-time model probabilities
and "the live path keeps only the latest EV snapshot
(ev_engine.save_ev_snapshot overwrites ev_latest_<ICAO>.json)".

THAT IS NO LONGER THE WHOLE PICTURE. `positions.model_prob` now stores the
model's probability AT ENTRY, per position, copied from the decision and
never recomputed (executor.open_position's own comment: "re-deriving
model_prob at any later point would read a calibration that has since seen
the outcome"). Joined to `settled_buckets`, that is a complete point-in-time
Brier score off the live book, with no backtest run, no lookahead, and none
of the four failure modes calibration_vs_market fails on (stale window,
superseded git sha, missing brier_n, no run at all).

WHAT THIS SECOND READ IS AND IS NOT. It is NOT a replacement for the gate,
and this module changes no gate. The two numbers are not expected to agree
and disagreement is not a bug in either:

  - The backtest makes its OWN entry decisions over a replayed window. The
    live book contains the entries the daemon ACTUALLY took. Different
    selections of the same market, scored the same way.
  - Both share one selection effect, so this inherits it rather than
    introducing it: only entries that PASSED every gate are scored, i.e.
    the model is graded exactly where it claimed an edge. That is a
    friendlier test than grading it across all candidates, and neither
    number should be read as the model's calibration in general.
  - The market term is `positions.entry_price`, the price the row was booked
    at. Its meaning has CHANGED once: before 7ccb98a (2026-08-18) the
    simulation rung stored the padded limit -- "the worst price accepted" --
    rather than the price it expected to pay. Two of the seven simulation
    rows then stored were high by one and two ticks, and were deliberately
    not backfilled. Paper rows are unaffected (they store the decision
    quote). A window spanning that date mixes two definitions on the
    simulation rows only; --since is how you exclude it.

PAIRED, AND WITH ITS UNCERTAINTY
--------------------------------
Two Brier means side by side invite reading a gap that the sample cannot
support. RCSS was promoted on "0.062 vs 0.145" over 9 entries -- a ratio of
more than two, which sounds decisive and, at n=9 on a statistic whose
per-entry terms are mostly 0s and 1s, is not.

So the terms are PAIRED per entry (added to both lists or to neither, as
backtest/report._brier_scores() also insists) and the per-entry difference is
reported with its standard error. That is the same test the rest of this
codebase already applies to bias: MAX_BIAS_STANDARD_ERROR_C gates on stderr
rather than magnitude, because "a large bias measured precisely is
correctable, a small one measured noisily is not". The identical logic
applies to an edge over the market, and until now nothing applied it.

BUCKET-BOUNDS DRIFT
-------------------
A bucket is only meaningful relative to the event's bounds, and the bounds
MOVE. config.STATIONS' bucket_min_c/bucket_max_c are explicitly "a seasonal
cross-check that drifts (ten of thirteen stations had by 2026-08-14, two by
5C)"; the authoritative per-day bounds are the live-derived ones stored on
`settled_buckets`.

This matters most for the EDGE buckets, which are censored catch-alls ("X or
below", "Y or higher") rather than single degrees. A window shift turns an
interior bucket into an edge bucket and back, which changes what winning
MEANS for a position on it -- so a book spanning a shift is not one sample.
The dossier counts scored entries whose day traded bounds other than the ones
config now carries, and counts entries taken on an edge bucket, rather than
silently averaging across the change.

DEPENDENCIES
------------
argparse, statistics, datetime (standard library)
config.py, storage.py, paper_trading_report.py (local)
backtest/resolution.py (local -- for resolution_exit_price only, the same
function backtest/report.py scores with, so the two agree by construction)
"""

import argparse
import statistics
from datetime import date
from typing import Dict, List, Optional, Tuple

import config
import paper_trading_report
import storage
from backtest import resolution
from models import Position

# Read cap for every position query here. Matches the limit
# config.maturity_report() uses for its own order_path count, so the two
# cannot disagree about how much history they looked at.
HISTORY_LIMIT = 1000

# Above this the paired gap is called separable from noise. Two standard
# errors, the same convention MATURITY_BIAS_SPLIT_HALF_SIGMAS applies to the
# bias halves.
GAP_SIGMAS = 2.0


# --------------------------------------------------------------------------
# Scoring the live book
# --------------------------------------------------------------------------

def score_entries(
    positions: List[Position],
    settled: Dict[date, Tuple[int, int, int]],
    since: Optional[date] = None,
    until: Optional[date] = None,
) -> Tuple[List[dict], Dict[str, int]]:
    """
    ([scored entry, ...], {reason: n_skipped}) over positions held IN MEMORY.

    Pure -- no I/O of any kind. Separated from scorable_entries() below for
    the same reason paper_trading_report.summarize_positions() is separated
    from summarize_paper_performance(): the arithmetic that decides whether a
    station beats the market should be testable without a database, and there
    should be exactly one copy of it.

    An entry is scorable when its target date has a settled bucket AND the
    row carries a stored model_prob. Both absences are counted rather than
    silently dropped: the whole point of the skip tally is that "we could
    not score it" stays visible next to "we scored it".

    THE WINNING BUCKET IS READ, NOT DERIVED. `settled_buckets` stores the
    bucket the market settled into together with that day's live-derived
    bounds, so there is no rounding rule to re-apply and no chance of
    scoring a day against a bucket map it did not trade under. That is the
    one thing this has over backtest/report._brier_scores(), which
    recomputes the bucket from an observation using TODAY's config bounds.

    model_prob IS ALREADY SIDE-ADJUSTED and is not flipped here.
    ev_engine.build_ev_table computes `side_model_prob = model_prob if side
    == "YES" else (1 - model_prob)` and stores THAT on the EVResult, which
    executor.open_position copies to the row. It is P(this side wins),
    directly comparable to a 0/1 outcome for this side -- exactly as
    backtest/report._brier_scores() documents for its own copy of the value.

    entry_price needs no adjustment either: it is the price of THIS side, so
    it is already the market's implied probability that this side wins.
    """
    entries: List[dict] = []
    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for position in positions:
        if since is not None and position.target_date < since:
            skip("outside --since/--until window")
            continue
        if until is not None and position.target_date > until:
            skip("outside --since/--until window")
            continue

        row = settled.get(position.target_date)
        if row is None:
            skip("target date not settled yet")
            continue
        winning_bucket, bounds_min, bounds_max = row

        # NULL is the honest value on rows written before the column existed
        # and on manual_trigger rows that bypassed the model -- storage.py
        # says so and says not to backfill them. "No model ran" must stay
        # distinguishable from "the model said 0", so they are skipped.
        if position.model_prob is None:
            skip("no stored model_prob")
            continue
        if position.entry_price is None:
            skip("no entry price")
            continue

        try:
            outcome = resolution.resolution_exit_price(
                position.side, position.bucket_c, winning_bucket
            )
        except ValueError:
            # resolution_exit_price refuses a side it does not recognise. One
            # corrupt row must not take down the whole dossier -- an
            # unreadable entry is an UNSCORED entry, the same way
            # config.maturity_report() treats an unreadable criterion as a
            # failed one rather than raising.
            skip("unrecognised side")
            continue
        entries.append({
            "position": position,
            "outcome": outcome,
            "model_prob": float(position.model_prob),
            "market_price": float(position.entry_price),
            "winning_bucket": winning_bucket,
            "bounds": (bounds_min, bounds_max),
        })

    return entries, skipped


def scorable_entries(
    station_icao: str,
    limit: int = HISTORY_LIMIT,
    since: Optional[date] = None,
    until: Optional[date] = None,
) -> Tuple[List[dict], Dict[str, int]]:
    """
    score_entries() against one station's stored book -- the I/O half.

    Reads CLOSED positions only, which is what storage.load_position_history()
    returns and what config.maturity_report() counts, so the two cannot
    disagree about the history they saw. An open position on an already-
    settled day is not scored; position_manager closes those as resolved, so
    in practice the set is empty rather than merely small.
    """
    return score_entries(
        storage.load_position_history(station_icao, limit=limit),
        storage.load_settled_buckets(station_icao),
        since=since,
        until=until,
    )


def live_calibration(entries: List[dict]) -> Optional[dict]:
    """
    Brier for the model and for the market over the same entries, plus the
    PAIRED per-entry difference and its standard error.

    None when nothing is scorable. Returning None beats returning zeros: a
    Brier of 0.0 is a PERFECT score, and an empty book must not be able to
    print one -- the same trap backtest/report._brier_scores() calls out.

    gap is defined market_term - model_term, so POSITIVE MEANS THE MODEL
    WON that entry. mean(gap) > 0 is `brier_model < brier_market`, i.e. the
    direction config.calibration_vs_market() requires, expressed as one
    number that has a standard error.

    THE STANDARD ERROR IS OPTIMISTIC WHEN n > n_days, AND IT USUALLY IS.
    Every entry taken on one station-day resolves off the SAME settlement --
    a YES on 34 and a NO on 36 on the same morning are one draw of the
    weather, not two -- so treating them as independent samples divides by a
    larger n than the evidence supports and narrows the interval. n_days is
    reported alongside n so the reader can see the real number of
    independent draws; a proper day-clustered error is not computed here
    because at these sample sizes it would be estimated off single-digit
    clusters, which is its own overreach. Read n_days as the honest ceiling
    on how much this can possibly say.
    """
    if not entries:
        return None

    model_terms = [(e["model_prob"] - e["outcome"]) ** 2 for e in entries]
    market_terms = [(e["market_price"] - e["outcome"]) ** 2 for e in entries]
    gaps = [mk - md for mk, md in zip(market_terms, model_terms)]

    n = len(entries)
    n_days = len({e["position"].target_date for e in entries})
    mean_gap = statistics.fmean(gaps)
    stderr = statistics.stdev(gaps) / (n ** 0.5) if n > 1 else None

    return {
        "n": n,
        "n_days": n_days,
        "brier_model": statistics.fmean(model_terms),
        "brier_market": statistics.fmean(market_terms),
        "mean_gap": mean_gap,
        "gap_stderr": stderr,
        "separable": (stderr is not None and stderr > 0
                      and abs(mean_gap) > GAP_SIGMAS * stderr),
        "model_wins": sum(1 for g in gaps if g > 0),
        "win_rate": sum(1 for e in entries if e["outcome"] > 0) / n,
    }


def bounds_drift(station_icao: str, entries: List[dict]) -> dict:
    """
    How far the scored book's per-day bucket windows sit from the window
    config now carries, and how much of it sat on a censored edge bucket.

    Both are reasons a book is not one sample. See this module's docstring.
    """
    try:
        station = config.get_station(station_icao)
        config_bounds = (station.bucket_min_c, station.bucket_max_c)
    except KeyError:
        config_bounds = None

    windows: Dict[Tuple[int, int], List[date]] = {}
    on_edge = 0
    for entry in entries:
        target_date = entry["position"].target_date
        windows.setdefault(entry["bounds"], []).append(target_date)
        if entry["position"].bucket_c in entry["bounds"]:
            on_edge += 1

    mismatched = sum(
        len(dates) for bounds, dates in windows.items()
        if config_bounds is not None and bounds != config_bounds
    )
    return {
        "config_bounds": config_bounds,
        "windows": {b: (min(d), max(d), len(d)) for b, d in windows.items()},
        "n_mismatched": mismatched,
        "n_on_edge": on_edge,
    }


def by_execution_mode(station_icao: str, limit: int = HISTORY_LIMIT) -> Dict[str, List[Position]]:
    """
    Closed positions grouped by the executor rung that opened them.

    The rungs are not interchangeable evidence. MATURITY_MIN_SIMULATED_ORDERS
    counts "simulation" rows ONLY -- a station with a hundred paper rows and
    no simulation rows has never exercised the order path at all, which is
    the exact gap config.MATURITY_OVERRIDE records against RCSS ("ZERO
    ORDER-PATH EVIDENCE ... the one gap that is cheap to close").
    """
    grouped: Dict[str, List[Position]] = {}
    for position in storage.load_position_history(station_icao, limit=limit):
        grouped.setdefault(position.execution_mode or "unknown", []).append(position)
    return grouped


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _print_standing(station_icao: str) -> None:
    # Resolved BEFORE ANYTHING IS PRINTED. live_mode_is_permitted("live")
    # calls station_maturity(), which on an overridden station prints a
    # warning on every call -- deliberately, and it must not be suppressed.
    # Emitted mid-section it lands between two rows of this table and reads
    # as part of it, so it is flushed ahead of the header instead.
    permissions = [
        (mode, config.live_mode_is_permitted(station_icao, mode))
        for mode in ("simulation", "live")
    ]

    _rule("STANDING -- what this station may do today")

    allowlisted = station_icao in config.LIVE_TRADING_STATIONS
    override = config.MATURITY_OVERRIDE.get(station_icao)

    print(f"  allowlisted (LIVE_TRADING_STATIONS): {'yes' if allowlisted else 'NO'}")
    if override:
        print(f"  MATURITY_OVERRIDE: forced '{override[0]}' -- {override[1]}")
    else:
        print("  MATURITY_OVERRIDE: none (maturity is whatever the criteria measure)")

    for mode, permitted in permissions:
        print(f"  --mode {mode:<11} {'PERMITTED' if permitted else 'blocked'}")

    if config.bias_gate_is_overridden(station_icao):
        print("  bias gate: OVERRIDDEN (BIAS_GATE_OVERRIDE_STATIONS)")


def _print_gate(station_icao: str) -> None:
    _rule("THE MEASURED GATE -- config.maturity_report()")

    report = config.maturity_report(station_icao)
    for name, (passed, detail) in report["criteria"].items():
        print(f"  [{'ok' if passed else '--'}] {name:<16} {detail}")

    measured = "MATURE" if report["mature"] else "exploratory"
    print(f"\n  measured verdict: {measured}")

    # An override makes station_maturity() disagree with the criteria above,
    # and that disagreement is the whole content of the override. Printing
    # only the effective value would hide which criteria are being bought
    # past, which is the one thing a promotion review must see.
    override = config.MATURITY_OVERRIDE.get(station_icao)
    if override:
        failing = [n for n, (passed, _) in report["criteria"].items() if not passed]
        print(f"  effective verdict: {override[0].upper()} (override)")
        if failing:
            print(f"  overridden past:   {', '.join(failing)}")


def _print_book(station_icao: str) -> None:
    _rule("THE PAPER BOOK -- realized economics")

    history = paper_trading_report.load_paper_history(station_icao, limit=HISTORY_LIMIT)
    summary = paper_trading_report.summarize_positions(history)
    if summary is None:
        print("  no closed paper positions with a usable realized return.")
    else:
        print(f"  trades closed:     {summary['n_trades']}")
        print(f"  win rate:          {summary['win_rate']:.1%}")
        print(f"  mean return/trade: {summary['mean_return_pct']:+.1%} "
              f"(sd {summary['std_return_pct']:.1%})")
        if summary["dollar_weighted_return_pct"] is not None:
            print(f"  dollar-weighted:   {summary['total_pnl_usd']:+,.2f} USD on "
                  f"{summary['total_staked_usd']:,.2f} staked = "
                  f"{summary['dollar_weighted_return_pct']:+.1%}")
        print("  by exit reason:")
        for reason, stats in summary["by_exit_reason"].items():
            print(f"    {reason:<16} n={stats['n']:<4} "
                  f"mean_return={stats['mean_return_pct']:+.1%}")

    grouped = by_execution_mode(station_icao)
    print("\n  closed rows by executor rung (simulation is the order-path evidence):")
    if not grouped:
        print("    none")
    for mode in sorted(grouped):
        print(f"    {mode:<16} {len(grouped[mode])}")
    n_sim = len(grouped.get("simulation", []))
    if n_sim < config.MATURITY_MIN_SIMULATED_ORDERS:
        needed = config.MATURITY_MIN_SIMULATED_ORDERS - n_sim
        print(f"\n    {needed} more resolved simulated order(s) needed. This needs the "
              f"allowlist\n    only, not maturity -- `python manual_trigger.py --station "
              f"{station_icao} --mode simulation`.")


def _print_calibration(station_icao: str, since, until) -> None:
    _rule("BEATS_MARKET -- measured on the live book, not the backtest")

    entries, skipped = scorable_entries(station_icao, since=since, until=until)
    stats = live_calibration(entries)

    if stats is None:
        print("  nothing scorable.")
    else:
        print(f"  scored entries:    {stats['n']} over {stats['n_days']} settled day(s)")
        print(f"  brier_model:       {stats['brier_model']:.4f}")
        print(f"  brier_market:      {stats['brier_market']:.4f}")
        direction = "beats" if stats["mean_gap"] > 0 else "LOSES to"
        print(f"  model {direction} market by {abs(stats['mean_gap']):.4f} per entry", end="")
        if stats["gap_stderr"] is not None:
            print(f", stderr {stats['gap_stderr']:.4f}")
            verdict = ("separable from noise"
                       if stats["separable"]
                       else f"NOT separable from zero at {GAP_SIGMAS:.0f} stderr")
            print(f"  -> {verdict}")
        else:
            print(" (one entry -- no standard error)")
        print(f"  model scored better on {stats['model_wins']} of {stats['n']} entries")
        if stats["n"] > stats["n_days"]:
            print(f"  NOTE {stats['n']} entries but only {stats['n_days']} independent "
                  f"settlements -- the stderr above\n       is optimistic by roughly "
                  f"sqrt({stats['n']}/{stats['n_days']}).")
        # NOT the paper book's win rate above: that one asks whether the
        # position was closed at a profit, this one asks whether the side it
        # took was RIGHT about the weather. A stop-loss on a side that went
        # on to settle in the money counts as a loss there and a win here.
        print(f"  settled in this side's favour: {stats['win_rate']:.1%}")

        # The gate counts scored entries, not entries taken, and reads the
        # backtest rather than this. Say both, so a reader cannot mistake a
        # passing count here for a passing criterion there.
        if stats["n"] < config.MATURITY_MIN_BRIER_ENTRIES:
            print(f"\n  Under MATURITY_MIN_BRIER_ENTRIES ({config.MATURITY_MIN_BRIER_ENTRIES}) "
                  f"-- decides nothing formally.")
        print("  The GATE reads the latest backtest summary, not this. This is a "
              "second,\n  independent read of the same question; see the module "
              "docstring for why\n  they are not expected to agree.")

    if skipped:
        print("\n  not scored:")
        for reason in sorted(skipped):
            print(f"    {skipped[reason]:<4} {reason}")

    drift = bounds_drift(station_icao, entries)
    if entries:
        _rule("BUCKET-BOUNDS DRIFT across the scored book")
        print(f"  config bounds now: {drift['config_bounds']}")
        print("  windows actually traded:")
        for bounds, (first, last, n) in sorted(drift["windows"].items()):
            flag = "" if bounds == drift["config_bounds"] else "   <- differs from config"
            print(f"    {bounds[0]}-{bounds[1]}  {first}..{last}  n={n}{flag}")
        if drift["n_mismatched"]:
            print(f"\n  {drift['n_mismatched']} of {len(entries)} scored entries traded a "
                  f"window config no longer\n  carries. A book spanning a shift is not one "
                  f"sample -- bound the window\n  with --since before reading the gap above "
                  f"as a single number.")
        print(f"  {drift['n_on_edge']} entry(s) sat on a CENSORED EDGE bucket, where the "
              f"outcome means\n  'at or beyond', not 'this degree'.")


def _print_what_promotion_buys(station_icao: str) -> None:
    _rule("WHAT PROMOTION WOULD AUTHORISE")

    already = station_icao in config.LIVE_TRADING_STATIONS
    region = config.region_of(station_icao)
    region_others = sorted(
        icao for icao in config.stations_in_region(region)
        if icao != station_icao and icao in config.LIVE_TRADING_STATIONS
    )

    max_concurrent = config.REGION_LIVE_MAX_CONCURRENT_POSITIONS[region]
    max_exposure = config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD[region]
    max_orders = config.REGION_LIVE_MAX_ORDERS_PER_DAY[region]

    print("  Into the allowlist -> `--mode simulation`: submits nothing, spends "
          "nothing,\n  and is what produces order-path evidence.")
    print(f"\n  Into real money -> ${config.LIVE_TRADE_SIZE_USD:.2f} nominal per order, "
          f"upsized to the exchange\n  minimum where a bucket demands one "
          f"(ceiling ${config.LIVE_SIZE_OVERSHOOT_CEILING_USD:.2f}).")
    print(f"\n  The blast radius is SHARED WITHIN THE REGION ({region}), not per-station "
          f"and not\n  across regions:")
    print(f"    max concurrent positions: {max_concurrent}")
    print(f"    max total exposure:       ${max_exposure:.2f}")
    print(f"    max orders per day:       {max_orders}")
    if max_concurrent == 0 and max_exposure == 0.0 and max_orders == 0:
        print(f"\n  Promotion alone buys NOTHING here: region {region!r} is funded at zero "
              f"on every\n  live axis, so this station cannot submit a real order regardless "
              f"of allowlist\n  membership. Raising it requires raising "
              f"REGION_LIVE_MAX_CONCURRENT_POSITIONS,\n  REGION_LIVE_MAX_TOTAL_EXPOSURE_USD and "
              f"REGION_LIVE_MAX_ORDERS_PER_DAY for {region!r}\n  in config.py -- and the exposure "
              f"ceiling must be RE-DERIVED from concurrent-\n  positions x worst-case-entry-cost "
              f"for this region's own bucket economics, not\n  copied from Asia's number (see "
              f"config.py, 'RE-DERIVE, DO NOT COPY').")
    elif region_others and not already:
        print(f"\n  So this station would COMPETE with {', '.join(region_others)} for those "
              f"same slots\n  and that same cap rather than adding to them. config's own note: "
              f"adding\n  a further station 'would need that trade-off re-read, not just "
              f"another\n  name here'.")
    print("\n  POLYMARKET_LIVE_TRADING is process-global and not per-station, so the\n"
          "  allowlist is the only thing scoping real money to particular stations.")


def print_dossier(station_icao: str, since=None, until=None) -> None:
    """The whole assembly for one station."""
    try:
        station = config.get_station(station_icao)
    except KeyError:
        print(f"[promotion_dossier] {station_icao} is not in the station registry.")
        return

    print(f"\n{'=' * 72}")
    print(f"PROMOTION DOSSIER -- {station_icao} ({station.display_name})")
    print(f"{'=' * 72}")

    _print_standing(station_icao)
    _print_gate(station_icao)
    _print_book(station_icao)
    _print_calibration(station_icao, since, until)
    _print_what_promotion_buys(station_icao)

    print("\nThis tool recommends nothing -- see the module docstring.\n")


def _parse_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Assemble one station's promotion evidence in a single run."
    )
    parser.add_argument("--station", required=True, help="ICAO code, e.g. ZGGG")
    parser.add_argument("--since", default=None,
                        help="Ignore target dates before this (YYYY-MM-DD). Use it to "
                             "exclude a bucket-window shift, or rows written before "
                             "7ccb98a (2026-08-18) changed what a simulation row's "
                             "entry_price means.")
    parser.add_argument("--until", default=None,
                        help="Ignore target dates after this (YYYY-MM-DD).")
    args = parser.parse_args()

    print_dossier(args.station, _parse_date(args.since), _parse_date(args.until))
