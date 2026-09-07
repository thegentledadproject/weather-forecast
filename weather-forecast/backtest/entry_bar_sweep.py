"""
backtest/entry_bar_sweep.py

PURPOSE
-------
Answer the question 9367907 left open: should the entry admission bar be
keyed on net_ev_per_dollar (edge divided by price) or on net_ev_per_share
(edge in dollars per share)?

That commit measured net_ev_per_dollar on the live book, scored
hold-to-settlement, and found it INVERTED rather than weakly informative --
the HIGHEST quintile returns -31.7% while the second-LOWEST returns +30.6%,
with the top quintile's mean price at 0.114. It changed the SORT and
deliberately left the BAR alone, because which candidates surface is a
trading change and this project's rule is that entry-gate changes need
replay evidence first. This module produces that evidence.

WHY A REPLAY AND NOT A RE-SCORE OF THE STORED BOOK
--------------------------------------------------
Re-scoring stored positions can only ever answer "which of the entries we
actually made would this bar have kept". It cannot show the entries a
different bar would have ADMITTED, and those are the whole question: the
per-share basis is expected to let through expensive buckets the ratio bar
screens out today. Only a replay that re-runs the decision has them.

It also has to reallocate capital honestly. ENTRY_PRICE_BLOCK_BAND is the
cautionary tale sitting in config.py: blocking a band looked like a $74 win
in isolation and was a $32 win in the portfolio, because the freed capital
flowed into the other bands and lost ~$42 there. engine.run() applies the
same budget and exposure caps live does, so this sweep gets that arithmetic
by construction rather than by assumption.

WHAT THIS SWEEP ASSUMES ABOUT EXITS, AND WHY IT IS THE OPPOSITE OF stop_sweep
-----------------------------------------------------------------------------
stop_sweep and take_sweep were inert from 2026-09-02 to 2026-09-07 because
their replay cohort was exempt from the rule they varied, and they failed by
printing a table whose every row was identical. The fix there was to ARM the
cohort. Here the correct setting is the shipped one, for a reason rather
than by luck: this sweep varies which entries are ADMITTED, so it wants
every admitted position carried to settlement. cohort_monitor has already
measured that entry selection is +18.4% held to settlement and that the stop
and the take turn it into -7.3% -- so a sweep that let the exit rules fire
would be reading an entry question through the noisiest rule in the book.

config.HOLD_TO_SETTLEMENT_MODES = ("paper",) already holds the replay cohort,
because engine.py builds every position is_paper=True on the Position
default mode. This module therefore leaves that tuple alone, and
assert_cohort_holds() PROVES it before any row is scored -- the discipline
the other two sweeps lacked, pointed the other way.

WHAT THIS CAN AND CANNOT CLAIM
------------------------------
The engine makes its own entry decisions, so absolute P&L is NOT expected to
equal the live book's. Read the ORDERING and the SPREAD between rows.

The two bases are not on one scale and their thresholds are not
interchangeable: a 0.15 ratio is about 4.5c per share at the median entry
price, and about 0.6c at price 0.04. Every cell is therefore (basis, bar),
never a bare number.

And the bar this project set for acting on a replayed threshold is
explicitly higher than a pooled number: the ordering must hold PER STATION
and ACROSS WINDOWS. ENTRY_PRICE_BLOCK_BAND failed exactly that test and
still ships off.

DEPENDENCIES
------------
config.py, risk_manager.py (local), backtest/engine.py (local)
"""

# WHAT THIS SWEEP ASSUMES ABOUT EXITS, stated in its own output because the
# assumption decides what the answer means (the P1-10 discipline, applied to
# the premise this tool actually rests on).
HOLD_ASSUMPTION_NOTE = (
    "EXIT ASSUMPTION: every admitted position is HELD TO SETTLEMENT. The stop "
    "and the take-profit are off for the replay cohort (the shipped "
    "config.HOLD_TO_SETTLEMENT_MODES), deliberately and not by accident -- this "
    "sweep varies which entries are ADMITTED, and cohort_monitor has measured "
    "entry selection at +18.4% held against -7.3% as traded, so letting the exit "
    "rules fire would read an entry question through them. Cells scored here are "
    "what the SELECTION is worth, not what the book would have booked. Proved "
    "before scoring by assert_cohort_holds()."
)


import argparse
import statistics
from contextlib import contextmanager
from datetime import date, datetime

import config
import risk_manager
from models import Position
from backtest import engine


