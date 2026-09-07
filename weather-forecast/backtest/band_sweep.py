"""
backtest/band_sweep.py

PURPOSE
-------
Answer the question config.ENTRY_PRICE_BLOCK_BAND's own measurement block ends
on: "Do not turn this on without a replay whose ordering holds per station AND
across windows."

That is an acceptance bar, and no tool in this repo could meet it. stop_sweep
and take_sweep report pooled totals; entry_bar_sweep does too. This one reports
PER STATION first and pooled second, and it is meant to be run over disjoint
windows.

WHY THE QUESTION IS OPEN AGAIN, WHICH IS NOT OBVIOUS
-----------------------------------------------------
The standing verdict -- "still off, and now that is a measurement, not a
caution" -- was replayed over 2026-08-06..08-24 and written up around 08-27.
config.HOLD_TO_SETTLEMENT_MODES landed on 2026-09-02 (2c8db40). So that verdict
was measured with the replay's STOP AND TAKE ARMED, under an exit regime this
book no longer runs.

That matters specifically for THIS gate. The entire case for the band is a
held-to-settlement one -- config.py: 0.15-0.25 is "THE ONLY BAND NEGATIVE ON
THE HELD-TO-SETTLEMENT COUNTERFACTUAL", and the argument is that a band losing
at its upper bound loses under every honest counterfactual, so no exit tuning
rescues it. When that was written, holding to settlement was a counterfactual.
It is now the policy. The measurement and the book finally agree, and nobody
has re-asked since.

WHY POOLED IS PRINTED SECOND, AND SMALLER
------------------------------------------
config.py records exactly how the pooled line misleads here, so this is not a
general preference. Blocking the band scored +$32.34 pooled and looked like a
win. It was not one: the blocked band lost $74.26 on its own, but the capital
the block freed flowed into the other bands and lost ~$42 there, the pooled
MEDIAN got worse (-42.6% -> -55.3%), and per station it was five better to
three worse on n of 6-34. "This band loses $74 so blocking saves $74" was wrong
by more than half.

So the pooled row is a summary of a decomposition, never the finding. This
module refuses to hand back one without the other -- see sweep().

WHAT THIS SWEEP ASSUMES ABOUT EXITS
-----------------------------------
The same as entry_bar_sweep, for the same reason and by importing the same
guard rather than restating it: every admitted position is HELD TO SETTLEMENT,
which is the shipped config.HOLD_TO_SETTLEMENT_MODES for the replay cohort.
This sweep varies which entries are ADMITTED; letting the exit rules fire would
read an entry question through them.

Both guards are imported, never copied. assert_cohort_holds() proves the
premise (the cohort really is held) and assert_measured_something() proves the
result (something was actually admitted) -- the second exists because the first
does not catch an empty run, which is how entry_bar_sweep's first execution
printed seven identical rows of zeros.

NO LIVE-CODE CHANGE IS NEEDED FOR THIS TOOL
-------------------------------------------
Both enforcement sites already read config.entry_price_is_blocked(): live at
entry_manager.py's Veto 00b, replay at backtest/entry_sim.py's gate 0b. This
module only sets the module global they both read, so there is nothing here to
deploy and nothing about the daemon's behaviour that changes.

DEPENDENCIES
------------
config.py (local), backtest/engine.py, backtest/entry_bar_sweep.py (local)
"""

import argparse
import statistics
from contextlib import contextmanager
from datetime import date, datetime

import config
from backtest import engine
# IMPORTED, NOT COPIED. Both are safety-critical and a second copy of either
# will drift from the original the first time one of them changes.
from backtest.entry_bar_sweep import (  # noqa: F401
    assert_cohort_holds,
    assert_measured_something,
    HOLD_ASSUMPTION_NOTE,
)


