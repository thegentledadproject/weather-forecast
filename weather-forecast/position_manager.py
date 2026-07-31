"""
position_manager.py

PURPOSE
-------
Orchestrates the "don't wait for resolution" workflow:

  1. Load every currently-open position (any station) from storage
  2. Pull each one's LIVE current price via market_client
  3. Ask risk_manager whether it should be exited right now
  4. If yes, hand off to executor.py to actually close it, then
     record the closed position back to storage

This module is meant to run on every scan cycle, same cadence as
pipeline.py -- exit checks need to happen just as often as entry
checks, arguably more often near the edge-decay tightening hour,
since that's exactly when positions are most likely to need closing
rather than being opened fresh.

Deliberately separate from pipeline.py: pipeline.py answers "is there
a new trade worth entering," this module answers "does an existing
trade need to be exited." Different questions, different triggers,
different modules -- keeps each one simpler to reason about and test.

DEPENDENCIES
------------
config.py, models.py, risk_manager.py, storage.py (local)
clients/market_client.py (local)
executor.py (local)
"""

from datetime import datetime, timezone
from typing import List, Optional

import storage
import risk_manager
from clients import market_client
from models import Position, ExitDecision
import executor


def check_and_exit_positions(station_icao: Optional[str] = None) -> List[ExitDecision]:
    """
    Run one full exit-check cycle. Returns the list of ExitDecisions
    made this cycle (including "hold" decisions), for logging/summary.
    """
    open_positions = storage.load_open_positions(station_icao=station_icao)
    decisions = []

    for position in open_positions:
        current_price = market_client.get_current_price_for_side(
            market_token_id=_token_id_for(position),
            side=position.side,
        )

        if current_price is None:
            print(
                f"[position_manager] could not fetch live price for {position.position_id} "
                f"-- skipping this cycle, will retry next scan"
            )
            continue

        # Refresh high-water-mark BEFORE evaluating -- the trailing stop
        # needs to see this cycle's peak, not last cycle's.
        new_hwm = risk_manager.update_high_water_mark(position, current_price)
        if new_hwm != position.high_water_mark:
            storage.update_high_water_mark(position.position_id, new_hwm)
            position.high_water_mark = new_hwm

        decision = risk_manager.evaluate_exit(position, current_price)
        decisions.append(decision)

        if decision.should_exit:
            executor.close_position(position, decision)

    return decisions


def _token_id_for(position: Position) -> str:
    """
    Returns the Polymarket CLOB token_id for this position's specific
    (station, bucket, side). This used to be an unwired gap -- nothing
    stored this at entry time, so exit-side price checks couldn't run
    at all. Fixed by threading token_id through entry_manager.EntryDecision
    -> executor.open_position() -> Position.token_id, so it's simply read
    back here rather than needing rediscovery.

    Still raises for positions that predate this fix (token_id=None) --
    that's a real, if narrower, gap: any position opened before this
    change has no token_id on record and genuinely cannot be priced
    without a manual backfill.
    """
    if position.token_id is None:
        raise NotImplementedError(
            f"Position {position.position_id} has no token_id on record "
            f"(likely opened before entry_manager -> executor -> Position "
            f"wiring existed). Cannot price-check without a manual backfill."
        )
    return position.token_id


def print_summary(decisions: List[ExitDecision]) -> None:
    """Human-readable console output for one exit-check cycle."""
    if not decisions:
        print("[position_manager] no open positions to check.")
        return

    print(f"\n=== Position Exit Check — {datetime.now(timezone.utc).isoformat()} ===")
    for d in decisions:
        flag = "EXIT" if d.should_exit else "hold"
        print(
            f"  {d.position_id:<40} {flag:>5}  reason={d.reason:<12} "
            f"price={d.current_price:.3f}  pnl={d.pnl_pct:+.1%}"
        )
    print()
