"""
executor.py

PURPOSE
-------
The only module allowed to represent placing/closing real trades.
Two responsibilities relevant to this update:

  - close_position(): called by position_manager.py when risk_manager
    decides a profit-take or stop-loss should fire. Closes the
    position WITHOUT waiting for market resolution -- this is the
    entire point of the profit-taking/stop-loss feature.
  - Mode gating per station, same pattern established earlier for
    entries: "manual_review" prints the recommended action for a
    human to execute by hand; "auto" would place the real sell order.
    Only stations with a confirmed, mature edge should ever be
    eligible for "auto" (currently: none are pre-configured that way
    by default here -- station maturity gating from the risk-manager
    design is a deliberate manual step, not a default).

IMPLEMENTATION NOTE
--------------------
The actual order-placement call (selling YES/NO shares back into the
book) requires Polymarket's CLOB order-signing flow (a funded wallet,
API credentials, order signing) which is out of scope for this
module to fabricate. _place_sell_order() is stubbed with a clear
NotImplementedError in "auto" mode so this never silently pretends to
trade -- "manual_review" mode is fully functional today.

DEPENDENCIES
------------
datetime (standard library)
models.py, storage.py (local)
"""

from datetime import datetime, timezone

from models import Position, ExitDecision, EntryDecision
import storage

# Per-station execution mode. Three options:
#   "manual_review" -- prints an ACTION NEEDED alert, requires a human to
#                       act; the safest default, appropriate before any
#                       automated track record exists for a station.
#   "paper"         -- auto-fills at the live decision price with ZERO
#                       real risk (is_paper=True on the Position, never
#                       touches wallet_client). This is the recommended
#                       validation step BEFORE "auto": it builds a real,
#                       forward-only performance track record (no
#                       look-ahead bias, since it's genuinely executed as
#                       time passes) that backtest_engine.py's blocked
#                       trading-strategy backtest has no other way to get,
#                       since no historical Polymarket price data exists
#                       anywhere in this codebase.
#   "auto"          -- real order placement via wallet_client.py. Still
#                       hits the NotImplementedError stubs below --
#                       promoting a station here is a deliberate, separate
#                       decision requiring real credentials and its own
#                       security review, not something this module enables
#                       by default.
EXECUTION_MODE = {
    "WSSS": "manual_review",
    "WMKK": "manual_review",
}


def open_position(decision: EntryDecision) -> None:
    """
    Open a new position per entry_manager's EntryDecision. This closes
    a real gap: previously nothing in this codebase gated ENTRIES the
    way close_position() already gates exits -- callers (the earlier
    end-to-end simulation, in particular) were calling
    storage.open_position() directly, bypassing manual_review/auto mode
    entirely. This function is now the single required path for
    opening a position, symmetric with close_position() below.

    Does nothing if decision.approved is False -- callers should still
    be able to pass every EntryDecision (approved or not) through here
    without special-casing, for a single consistent log of what was
    considered each cycle.
    """
    if not decision.approved:
        print(f"[executor] {decision.station_icao} {decision.bucket_c}°{decision.side}: not opened -- {decision.reason}")
        return

    if decision.entry_price is None:
        print(f"[executor] {decision.station_icao} {decision.bucket_c}°{decision.side}: approved but no entry_price recorded -- refusing to open blind.")
        return

    mode = EXECUTION_MODE.get(decision.station_icao, "manual_review")
    entry_time = datetime.now(timezone.utc).isoformat()
    position_id = f"{decision.station_icao}:{decision.target_date}:{decision.bucket_c}:{decision.side}:{entry_time}"

    position = Position(
        position_id=position_id,
        station_icao=decision.station_icao,
        target_date=decision.target_date,
        bucket_c=decision.bucket_c,
        side=decision.side,
        entry_price=decision.entry_price,
        size_usd=decision.recommended_size_usd,
        entry_time=entry_time,
        status="open",
        token_id=decision.token_id,
        is_paper=(mode == "paper"),
    )

    if mode == "manual_review":
        print(
            f"\n[ACTION NEEDED] {decision.station_icao} {decision.bucket_c}°C ({decision.side}) -- OPEN ENTRY\n"
            f"  Price: {decision.entry_price:.3f}  Size: ${decision.recommended_size_usd:.2f}  "
            f"({decision.station_maturity} station)\n"
            f"  Net EV at size: {decision.net_ev_at_size:+.1%}\n"
            f"  Recommended: BUY {decision.recommended_size_usd:.2f} USD of this position now.\n"
        )
        storage.open_position(position)
        return

    if mode == "paper":
        print(
            f"[executor] PAPER FILL: {decision.station_icao} {decision.bucket_c}°{decision.side} "
            f"@ {decision.entry_price:.3f}, size=${decision.recommended_size_usd:.2f} "
            f"(net EV at entry: {decision.net_ev_at_size:+.1%}) -- zero real risk, auto-filled."
        )
        storage.open_position(position)
        return

    if mode == "auto":
        _place_buy_order(position, decision)
        storage.open_position(position)
        return

    raise ValueError(f"Unknown execution mode '{mode}' for station {decision.station_icao}")


