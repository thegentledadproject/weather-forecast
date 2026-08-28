"""
backtest/take_sweep.py

PURPOSE
-------
Answer the question the 2026-08-19 WSSS trade raised and could not settle:
should lottery-priced entries have a fixed profit-take at all, and if so,
how far out?

risk_manager.evaluate_exit() exempts sub-LOTTERY_PRICE_THRESHOLD entries
from the percentage STOP -- the threshold distance there is under
Polymarket's 1-cent tick, so book wobble alone triggers it, and cutting a
ticket on noise forfeits exactly the tail that justified buying it. The
profit-take was never given the same treatment. A 0.08 entry therefore
holds through its whole 8c of downside while its upside is cut at 0.12: a
4-cent move on a 1-cent tick. Both halves of the stop's argument apply to
the upside exit, and neither had been measured against it.

This module varies config.LOTTERY_PROFIT_TAKE_PCT and replays, so every
distance is scored by the same simulator over the same prices.

WHY THIS GOES THROUGH engine.run()
-----------------------------------
Same reason stop_sweep.py does, and the same failure is on record. A naive
walk of each stored position's price path was tried for the stop question
on 2026-08-18 and came out 12x from the live number, because it modelled
the loose thresholds only -- ignoring the post-10:00 tightening that
halves every winner -- and ignored the trailing stop that was live until
2026-08-17. Driving the real risk_manager.evaluate_exit() at the real
simulated hour makes those omissions impossible by construction.

THE ALIAS GOTCHA
----------------
config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT is written as a reference to its
loose sibling, which binds the VALUE at import. Rebinding the loose one
alone leaves the tightened one at the old number and silently models a
tightening this sweep is not trying to model. Both are always set together
below -- see _lottery_take().

WHAT THIS CAN AND CANNOT CLAIM
------------------------------
The engine makes its OWN entry decisions rather than replaying stored
ones, so no row's absolute P&L is expected to equal the live book's (see
engine.py, DELIBERATE DIVERGENCES). Read the ORDERING and the SPREAD
between rows, which do see identical prices and identical entries up to
the point the take first matters.

One coupling is not removable and is not hidden: this is a PORTFOLIO
simulator, so a lottery position exiting at a different hour frees capital
at a different hour and can change what gets entered afterwards. The
non-lottery cohort's exit DECISIONS are identical across rows -- that is
what the separate constant buys -- but its entries are not guaranteed to
be. stop_sweep.py has the same property. It is reported (see the "normal"
block) rather than assumed away.

READ THIS BEFORE BELIEVING ANY ROW: THE PRICE HISTORY IS NOT YET GOOD
ENOUGH
----------------------------------------------------------------------
A take-profit fires on a PEAK. Snapshot coverage before 2026-08-17 is
9-11 hours of the day; it reaches 14-19 only from 2026-08-17 on. Missing
13-15 hours a day means systematically missing peaks, which under-triggers
EVERY take level and drags every row toward its settlement outcome. The
bias is one-directional and it is largest for the widest distances, which
are precisely the rows this tool exists to evaluate.

That is not the same defect as the stop sweep's: a stop fires on a trough
and the same gap hurts it too, but the stop question had 80 closed
positions per row spread over a window where the gap was uniform. Here the
gap CHANGES mid-window, so a wide-take row scored across 2026-08-10..19 is
comparing a peak-blind first half against a peak-sighted second half.

So: --from 2026-08-17 is the only window whose peaks are trustworthy, and
it is three days long. print_coverage() reports what the chosen window
actually had, every run, unconditionally. A row is worth acting on when
the ordering holds per station AND across windows AND on full-coverage
days -- the same bar stop_sweep.py set and did not clear.

STORED-ENTRY REPLAY MODE (--stored, added 2026-08-28)
-----------------------------------------------------
The engine path above re-decides ENTRIES at every take level, under a
capital constraint. A wider take holds capital longer, so it finances
fewer subsequent entries: the 2026-08-28 run moved 44 -> 39 lottery
positions between its tightest and widest rows. Part of every difference
in that table is therefore entry selection, not the take.

--stored removes that confound by replaying the book's OWN stored entries
through the same risk_manager.evaluate_exit(), so the cohort is identical
in every row. What it gives up is the portfolio effect itself: capital
freed by an early exit finances nothing, so a tight take looks free when
in the real book it funds the next trade.

NEITHER MODE IS THE ANSWER. They bracket it, and on 2026-08-28 they
disagreed in SIGN: the engine path scored `none` as the WORST lottery row
in every window, while a marginal read of the stored book scored removing
the take as worth +$194. Run both. A recommendation needs them to agree.

DEPENDENCIES
------------
argparse, sqlite3, statistics, contextlib, datetime (standard library)
config.py (local), backtest/engine.py, backtest/settings.py (local)
"""