@contextmanager
def _blocked_band(band):
    """
    Set config.ENTRY_PRICE_BLOCK_BAND for the duration, then put it back.

    RESTORING IS LOAD-BEARING. config is process-global and the daemon imports
    the same module, so a leaked band is a live trading change made by a
    crashed analysis script -- and unlike a reordering, this one DELETES a
    slice of the book. Pinned on the raising path too.

    None is a cell, not a missing argument: it is the control arm, and the
    sweep is meaningless without it.
    """
    old = config.ENTRY_PRICE_BLOCK_BAND
    config.ENTRY_PRICE_BLOCK_BAND = band
    try:
        yield
    finally:
        config.ENTRY_PRICE_BLOCK_BAND = old


def parse_cells(spec: str):
    """
    "off,0.15-0.25,0.20-0.25" -> [None, (0.15, 0.25), (0.20, 0.25)].

    "off" is required in any real run and main() adds it if absent -- a table
    of blocked bands with nothing to compare them against cannot answer an
    ordering question.
    """
    cells = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower() in ("off", "none"):
            cells.append(None)
            continue
        low, _, high = part.partition("-")
        band = (float(low), float(high))
        if not 0.0 <= band[0] < band[1] <= 1.0:
            raise ValueError(
                f"band {part!r} must be low<high inside [0,1]; got {band}")
        cells.append(band)
    return cells


def _accumulate(positions):
    """Shared arithmetic for the pooled and per-station scorers."""
    stake = pnl = 0.0
    rets, prices = [], []
    n = 0
    for p in positions:
        if p.exit_price is None or not p.entry_price:
            continue
        got = (p.exit_price - p.entry_price) / p.entry_price
        stake += p.size_usd
        pnl += p.size_usd * got
        rets.append(got)
        prices.append(p.entry_price)
        n += 1
    return {
        "n": n, "stake": stake, "pnl": pnl,
        "ret": pnl / stake if stake else 0.0,
        "median": statistics.median(rets) if rets else 0.0,
        "mean_price": statistics.mean(prices) if prices else 0.0,
    }


def score(runs) -> dict:
    """
    Pooled scorecard. Deliberately NOT the headline -- see the module
    docstring for what reading this line alone did last time.

    The MEDIAN is carried alongside the return because it is the statistic
    that caught the problem before: blocking improved the pooled return and
    made the median worse, which is the signature of a change that helps the
    aggregate by concentrating the losses.
    """
    out = _accumulate([p for r in runs for p in r.closed_positions])
    out["unresolved"] = sum(len(r.unresolved_positions) for r in runs)
    return out


def score_by_station(runs) -> dict:
    """
    {station_icao: scorecard}. THE HEADLINE, because the acceptance bar this
    tool exists to serve is a per-station ordering.
    """
    by_station = {}
    for r in runs:
        by_station.setdefault(r.station_icao, []).extend(r.closed_positions)
    return {icao: _accumulate(ps) for icao, ps in sorted(by_station.items())}


def station_ordering(off_by_station, on_by_station):
    """
    (better, worse, tied) station lists comparing a blocked cell against the
    off cell, on return per dollar staked.

    Reports the SPLIT rather than a verdict, because the last reading of this
    gate was five better to three worse and config.py records that as "not a
    result" -- a boolean would have rounded it into one.

    A station present on only one side is EXCLUDED, not counted as a win. It
    traded under one cell and not the other, which is not evidence about the
    ordering, and counting it would be the easiest way for this tool to
    flatter the band.
    """
    better, worse, tied = [], [], []
    for icao in sorted(set(off_by_station) & set(on_by_station)):
        off_ret = off_by_station[icao]["ret"]
        on_ret = on_by_station[icao]["ret"]
        if on_ret > off_ret:
            better.append(icao)
        elif on_ret < off_ret:
            worse.append(icao)
        else:
            tied.append(icao)
    return better, worse, tied


def sweep(stations, start: date, end: date, cells, market_db_path=None) -> dict:
    """
    {cell: {"pooled": scorecard, "by_station": {icao: scorecard}}}.

    Returns both views together and never one alone -- the pooled figure is
    only safe to read as a summary of the decomposition beside it.
    """
    assert_cohort_holds()
    out = {}
    for band in cells:
        runs = []
        with _blocked_band(band):
            for icao in stations:
                try:
                    runs.append(engine.run(icao, start, end, market_db_path=market_db_path))
                except Exception as exc:  # noqa: BLE001 -- one bad station must not void the sweep
                    print(f"  [band_sweep] {icao} failed at band={band}: "
                          f"{type(exc).__name__}: {str(exc)[:100]}")
        out[band] = {"pooled": score(runs), "by_station": score_by_station(runs)}
    assert_measured_something({k: v["pooled"] for k, v in out.items()})
    return out