def _place_buy_order(position: Position, decision: EntryDecision) -> None:
    """
    Real order placement -- NOT implemented, same deliberate gap as
    _place_sell_order below. Requires a funded wallet, Polymarket CLOB
    API credentials, and signed-order construction.
    """
    raise NotImplementedError(
        "Auto-mode order placement is not implemented. This is a deliberate "
        "gap, not an oversight -- promoting a station to auto-execution needs "
        "its own security review (wallet custody, position limits, kill "
        "switches) before this function should do anything real."
    )


def close_position(position: Position, decision: ExitDecision) -> None:
    """
    Close an open position per risk_manager's ExitDecision. This is
    the function that makes profit-taking/stop-loss real: it fires
    immediately when the decision says to exit, independent of
    whether the underlying weather market has resolved yet.
    """
    mode = EXECUTION_MODE.get(position.station_icao, "manual_review")
    exit_time = datetime.now(timezone.utc).isoformat()

    if mode == "manual_review":
        print(
            f"\n[ACTION NEEDED] {position.station_icao} {position.bucket_c}°C "
            f"({position.side}) -- {decision.reason.upper()}\n"
            f"  Entry: {position.entry_price:.3f}  Current: {decision.current_price:.3f}  "
            f"P&L: {decision.pnl_pct:+.1%}\n"
            f"  Recommended: SELL {position.size_usd:.2f} USD of this position now.\n"
        )
        # Log as pending-manual so it doesn't get re-flagged identically
        # every single scan cycle while a human hasn't acted yet.
        storage.close_position(
            position_id=position.position_id,
            exit_price=decision.current_price,
            exit_time=exit_time,
            status=f"closed_{decision.reason}",
            reason=f"{decision.reason} (manual review, pnl={decision.pnl_pct:+.1%})",
        )
        return

    if mode == "paper":
        print(
            f"[executor] PAPER EXIT: {position.station_icao} {position.bucket_c}°{position.side} "
            f"-- {decision.reason.upper()} @ {decision.current_price:.3f}, "
            f"pnl={decision.pnl_pct:+.1%} -- zero real risk, auto-filled."
        )
        storage.close_position(
            position_id=position.position_id,
            exit_price=decision.current_price,
            exit_time=exit_time,
            status=f"closed_{decision.reason}",
            reason=f"{decision.reason} (paper, pnl={decision.pnl_pct:+.1%})",
        )
        return

    if mode == "auto":
        _place_sell_order(position, decision)
        storage.close_position(
            position_id=position.position_id,
            exit_price=decision.current_price,
            exit_time=exit_time,
            status=f"closed_{decision.reason}",
            reason=f"{decision.reason} (auto, pnl={decision.pnl_pct:+.1%})",
        )
        return

    raise ValueError(f"Unknown execution mode '{mode}' for station {position.station_icao}")


def _place_sell_order(position: Position, decision: ExitDecision) -> None:
    """
    Real order placement -- NOT implemented. Requires a funded wallet,
    Polymarket CLOB API credentials, and signed-order construction,
    none of which belong hardcoded into this framework. Wire this up
    against Polymarket's official CLOB client library when actually
    promoting a station to 'auto' mode, with its own credential and
    risk review separate from this codebase.

    NOTED FUTURE IMPROVEMENT: per the order-execution reanalysis, a
    real implementation could offer two paths, not just a market sell
    -- (1) sell YES directly into the book, or (2) buy the
    complementary NO and call NegRisk's Merge to redeem both for
    exactly $1, avoiding the YES sell-side spread when NO's buy-side
    liquidity is better. Path (2) is a legitimate use of holding both
    sides of a bucket (unlike opening both as a fresh entry, which
    entry_manager.veto_same_bucket_conflicts() blocks) -- worth
    implementing as a smarter default once real order placement exists.
    """
    raise NotImplementedError(
        "Auto-mode order placement is not implemented. This is a deliberate "
        "gap, not an oversight -- promoting a station to auto-execution needs "
        "its own security review (wallet custody, position limits, kill "
        "switches) before this function should do anything real."
    )