import argparse
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import date, datetime, timezone

import config
import risk_manager
from models import Position
from backtest import engine, settings

# A take this far away cannot fire. The risk unit for a lottery entry IS
# the entry price (min(entry, 1-entry) below 0.50), so 99 units above entry
# is price ~8.00 for an 0.08 ticket -- unreachable, leaving resolution as
# the only way out. This is the "no take-profit at all" row.
NO_TAKE = 99.0


@contextmanager
def _lottery_take(pct: float):
    """Set both lottery take constants for the duration, then put them back."""
    old_loose = config.LOTTERY_PROFIT_TAKE_PCT
    old_tight = config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT
    config.LOTTERY_PROFIT_TAKE_PCT = pct
    config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT = pct
    try:
        yield
    finally:
        config.LOTTERY_PROFIT_TAKE_PCT = old_loose
        config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT = old_tight


class StoredRun:
    """
    Duck-types engine.run()'s result for _cohort() and _unresolved_count(),
    so the whole scoring half below is shared between the two modes and
    cannot drift.
    """

    def __init__(self):
        self.closed_positions = []
        self.unresolved_positions = []


def _stored_rows(stations, start, end, db_path):
    """The book's own entries in the window, oldest first."""
    path = str(db_path or config.DB_PATH)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in stations)
        rows = conn.execute(
            "select * from positions"
            " where station_icao in (%s) and target_date >= ? and target_date <= ?"
            "   and entry_price is not null and token_id is not null"
            " order by entry_time" % marks,
            (*stations, start.isoformat(), end.isoformat()),
        ).fetchall()
        settled = {
            (r["station_icao"], r["target_date"]): r["bucket_c"]
            for r in conn.execute("select station_icao, target_date, bucket_c"
                                  " from settled_buckets")
        }
    finally:
        conn.close()
    return rows, settled


def _price_path(token_id, since_ts, until_ts, market_db_path):
    path = str(market_db_path or settings.MARKET_DATA_DB)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return conn.execute(
            "select ts, price from price_snapshots"
            " where token_id = ? and ts >= ? and ts < ? and price is not null"
            " order by ts",
            (token_id, since_ts, until_ts),
        ).fetchall()
    finally:
        conn.close()