def assert_cohort_holds() -> bool:
    """
    Prove the replay cohort is exempt from the price exits, before scoring.

    THE LESSON THIS ENCODES. stop_sweep and take_sweep answered nothing for
    five days because their cohort was in the opposite state to the one their
    output assumed, and nothing checked. The check is cheap and it is the
    difference between a null result and a fake one -- so it runs every time,
    on a position shaped exactly as engine.py builds them.

    Raises AssertionError rather than returning False: a sweep that cannot
    establish its own premise must not print a table.
    """
    probe = Position(
        position_id="entry-bar-sweep-probe", station_icao="WSSS",
        target_date="2026-09-07", bucket_c=32, side="YES",
        entry_price=0.40, size_usd=10.0,
        entry_time="2026-09-07T00:00:00Z", high_water_mark=0.40,
        is_paper=True,
    )
    # 0.05 is an 87% loss on a 0.40 entry -- past every stop distance. 0.95
    # is a 137% gain -- past every take-profit target. If either exits, the
    # cohort is armed and the rows would differ by exit rules, not entries.
    for price, what in ((0.05, "stop"), (0.95, "take-profit")):
        reason = risk_manager.evaluate_exit(
            probe, current_price=price, local_hour=6).reason
        assert reason == "hold", (
            f"cohort is NOT held to settlement: a replay-shaped position at "
            f"{price} returned {reason!r}, so this sweep would score the {what} "
            f"rule as well as the entry bar. config.HOLD_TO_SETTLEMENT_MODES is "
            f"{config.HOLD_TO_SETTLEMENT_MODES!r}."
        )
    return True


@contextmanager
def _entry_bar(basis: str, bar=None):
    """
    Set the admission basis (and optionally the bar every entry window reads)
    for the duration, then put both back.

    RESTORING IS LOAD-BEARING, NOT BOOKKEEPING. config is process-global and
    the daemon imports the same module, so a leaked basis is a live trading
    change made by a crashed analysis script. Pinned on the raising path too.

    The bar travels through config.SCHEDULE_WINDOWS because that is where the
    replay reads it: simclock hands engine.run() the active window's
    min_net_ev, exactly as scheduler.determine_window() does live. Windows
    that surface no entries carry None and are left alone -- overwriting
    those would hand a bar to a "closed" or "collection" window and change
    what the replay does, not just what it demands.

    DELIBERATELY NOT TOUCHED: config.HOLD_TO_SETTLEMENT_MODES. See this
    module's docstring -- stop_sweep empties it and this one must not.
    """
    if basis not in config.ENTRY_BAR_BASES:
        raise ValueError(
            f"basis must be one of {config.ENTRY_BAR_BASES}, got {basis!r}")

    if bar is not None and config.ENABLE_MARKET_OPEN_WINDOW:
        # determine_window() prepends config.MARKET_OPEN_WINDOW, which carries
        # its own min_net_ev and is NOT in the list rewritten below -- so the
        # replay would run two bars and the table would print one. Refuse
        # rather than handle a path that ships off and nothing exercises.
        raise RuntimeError(
            "ENABLE_MARKET_OPEN_WINDOW is on: config.MARKET_OPEN_WINDOW carries "
            "its own min_net_ev and this override does not reach it, so the "
            "sweep would score a mixed bar and label it a single one."
        )

    old_basis = config.ENTRY_BAR_BASIS
    old_windows = config.SCHEDULE_WINDOWS
    config.ENTRY_BAR_BASIS = basis
    if bar is not None:
        config.SCHEDULE_WINDOWS = [
            w if w[6] is None else (w[:6] + (bar,) + w[7:])
            for w in old_windows
        ]
    try:
        yield
    finally:
        config.ENTRY_BAR_BASIS = old_basis
        config.SCHEDULE_WINDOWS = old_windows


def parse_cells(spec: str):
    """
    "ratio:0.15,per_share:0.045" -> [("ratio", 0.15), ("per_share", 0.045)].

    A cell is always (basis, bar) because the two thresholds are not on the
    same scale: 0.15 as a ratio and 0.15 per share are different rules by an
    order of magnitude, and a table whose rows were bare numbers could not be
    read at all.
    """
    cells = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        basis, _, bar = part.partition(":")
        basis = basis.strip()
        if basis not in config.ENTRY_BAR_BASES:
            raise ValueError(
                f"unknown basis {basis!r} in cell {part!r}; "
                f"expected one of {config.ENTRY_BAR_BASES}")
        cells.append((basis, float(bar)))
    return cells


