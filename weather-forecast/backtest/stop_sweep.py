"""
backtest/stop_sweep.py

PURPOSE
-------
Answer the question stop_loss_audit.py cannot: what would a DIFFERENT stop
distance have done?

The audit scores one counterfactual -- this stop did not fire -- and is
explicit that its "held" column is an upper bound, because it holds every
loser to settlement and charges nothing for the capital that would have
tied up. It cannot price a wider stop, because widening one changes WHICH
positions stop at all, and a position that survives a wider stop may still
hit take-profit later rather than settling.

That needs a full replay of entries, exits and the schedule, which is what
engine.run() already is. This module just drives it repeatedly with the
stop distance varied, so every threshold is scored by the same simulator
over the same prices.

WHY THIS IS NOT A NAIVE PRICE-PATH REPLAY
-----------------------------------------
The obvious version of this -- walk each stored position's recorded price
path and apply a candidate stop -- was tried on 2026-08-18 and FAILED
validation: at the live setting it produced +381 USD against an actual
+33, about 12x out. Two omissions did it. It modelled only the loose
thresholds, ignoring the post-10:00 take-profit tightening that halves
every winner; and it ignored the trailing stop, which was live until
2026-08-17 and closed 14 paper positions. Restricting the path to ticks
the daemon actually saw barely moved the number, so tick resolution was
not the cause.

Going through engine.run() avoids all of that by construction: exits go
through the real risk_manager.evaluate_exit() at the real simulated hour,
so whatever exit mechanisms exist are the ones being modelled.

WHAT THIS CAN AND CANNOT CLAIM
------------------------------
The engine makes its OWN entry decisions rather than replaying the stored
ones, so its absolute P&L is NOT expected to equal the live book's, and
the live row here is a sanity check rather than a parity assertion. See
engine.py's DELIBERATE DIVERGENCES list for why they differ.

What the comparison IS good for is RELATIVE: every row sees identical
prices, identical entries at the point the stop first matters, and differs
only in the stop distance. Read the ordering and the spread between rows,
not the absolute level of any one of them.

WHAT STOP_LOSS_PCT MEANS CHANGED ON 2026-08-20
----------------------------------------------
config.STOP_EXEMPT_ABOVE_PRICE now exempts entries at or above 0.45 from
the stop entirely, so this sweep no longer varies a distance that applies
to every position -- it varies one that applies inside [LOTTERY_PRICE_THRESHOLD,
STOP_EXEMPT_ABOVE_PRICE). Rows produced before that commit are NOT
comparable to rows produced after it, and the per-station table in the
first run below is a reading of the OLD, unconditional stop.

That also retires the specific puzzle in the first run: WMKK scoring best
at the tightest distance. Conditioning the stop on entry price was never
one of the settings this tool sweeps, and the paper book measured directly
against settlement says WMKK's stop is 83% precise below 0.30 and 33% at
or above 0.45. Sweeping one number across both bands averages those.

THE ALIAS GOTCHA
----------------
config.TIGHTENED_STOP_LOSS_PCT is written as `= STOP_LOSS_PCT`, which
binds the VALUE at import. Rebinding config.STOP_LOSS_PCT alone therefore
leaves the tightened one at the old number and silently reintroduces a
tightening this sweep is not trying to model. Both are always set together
below.

FIRST RUN, 2026-08-18: IT DOES NOT SUPPORT MOVING THE STOP
----------------------------------------------------------
Over 2026-08-10..17, all stations, ~80 closed positions per row, the
aggregate looked clean -- 15% -1.3%, 30% (live) +8.8%, 45% +8.9%, 60%
+12.2%, 80% +11.3%, none +10.7% -- i.e. "widen the stop to about 60%".

Three checks say do not act on that:

  - PER STATION the optimum is scattered across the entire range. Five
    stations score best at the TIGHTEST 15% (WMKK, RPLL, RCSS, ZGGG,
    ZGSZ), four at NO STOP AT ALL (RJTT, RKPK, VHHH, ZSPD), two at the
    current 30% (WSSS, RKSI). There is no common distance.
  - The aggregate optimum is carried by two stations. ZSPD alone runs
    +1.7% at 30% against +75.4% at 60%; without it and VHHH the curve
    flattens.
  - The EARLIER window (2026-08-02..09) REVERSES the ordering -- tighter
    scores better there. That window replays only 6-8 closed positions
    because today's bias gates keep most stations collection-only in it,
    so it does not refute the later window; it just fails to confirm it.

Per-station n is 4-13 and per-trade sd is over 100%, so none of these
differences is separable from noise. The tool is the deliverable; the
answer needs more data. Re-run it when the post-fix book is several times
larger, and treat a recommendation as earned only if the ordering holds
per station AND across windows.

What DID survive this analysis is a different change: the stop TIGHTENING
was removed on the same day (config.TIGHTENED_STOP_LOSS_PCT), on
stop_loss_audit evidence that is direct rather than simulated.

DEPENDENCIES
------------
config.py, risk_manager.py (local), backtest/engine.py (local)
"""