def replay_stored(stations, start: date, end: date,
                  db_path=None, market_db_path=None) -> StoredRun:
    """
    Re-run the EXIT rules over the book's OWN stored entries.

    This is the answer to the confound in sweep()'s engine path: there the
    simulator re-decides entries under a capital constraint, so a wider take
    frees capital at a different hour and changes WHICH positions exist. The
    2026-08-28 run moved 44 -> 39 lottery positions between its tightest and
    widest rows, and none of that difference is the take's doing. Here the
    cohort is whatever the book actually entered, identical at every
    distance, so the only thing varying between rows is the exit rule.

    WHAT THIS BUYS AND WHAT IT COSTS. It buys a clean marginal read on the
    take. It gives up the portfolio effect entirely: capital freed by an
    earlier exit finances nothing here, so a tight take looks free when in
    the real book it funds the next entry. The engine path models that and
    this one does not. **The two modes bracket the answer; neither is it.**

    Each snapshot is evaluated through the real risk_manager.evaluate_exit()
    at ITS OWN simulated local hour -- the same reason the engine path
    exists (module docstring, WHY THIS GOES THROUGH engine.run()). A naive
    walk that models the loose thresholds only came out 12x from the live
    number on 2026-08-18, because it missed the post-10:00 tightening.

    Positions with no settlement observation land in unresolved_positions
    rather than being booked at zero -- see _unresolved_count() for why that
    censoring is not neutral on this band.
    """
    run = StoredRun()
    rows, settled = _stored_rows(stations, start, end, db_path)

    for r in rows:
        entry = r["entry_price"]
        target = date.fromisoformat(r["target_date"])
        _, day_end = config.local_day_bounds_utc(r["station_icao"], target)
        entered_at = datetime.fromisoformat(r["entry_time"])

        pos = Position(
            position_id=r["position_id"],
            station_icao=r["station_icao"],
            target_date=target,
            bucket_c=r["bucket_c"],
            side=r["side"],
            entry_price=entry,
            size_usd=r["size_usd"],
            entry_time=r["entry_time"],
            status="open",
            high_water_mark=entry,
            token_id=r["token_id"],
        )

        for ts, price in _price_path(r["token_id"], int(entered_at.timestamp()),
                                     int(day_end.timestamp()), market_db_path):
            at = datetime.fromtimestamp(ts, tz=timezone.utc)
            # AT THE SNAPSHOT, not at wall clock: the tightening is a
            # function of the simulated hour, and config.py's own offset
            # helper refuses to be correct about a past instant otherwise.
            offset = config.current_utc_offset_hours(r["station_icao"], at=at)
            pos.high_water_mark = max(pos.high_water_mark, price)
            decision = risk_manager.evaluate_exit(
                pos, price, local_hour=(at.hour + offset) % 24)
            if decision.should_exit:
                pos.status = "closed_%s" % decision.reason
                pos.exit_price = price
                pos.exit_time = at.isoformat()
                pos.exit_reason = decision.reason
                break

        if pos.status == "open":
            bucket = settled.get((r["station_icao"], r["target_date"]))
            if bucket is None:
                run.unresolved_positions.append(pos)
                continue
            in_bucket = (bucket == r["bucket_c"])
            won = in_bucket if r["side"] == "YES" else not in_bucket
            pos.status = "closed_resolution"
            pos.exit_price = 1.0 if won else 0.0
            pos.exit_reason = "resolution"

        run.closed_positions.append(pos)

    return run


def _cohort(runs, lottery: bool):
    """
    Closed positions on one side of LOTTERY_PRICE_THRESHOLD, as
    (return, stake, status) triples.

    Partitioned on the position's OWN entry price against the threshold in
    force, which is what risk_manager.evaluate_exit() branches on -- not on
    which constant the sweep was varying.
    """
    out = []
    for r in runs:
        for p in r.closed_positions:
            # No exit price means nothing scored this position. Dividing by
            # entry anyway would book a -100% that never happened.
            if p.exit_price is None or not p.entry_price:
                continue
            if (p.entry_price < config.LOTTERY_PRICE_THRESHOLD) != lottery:
                continue
            out.append((
                (p.exit_price - p.entry_price) / p.entry_price,
                p.size_usd,
                p.status,
            ))
    return out