def _label(band) -> str:
    return "off" if band is None else f"{band[0]:.2f}-{band[1]:.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stations", default=None,
                    help="Comma-separated ICAOs (default: every station in the registry)")
    ap.add_argument("--from", dest="from_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="to_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--bands", default="off,0.15-0.25,0.15-0.30,0.20-0.25",
                    help="Comma-separated bands as low-high, plus 'off' for the "
                         "control arm. 0.15-0.25 is the motivated band; the other "
                         "two test whether it is knife-edge.")
    ap.add_argument("--market-db", dest="market_db", default=None)
    args = ap.parse_args()

    stations = ([s.strip().upper() for s in args.stations.split(",")]
                if args.stations else list(config.STATIONS))
    cells = parse_cells(args.bands)
    if None not in cells:
        cells.insert(0, None)

    print(f"band sweep: {len(stations)} station(s), {args.from_date} to "
          f"{args.to_date}, {len(cells)} cell(s)")
    print(f"live setting is ENTRY_PRICE_BLOCK_BAND = "
          f"{config.ENTRY_PRICE_BLOCK_BAND!r}\n")
    print(HOLD_ASSUMPTION_NOTE + "\n")

    results = sweep(stations, args.from_date, args.to_date, cells, args.market_db)
    off = results[None]["by_station"]

    # PER STATION FIRST. See the module docstring: the pooled line is what
    # misled last time, and printing it first would invite the same reading.
    for band in cells:
        if band is None:
            continue
        on = results[band]["by_station"]
        better, worse, tied = station_ordering(off, on)
        print(f"\n=== band {_label(band)} vs off, PER STATION "
              f"(return per dollar staked, held to settlement) ===")
        print(f"{'station':>9} {'off n':>6} {'off ret':>9} {'on n':>6} {'on ret':>9} {'':>7}")
        for icao in sorted(set(off) & set(on)):
            o, n_ = off[icao], on[icao]
            mark = ("better" if n_["ret"] > o["ret"]
                    else "WORSE" if n_["ret"] < o["ret"] else "tied")
            print(f"{icao:>9} {o['n']:6} {o['ret']*100:+8.1f}% "
                  f"{n_['n']:6} {n_['ret']*100:+8.1f}% {mark:>7}")
        only_off = sorted(set(off) - set(on))
        only_on = sorted(set(on) - set(off))
        if only_off or only_on:
            print(f"  excluded, traded under one cell only: "
                  f"off-only {only_off or '-'}, on-only {only_on or '-'}")
        print(f"  ORDERING: {len(better)} better, {len(worse)} worse, "
              f"{len(tied)} tied"
              f"{'  <- ' + ', '.join(worse) + ' got worse' if worse else ''}")

    print(f"\n=== pooled (a summary of the tables above, not the finding) ===")
    print(f"{'band':>11} {'n':>6} {'staked':>9} {'pnl':>9} {'return':>8} "
          f"{'median':>8} {'meanpx':>7}")
    for band in cells:
        p = results[band]["pooled"]
        print(f"{_label(band):>11} {p['n']:6} {p['stake']:9.2f} {p['pnl']:+9.2f} "
              f"{p['ret']*100:+7.1f}% {p['median']*100:+7.1f}% {p['mean_price']:7.3f}")

    print("\nTHE BAR FOR ACTING (config.py, beside ENTRY_PRICE_BLOCK_BAND): the")
    print("ordering must hold PER STATION and ACROSS WINDOWS. A pooled win with a")
    print("split per-station ordering is what was already rejected once, at five")
    print("better to three worse. Run a second, DISJOINT window before reading a")
    print("result into any of this.")


if __name__ == "__main__":
    main()
