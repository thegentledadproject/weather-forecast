"""
stop_loss_audit.py -- what did the stop-loss rule cost, and how much of that
was the 10:00-local tightening?

READ-ONLY. Loads closed positions and observations through storage.py and
prints. It never writes a row, never touches the network, and never asks the
exchange for anything.

WHAT IT MEASURES
----------------
risk_manager.evaluate_exit() stops out a position when

    entry_price - bid  >=  stop_pct * risk_unit(entry_price)

and `stop_pct` is not constant: config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL swaps
STOP_LOSS_PCT for the smaller TIGHTENED_STOP_LOSS_PCT for the rest of the
day. So the same bid can be a hold at 09:59 and a stop at 10:01 with nothing
having happened in the market. This script sorts every closed stop-loss into
three buckets:

  before_tighten     fired while the loose threshold was active
  would_fire_anyway  fired after the tightening, at a distance the LOOSE
                     threshold would also have triggered on -- the tightening
                     changed nothing for these
  tightening_only    fired after the tightening, at a distance the loose
                     threshold would NEVER have triggered on. These exist
                     only because of the hour.

Then, for each group, it compares what was realized against what the position
would have paid HELD TO SETTLEMENT.

WHAT THE "HELD" COLUMN IS, AND WHAT IT IS NOT
---------------------------------------------
It is: this position, held to resolution, paid 1.0 or 0.0 per share, with no
fee (redeeming a resolved position is not a taker fill -- see
executor.close_position()).

It is NOT "what the book would have earned with stops disabled." A stop frees
capital, and under the exposure and per-cycle budget caps that capital funded
later entries -- including winners. This comparison holds every loser to
settlement without charging for the entries they would have blocked, so the
difference is an UPPER BOUND on what stopping cost, not an estimate of it.
The defensible reading is the narrow one: were the positions the stop cut, on
average, cut into losses that resolution reversed? Answering the portfolio
question needs the capital constraint modeled, which is backtest/engine.py's
job, not this script's.

THREE PLACES THE DATA IS WEAKER THAN IT LOOKS
---------------------------------------------
1. GROSS vs NET exit price. executor.close_position() stores exit_price NET of
   the exit fee and preserves the gross quote only in the exit_reason text.
   The stop TRIGGERED on the gross bid, so that is what the distance must be
   measured against -- this parses it back out of exit_reason and falls back
   to the stored price for rows written before the fee was modeled (66d3075).
2. CLAMPED SETTLEMENT. bucket_for_temp() clamps into the station's CONFIGURED
   bucket window, and config's bounds are documented as a drifting
   cross-check, not the live market's bounds (models.StationConfig). A
   settled reading outside that window gets pinned to an edge, so its
   win/lose label is a guess. Those rows are counted separately and the
   comparison is reprinted without them.
3. TRAILING STOPS ARE EXCLUDED. Only status == "closed_stop_loss" is scored.
   The stored closed_trailing_stop rows ran under a different rule (since
   removed, b528ead); scoring them against STOP_LOSS_PCT measures a threshold
   they never obeyed.

Every threshold, the risk unit and the tightening hour are READ FROM
config/risk_manager, never restated here -- change the rule and this audit
re-scores itself against the new one.

USAGE
-----
    python stop_loss_audit.py                    # paper track, every station
    python stop_loss_audit.py --track live       # real-money track
    python stop_loss_audit.py --station WMKK
    python stop_loss_audit.py --no-detail        # summary only

DEPENDENCIES
------------
config.py, storage.py, risk_manager.py, backtest/resolution.py (local)
"""

import argparse
import re
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import config
import risk_manager
import storage
from backtest.resolution import bucket_for_temp
from models import Position

# executor.close_position() writes "... gross 0.3700 - exit fee 0.0117/share
# = net 0.3583" into exit_reason. That gross figure is the bid the exit
# decision actually saw.
_GROSS_RE = re.compile(r"gross\s+([0-9]*\.?[0-9]+)")

BEFORE_TIGHTEN = "before_tighten"
WOULD_FIRE_ANYWAY = "would_fire_anyway"
TIGHTENING_ONLY = "tightening_only"

_GROUP_LABELS = {
    BEFORE_TIGHTEN: "stopped before the tightening hour",
    WOULD_FIRE_ANYWAY: "stopped after it, but reached the loose distance too",
    TIGHTENING_ONLY: "TIGHTENING-ONLY stops",
}


def gross_exit_price(position: Position) -> Optional[float]:
    """The bid the stop triggered on -- gross, before the exit fee.

    See caveat 1 in the module docstring: exit_price is stored net, and the
    gross quote survives only in the exit_reason text. Rows predating the fee
    model carry no note and stored gross directly, so the stored price is the
    right fallback for exactly those.
    """
    match = _GROSS_RE.search(position.exit_reason or "")
    if match:
        return float(match.group(1))
    return position.exit_price