def _unresolved_count(runs, lottery: bool) -> int:
    """
    Positions still open at the end of the window, on one side of the
    threshold. Excluded from every scorecard -- and that exclusion is not
    neutral, which is why it is counted per cohort rather than once.

    A position is unresolved because its settlement observation is not
    visible yet, which correlates with being RECENT, not with being a
    loser. On the 2026-08-17..19 run every closed lottery position was a
    settled total loss and every survivor was excluded -- including the
    WSSS 33 YES ticket that prompted this whole question. The table read
    -100% at every distance while the winners sat outside it.

    So: unresolved is a censoring problem, and on the lottery band it
    censors the tail the band exists to capture.
    """
    return sum(
        1
        for r in runs
        for p in r.unresolved_positions
        if p.entry_price and (p.entry_price < config.LOTTERY_PRICE_THRESHOLD) == lottery
    )


def _score(rows) -> dict:
    """
    Aggregate one cohort.

    Reports the $-weighted return AND the median, because they answer
    different questions and this cohort is where they diverge most: a
    lottery book is a handful of large multiples against many total
    losses, so one 10x can carry a $-weighted average that no typical
    trade resembles. The 2026-08-15 cohort checkpoint is the precedent --
    scoring the overhang turned an apparent +37.5% into -0.3% $-weighted
    against a MEDIAN of -44%.

    `ret_drop1` removes the single largest absolute P&L contributor. If a
    row's advantage does not survive that, the row is one trade.
    """
    if not rows:
        return {"n": 0, "stake": 0.0, "pnl": 0.0, "ret": 0.0, "ret_drop1": 0.0,
                "median": 0.0, "sd": 0.0, "worst": 0.0, "took": 0, "settled": 0}

    rets = [r for r, _, _ in rows]
    stake = sum(s for _, s, _ in rows)
    contribs = [r * s for r, s, _ in rows]
    pnl = sum(contribs)

    # LARGEST CONTRIBUTOR, not largest return: it is the BOOK the row makes
    # a claim about, and a 10x on $1 moves that less than +50% on $100.
    # abs() so a single blow-up is as removable as a single windfall.
    biggest = max(range(len(contribs)), key=lambda i: abs(contribs[i]))
    stake_wo = stake - rows[biggest][1]
    pnl_wo = pnl - contribs[biggest]

    return {
        "n": len(rows),
        "stake": stake,
        "pnl": pnl,
        "ret": pnl / stake if stake else 0.0,
        # Dropping the only trade leaves nothing to divide by. Reported as
        # 0.0 rather than raising: the sparsest row is the one the coverage
        # problem makes likeliest, and it must still print.
        "ret_drop1": pnl_wo / stake_wo if stake_wo else 0.0,
        "median": statistics.median(rets),
        "sd": statistics.stdev(rets) if len(rets) > 1 else 0.0,
        "worst": min(rets),
        "took": sum(1 for _, _, st in rows if st == "closed_take_profit"),
        "settled": sum(1 for _, _, st in rows if st == "closed_resolution"),
    }


def sweep(stations, start: date, end: date, take_pcts, market_db_path=None,
          stored: bool = False, db_path=None) -> dict:
    """
    {take_pct: {"lottery": scorecard, "normal": scorecard, ...}}.

    stored=True scores the book's OWN entries via replay_stored() instead of
    letting engine.run() generate them. That holds the cohort fixed across
    rows at the cost of the portfolio effect -- see replay_stored() for what
    each mode buys and gives up. They bracket the answer; neither is it.
    """
    out = {}
    for pct in take_pcts:
        runs = []
        with _lottery_take(pct):
            if stored:
                runs.append(replay_stored(stations, start, end, db_path=db_path,
                                          market_db_path=market_db_path))
            else:
                for icao in stations:
                    try:
                        runs.append(engine.run(icao, start, end,
                                               market_db_path=market_db_path))
                    except Exception as exc:  # noqa: BLE001 -- one bad station must not void the sweep
                        print(f"  [take_sweep] {icao} failed at take={pct}: "
                              f"{type(exc).__name__}: {str(exc)[:100]}")
        out[pct] = {
            "lottery": _score(_cohort(runs, lottery=True)),
            "normal": _score(_cohort(runs, lottery=False)),
            "unresolved_lottery": _unresolved_count(runs, lottery=True),
            "unresolved_normal": _unresolved_count(runs, lottery=False),
            "stations_run": len(stations) if stored else len(runs),
        }
    return out