def _score(runs) -> dict:
    """
    Aggregate closed positions across a set of BacktestRuns.

    n_price_exits should be ZERO on a correctly held cohort. It is reported
    rather than asserted so a partial breakdown shows up in the table as a
    number instead of killing a long run at the last station.
    """
    stake = pnl = 0.0
    rets = []
    prices = []
    n_price_exits = n = 0
    for r in runs:
        for p in r.closed_positions:
            if p.exit_price is None or not p.entry_price:
                continue
            got = (p.exit_price - p.entry_price) / p.entry_price
            stake += p.size_usd
            pnl += p.size_usd * got
            rets.append(got)
            prices.append(p.entry_price)
            n += 1
            if p.status in ("closed_stop_loss", "closed_take_profit",
                            "closed_trailing_stop"):
                n_price_exits += 1
    return {
        "n": n, "stake": stake, "pnl": pnl,
        "ret": pnl / stake if stake else 0.0,
        "sd": statistics.stdev(rets) if len(rets) > 1 else 0.0,
        "worst": min(rets) if rets else 0.0,
        "mean_price": statistics.mean(prices) if prices else 0.0,
        "price_exits": n_price_exits,
        "unresolved": sum(len(r.unresolved_positions) for r in runs),
    }


def sweep(stations, start: date, end: date, cells, market_db_path=None) -> dict:
    """{(basis, bar): scorecard} over every station, replayed once per cell."""
    assert_cohort_holds()
    out = {}
    for basis, bar in cells:
        runs = []
        with _entry_bar(basis, bar):
            for icao in stations:
                try:
                    runs.append(engine.run(icao, start, end, market_db_path=market_db_path))
                except Exception as exc:  # noqa: BLE001 -- one bad station must not void the sweep
                    print(f"  [entry_bar_sweep] {icao} failed at {basis}:{bar}: "
                          f"{type(exc).__name__}: {str(exc)[:100]}")
        out[(basis, bar)] = _score(runs)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stations", default=None,
                    help="Comma-separated ICAOs (default: every station in the registry)")
    ap.add_argument("--from", dest="from_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="to_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--cells",
                    default="ratio:0.10,ratio:0.15,ratio:0.25,"
                            "per_share:0.02,per_share:0.03,per_share:0.045,per_share:0.06",
                    help="Comma-separated basis:bar cells. The ratio arm is the "
                         "control; per_share:0.045 is roughly the like-for-like "
                         "cell against the live ratio:0.15.")
    ap.add_argument("--market-db", dest="market_db", default=None)
    args = ap.parse_args()

    stations = ([s.strip().upper() for s in args.stations.split(",")]
                if args.stations else list(config.STATIONS))
    cells = parse_cells(args.cells)

    print(f"entry bar sweep: {len(stations)} station(s), {args.from_date} to "
          f"{args.to_date}, {len(cells)} cell(s)")
    # Derived, not indexed: SCHEDULE_WINDOWS has been reshaped twice this
    # month (the pre_poll windows went on 2026-08-30, the secondary entry
    # ramp on 2026-08-17), and a hardcoded index would keep printing a
    # confident number off the wrong row.
    live_bars = sorted({w[6] for w in config.SCHEDULE_WINDOWS if w[6] is not None})
    print(f"live setting is {config.ENTRY_BAR_BASIS}, bar(s) "
          f"{', '.join(f'{b:g}' for b in live_bars)} across the entry window(s)\n")
    print(HOLD_ASSUMPTION_NOTE + "\n")

    results = sweep(stations, args.from_date, args.to_date, cells, args.market_db)

    print(f"\n{'basis':>10} {'bar':>8} {'entries':>8} {'staked':>9} {'pnl':>9} "
          f"{'return':>8} {'meanpx':>7} {'sd/trade':>9} {'pxexits':>8}")
    for basis, bar in cells:
        s = results[(basis, bar)]
        print(f"{basis:>10} {bar:>8.3f} {s['n']:8} {s['stake']:9.2f} {s['pnl']:+9.2f} "
              f"{s['ret']*100:+7.1f}% {s['mean_price']:7.3f} {s['sd']*100:8.1f}% "
              f"{s['price_exits']:8}")

    print("\nRead the ORDERING and the SPREAD, not the level -- the engine makes its")
    print("own entry decisions, so absolute P&L is not the live book's. A nonzero")
    print("pxexits column means the cohort was not fully held and the rows are not")
    print("a clean entry comparison.")


if __name__ == "__main__":
    main()