import argparse
import statistics
from contextlib import contextmanager
from datetime import date, datetime

import config
from backtest import engine

# A stop this far away cannot fire: the risk unit is min(entry, 1-entry),
# so one whole unit below entry is already price 0 for any entry under 0.50.
NO_STOP = 99.0


@contextmanager
def _stop_distance(pct: float):
    """Set both stop constants for the duration, then put them back."""
    old_loose = config.STOP_LOSS_PCT
    old_tight = config.TIGHTENED_STOP_LOSS_PCT
    config.STOP_LOSS_PCT = pct
    config.TIGHTENED_STOP_LOSS_PCT = pct
    try:
        yield
    finally:
        config.STOP_LOSS_PCT = old_loose
        config.TIGHTENED_STOP_LOSS_PCT = old_tight


def _score(runs) -> dict:
    """Aggregate closed positions across a set of BacktestRuns."""
    stake = pnl = 0.0
    rets = []
    n_stopped = n = 0
    for r in runs:
        for p in r.closed_positions:
            if p.exit_price is None or not p.entry_price:
                continue
            got = (p.exit_price - p.entry_price) / p.entry_price
            stake += p.size_usd
            pnl += p.size_usd * got
            rets.append(got)
            n += 1
            if p.status == "closed_stop_loss":
                n_stopped += 1
    return {
        "n": n, "stake": stake, "pnl": pnl,
        "ret": pnl / stake if stake else 0.0,
        "sd": statistics.stdev(rets) if len(rets) > 1 else 0.0,
        "worst": min(rets) if rets else 0.0,
        "stopped": n_stopped,
        "unresolved": sum(len(r.unresolved_positions) for r in runs),
    }


def sweep(stations, start: date, end: date, stop_pcts, market_db_path=None) -> dict:
    """{stop_pct: scorecard} over every station, replayed once per distance."""
    out = {}
    for pct in stop_pcts:
        runs = []
        with _stop_distance(pct):
            for icao in stations:
                try:
                    runs.append(engine.run(icao, start, end, market_db_path=market_db_path))
                except Exception as exc:  # noqa: BLE001 -- one bad station must not void the sweep
                    print(f"  [stop_sweep] {icao} failed at stop={pct}: "
                          f"{type(exc).__name__}: {str(exc)[:100]}")
        out[pct] = _score(runs)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stations", default=None,
                    help="Comma-separated ICAOs (default: every station in the registry)")
    ap.add_argument("--from", dest="from_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="to_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--stops", default="0.15,0.30,0.45,0.60,0.80,none",
                    help="Comma-separated stop distances as fractions of the risk unit; "
                         "'none' disables the stop entirely")
    ap.add_argument("--market-db", dest="market_db", default=None)
    args = ap.parse_args()

    stations = ([s.strip().upper() for s in args.stations.split(",")]
                if args.stations else list(config.STATIONS))
    pcts = [NO_STOP if s.strip().lower() == "none" else float(s)
            for s in args.stops.split(",")]

    live = config.STOP_LOSS_PCT
    if live not in pcts:
        pcts.append(live)
    pcts.sort()

    print(f"stop sweep: {len(stations)} station(s), {args.from_date} to {args.to_date}, "
          f"{len(pcts)} distance(s)")
    print(f"live setting is {live:.0%} of the risk unit; take-profit "
          f"{config.PROFIT_TAKE_PCT:.0%} (tightened {config.TIGHTENED_PROFIT_TAKE_PCT:.0%} "
          f"after {config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL}:00) is held constant throughout\n")

    results = sweep(stations, args.from_date, args.to_date, pcts, args.market_db)

    print(f"\n{'stop':>8} {'closed':>7} {'stopped':>8} {'staked':>9} {'pnl':>9} "
          f"{'return':>8} {'sd/trade':>9} {'worst':>7}")
    for pct in pcts:
        s = results[pct]
        label = "none" if pct >= NO_STOP else f"{pct:.0%}"
        mark = "  <- live" if abs(pct - live) < 1e-9 else ""
        print(f"{label:>8} {s['n']:7} {s['stopped']:8} {s['stake']:9.2f} {s['pnl']:+9.2f} "
              f"{s['ret']*100:+7.1f}% {s['sd']*100:8.1f}% {s['worst']*100:+6.0f}%{mark}")

    unres = results[pcts[0]]["unresolved"]
    if unres:
        print(f"\n{unres} position(s) unresolved at the end of the window are excluded "
              f"from every row.")
    print("\nRelative comparison only: the engine makes its own entries, so no row is "
          "expected to equal the live book's P&L (engine.py, DELIBERATE DIVERGENCES).")


if __name__ == "__main__":
    main()