def print_coverage(start: date, end: date, market_db_path=None) -> None:
    """
    Hours of the day with at least one price snapshot, per day of the
    window. Printed EVERY run, before any result, because the headline
    limitation of this tool is a data limitation and a reader who sees only
    the table will not know it applies.
    """
    path = str(market_db_path or settings.MARKET_DATA_DB)
    start_ts = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "select date(ts,'unixepoch') d,"
                "       count(distinct strftime('%H',ts,'unixepoch')) hrs,"
                "       count(*) snaps"
                "  from price_snapshots where ts >= ? and ts < ?"
                " group by d order by d",
                (start_ts, end_ts),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"price coverage: UNAVAILABLE ({exc}) -- treat every row below as unvalidated\n")
        return

    if not rows:
        print("price coverage: NO SNAPSHOTS in this window. Every row below is empty "
              "or settlement-only.\n")
        return

    thin = [d for d, hrs, _ in rows if hrs < 14]
    print("price coverage (hours of the day with any snapshot):")
    for d, hrs, snaps in rows:
        flag = "  <- peak-blind" if hrs < 14 else ""
        print(f"  {d}  {hrs:2}/24 h  {snaps:6} snaps{flag}")
    if thin:
        print(f"\n  {len(thin)} of {len(rows)} day(s) below 14h. A take-profit fires on a "
              f"PEAK, so\n  those days under-trigger EVERY take level and drag each row "
              f"toward its\n  settlement outcome -- widest distances worst. See the module "
              f"docstring.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stations", default=None,
                    help="Comma-separated ICAOs (default: every station in the registry)")
    ap.add_argument("--from", dest="from_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="to_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--takes", default="0.50,1,2,4,8,none",
                    help="Comma-separated lottery take distances as fractions of the risk "
                         "unit -- which for a lottery entry IS the entry price, so 2 means "
                         "'sell at 3x entry'. 'none' disables the lottery take entirely.")
    ap.add_argument("--market-db", dest="market_db", default=None)
    ap.add_argument("--stored", action="store_true",
                    help="Score the BOOK'S OWN stored entries instead of letting the engine "
                         "generate them. Holds the cohort fixed across rows, which the engine "
                         "path cannot -- at the cost of the portfolio effect. See "
                         "replay_stored(); the two modes bracket the answer, neither is it.")
    ap.add_argument("--live-db", dest="live_db", default=None,
                    help="Positions database for --stored (default: config.DB_PATH)")
    args = ap.parse_args()

    if args.live_db and not args.stored:
        ap.error("--live-db only means anything with --stored")

    stations = ([s.strip().upper() for s in args.stations.split(",")]
                if args.stations else list(config.STATIONS))
    pcts = [NO_TAKE if s.strip().lower() == "none" else float(s)
            for s in args.takes.split(",")]

    live = config.LOTTERY_PROFIT_TAKE_PCT
    if live not in pcts:
        pcts.append(live)
    pcts.sort()

    print(f"lottery take-profit sweep: {len(stations)} station(s), "
          f"{args.from_date} to {args.to_date}, {len(pcts)} distance(s)")
    print(f"lottery band is entry < {config.LOTTERY_PRICE_THRESHOLD}; live take there is "
          f"{live:.0%} of the risk unit "
          f"(tightened {config.TIGHTENED_LOTTERY_PROFIT_TAKE_PCT:.0%} after "
          f"{config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL}:00 local)")
    print(f"the stop is untouched throughout: lottery entries have none, and normal "
          f"entries keep {config.STOP_LOSS_PCT:.0%}")
    if args.stored:
        print("MODE: STORED ENTRIES -- the book's own positions, re-exited at each distance.\n"
              "      The cohort is identical in every row, so no difference below is entry\n"
              "      selection. It also models NO portfolio effect: capital freed by an early\n"
              "      exit finances nothing here. Compare against a run WITHOUT --stored --\n"
              "      the two bracket the answer and neither is it on its own.\n")
    else:
        print("MODE: ENGINE ENTRIES -- the simulator re-decides entries under a capital\n"
              "      constraint, so n can move between rows and part of any difference is\n"
              "      entry selection, not the take. Re-run with --stored for the other half.\n")

    print_coverage(args.from_date, args.to_date, args.market_db)

    results = sweep(stations, args.from_date, args.to_date, pcts, args.market_db,
                    stored=args.stored, db_path=args.live_db)

    print(f"LOTTERY COHORT (entry < {config.LOTTERY_PRICE_THRESHOLD})")
    print(f"{'take':>8} {'n':>4} {'took':>5} {'settled':>8} {'staked':>9} {'pnl':>9} "
          f"{'return':>8} {'drop1':>8} {'median':>8} {'sd':>8} {'worst':>7}")
    for pct in pcts:
        s = results[pct]["lottery"]
        label = "none" if pct >= NO_TAKE else f"{pct:.0%}"
        mark = "  <- live" if abs(pct - live) < 1e-9 else ""
        print(f"{label:>8} {s['n']:4} {s['took']:5} {s['settled']:8} {s['stake']:9.2f} "
              f"{s['pnl']:+9.2f} {s['ret']*100:+7.1f}% {s['ret_drop1']*100:+7.1f}% "
              f"{s['median']*100:+7.1f}% {s['sd']*100:7.1f}% {s['worst']*100:+6.0f}%{mark}")

    print(f"\nNORMAL COHORT (entry >= {config.LOTTERY_PRICE_THRESHOLD}) -- exit decisions "
          f"are identical across rows;\nany movement here is the portfolio coupling in the "
          f"docstring, not the take.")
    print(f"{'take':>8} {'n':>4} {'staked':>9} {'pnl':>9} {'return':>8}")
    for pct in pcts:
        s = results[pct]["normal"]
        label = "none" if pct >= NO_TAKE else f"{pct:.0%}"
        print(f"{label:>8} {s['n']:4} {s['stake']:9.2f} {s['pnl']:+9.2f} {s['ret']*100:+7.1f}%")

    # CENSORING WARNING, printed before anything else that follows the
    # tables, because it can invert their reading rather than merely widen
    # the error bars. Unresolved correlates with RECENT, not with LOSING.
    unres_lot = results[pcts[0]]["unresolved_lottery"]
    unres_norm = results[pcts[0]]["unresolved_normal"]
    closed_lot = results[pcts[0]]["lottery"]["n"]
    if unres_lot or unres_norm:
        print(f"\nEXCLUDED as unresolved at the window's end: {unres_lot} lottery, "
              f"{unres_norm} normal.")
    if unres_lot and unres_lot >= 0.5 * max(closed_lot, 1):
        print(f"  *** {unres_lot} unresolved against {closed_lot} closed means the LOTTERY "
              f"rows above describe a\n      MINORITY of that cohort. A position is "
              f"unresolved because its settlement is not\n      visible yet -- which tracks "
              f"how recent it is, not whether it won -- so the survivors\n      are censored "
              f"out while the already-settled losers are all counted. Do not read the\n"
              f"      ordering off this run. End the window earlier than the last settled "
              f"day. ***")
    if results[pcts[0]]["stations_run"] < len(stations):
        print(f"\nONLY {results[pcts[0]]['stations_run']} of {len(stations)} station(s) "
              f"replayed -- the rest raised and were skipped (see the lines above).")
    print("\nRelative comparison only: the engine makes its own entries, so no row is "
          "expected to equal\nthe live book's P&L (engine.py, DELIBERATE DIVERGENCES). "
          "Act on a distance only if its\nordering holds per station AND across windows "
          "AND on full-coverage days.")


if __name__ == "__main__":
    main()