def exit_local_hour(position: Position) -> Optional[int]:
    """Hour 0-23 in the POSITION'S OWN station timezone, which is what
    risk_manager's tightening is evaluated against (position_manager.
    _local_hour_for). A UTC hour would misfile every non-UTC+8 station."""
    if not position.exit_time:
        return None
    try:
        dt = datetime.fromisoformat(str(position.exit_time))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        offset = config.get_station(position.station_icao).utc_offset_hours
    except KeyError:
        offset = config.LOCAL_UTC_OFFSET_HOURS
    return int((dt.timestamp() + offset * 3600) // 3600 % 24)


def classify(position: Position) -> Optional[str]:
    """Which of the three buckets this stop-loss belongs in, or None if the
    row is not a scoreable stop-loss."""
    if position.status != "closed_stop_loss" or not position.entry_price:
        return None
    bid = gross_exit_price(position)
    hour = exit_local_hour(position)
    if bid is None or hour is None:
        return None
    if hour < config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL:
        return BEFORE_TIGHTEN
    loose_distance = config.STOP_LOSS_PCT * risk_manager.risk_unit(position.entry_price)
    return (WOULD_FIRE_ANYWAY if (position.entry_price - bid) >= loose_distance
            else TIGHTENING_ONLY)


def loose_trigger_price(position: Position) -> float:
    """The bid the LOOSE threshold was waiting for -- the level a
    tightening-only stop never reached."""
    return position.entry_price - config.STOP_LOSS_PCT * risk_manager.risk_unit(position.entry_price)


def realized_pnl(position: Position) -> Optional[float]:
    """Realized dollars, on the same basis as
    paper_trading_report.summarize_positions(): stake x return, net exit fee."""
    if position.exit_price is None or not position.entry_price:
        return None
    return position.size_usd * (position.exit_price - position.entry_price) / position.entry_price


def load_settlements(stations: List[str], cutoff: date) -> Dict[Tuple[str, str], float]:
    """(icao, isodate) -> settled max temp, from each station's OWN
    resolution_grade_source. A reading under any other source is not
    settlement truth (Hong Kong settles on HKO, not METAR; Karachi's METAR is
    proxy-grade and settles nothing), so anything else is dropped here rather
    than being quietly scored."""
    settled = {}
    for icao in stations:
        station = config.STATIONS.get(icao)
        if station is None:
            continue
        for obs in storage.load_observations_since(icao, cutoff):
            if obs.source == station.resolution_grade_source:
                settled[(icao, obs.target_date.isoformat())] = float(obs.max_temp_c)
    return settled


def hold_to_settlement(position: Position, settled: Dict[Tuple[str, str], float]):
    """(pnl_usd, won, clamped) had this position been held to resolution.

    Returns (None, None, False) when no settlement-grade reading exists for
    that station and date -- unknown, which must never be scored as a loss.
    `clamped` marks the rows described in caveat 2.
    """
    key = (position.station_icao, position.target_date.isoformat())
    temp = settled.get(key)
    if temp is None:
        return None, None, False
    station = config.STATIONS[position.station_icao]
    bucket = bucket_for_temp(temp, station.bucket_min_c, station.bucket_max_c,
                             station.bucket_edge_mode)
    won = (bucket == position.bucket_c) if position.side == "YES" else (bucket != position.bucket_c)
    shares = position.size_usd / position.entry_price
    clamped = not (station.bucket_min_c < temp < station.bucket_max_c)
    return shares * (1.0 if won else 0.0) - position.size_usd, won, clamped


def audit(stations: List[str], is_paper: bool = True, limit: int = 1000) -> dict:
    """Load, classify and score. Pure of printing so a caller (or a test) can
    assert on the numbers."""
    positions = []
    for icao in stations:
        positions.extend(storage.load_position_history(icao, limit=limit, is_paper=is_paper))

    groups = {BEFORE_TIGHTEN: [], WOULD_FIRE_ANYWAY: [], TIGHTENING_ONLY: []}
    for position in positions:
        group = classify(position)
        if group:
            groups[group].append(position)

    cutoff = min((p.target_date for p in positions), default=date.today())
    settled = load_settlements(stations, cutoff)
    return {
        "closed_n": sum(1 for p in positions if p.exit_price is not None),
        "groups": groups,
        "settled": settled,
        "stop_n": sum(len(g) for g in groups.values()),
    }


def _score(group: List[Position], settled) -> dict:
    """Realized vs held-to-settlement for one group, with the clamp-pinned
    rows tracked separately so the comparison can be reprinted without them."""
    realized = held = firm_realized = firm_held = 0.0
    known = wins = unknown = clamped_n = firm_n = 0
    for position in group:
        real = realized_pnl(position)
        if real is None:
            continue
        realized += real
        hold, won, clamped = hold_to_settlement(position, settled)
        if hold is None:
            unknown += 1
            continue
        held += hold
        known += 1
        wins += bool(won)
        if clamped:
            clamped_n += 1
        else:
            firm_realized += real
            firm_held += hold
            firm_n += 1
    return {"n": len(group), "realized": realized, "held": held, "known": known,
            "wins": wins, "unknown": unknown, "clamped": clamped_n,
            "firm_n": firm_n, "firm_realized": firm_realized, "firm_held": firm_held}


def print_report(stations: List[str], is_paper: bool = True, limit: int = 1000,
                 detail: bool = True) -> None:
    result = audit(stations, is_paper=is_paper, limit=limit)
    groups, settled = result["groups"], result["settled"]
    stop_n, closed_n = result["stop_n"], result["closed_n"]
    track = "PAPER" if is_paper else "REAL-MONEY"
    hour = config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL

    # "scanned", not "trading": this walks the whole registry, and a
    # collection-only station contributes no closed positions at all.
    print(f"\n=== Stop-loss audit — {track} track, {len(stations)} station(s) scanned ===")
    if not stop_n:
        print("no closed stop-losses to score.\n")
        return
    print(f"closed positions                {closed_n}")
    print(f"  stop-loss exits               {stop_n}"
          + (f"  ({stop_n / closed_n:.0%} of closed)" if closed_n else ""))
    print(f"    before {hour:02d}:00 local          {len(groups[BEFORE_TIGHTEN])}"
          f"   ({config.STOP_LOSS_PCT:.0%} of the risk unit was the threshold)")
    after = len(groups[WOULD_FIRE_ANYWAY]) + len(groups[TIGHTENING_ONLY])
    print(f"    at/after {hour:02d}:00 local        {after}"
          f"   (tightened to {config.TIGHTENED_STOP_LOSS_PCT:.0%})")
    print(f"      would have fired anyway    {len(groups[WOULD_FIRE_ANYWAY])}")
    print(f"      TIGHTENING-ONLY            {len(groups[TIGHTENING_ONLY])}"
          f"   = {len(groups[TIGHTENING_ONLY]) / stop_n:.0%} of all stop-losses")

    for key in (TIGHTENING_ONLY, WOULD_FIRE_ANYWAY, BEFORE_TIGHTEN):
        group = groups[key]
        if not group:
            continue
        s = _score(group, settled)
        print(f"\n--- {_GROUP_LABELS[key]}  (n={s['n']}) ---")
        print(f"  realized                    {s['realized']:+9.2f} USD")
        if s["known"]:
            print(f"  held to settlement          {s['held']:+9.2f} USD"
                  f"   ({s['wins']}/{s['known']} would have won)")
            print(f"  upper bound on cost of cutting {s['held'] - s['realized']:+9.2f} USD")
        if s["unknown"]:
            print(f"  {s['unknown']} position(s) have no settlement-grade reading"
                  " -- left out of the held column, not scored as losses")
        if s["clamped"]:
            print(f"  excl. {s['clamped']} clamp-pinned row(s) (n={s['firm_n']}): "
                  f"realized {s['firm_realized']:+.2f} vs held {s['firm_held']:+.2f}"
                  f" -> {s['firm_held'] - s['firm_realized']:+.2f} USD")

    if detail and groups[TIGHTENING_ONLY]:
        print(f"\n--- every tightening-only stop ---")
        print(f"  {'station':7s} {'date':10s} {'bkt':>3s} {'sd':3s} {'entry':>5s} {'bid':>5s} "
              f"{'loose':>6s} {'hr':>2s} {'size':>7s} {'real':>7s} {'held':>7s}")
        for position in sorted(groups[TIGHTENING_ONLY], key=lambda p: str(p.exit_time)):
            hold, _won, clamped = hold_to_settlement(position, settled)
            held_cell = f"{hold:+.2f}" + ("?" if clamped else "") if hold is not None else "n/a"
            print(f"  {position.station_icao:7s} {position.target_date.isoformat():10s} "
                  f"{position.bucket_c:3d} {position.side:3s} {position.entry_price:5.2f} "
                  f"{gross_exit_price(position):5.2f} {loose_trigger_price(position):6.3f} "
                  f"{exit_local_hour(position):2d} {position.size_usd:7.2f} "
                  f"{realized_pnl(position):+7.2f} {held_cell:>7s}")
        print("  (? = settled reading outside the configured bucket window; see caveat 2)")

    print("\nThe held column is an UPPER BOUND on what stopping cost: it holds every")
    print("loser to settlement without charging for the entries that capital would")
    print("have blocked under the exposure caps. See the module docstring.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Score closed stop-losses against holding to settlement.")
    parser.add_argument("--station", default=None,
                        help="ICAO to audit (default: every station in config.STATIONS).")
    parser.add_argument("--track", choices=("paper", "live"), default="paper",
                        help="Which book to score. The two are never summed.")
    parser.add_argument("--limit", type=int, default=1000,
                        help="Max closed positions loaded per station (default 1000).")
    parser.add_argument("--no-detail", dest="detail", action="store_false",
                        help="Summary only -- skip the per-position table.")
    args = parser.parse_args()

    stations = [args.station] if args.station else list(config.STATIONS)
    print_report(stations, is_paper=(args.track == "paper"),
                 limit=args.limit, detail=args.detail)
