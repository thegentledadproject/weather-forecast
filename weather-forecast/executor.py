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
from typing import Optional

from models import Position, ExitDecision, EntryDecision
import storage
import risk_manager

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


def close_position(
    position: Position,
    decision: ExitDecision,
    status: Optional[str] = None,
    exit_reason: Optional[str] = None,
) -> None:
    """
    Close an open position per risk_manager's ExitDecision. This is
    the function that makes profit-taking/stop-loss real: it fires
    immediately when the decision says to exit, independent of
    whether the underlying weather market has resolved yet.

    status/exit_reason override what gets written to storage, for exits
    that are NOT a risk_manager price decision. position_manager.py uses
    them for market resolution (status="closed_resolution",
    exit_reason="market_resolved"), so a resolved market can never be
    filed under the derived "closed_{decision.reason}" of a stop-loss.
    Left None (the normal path), both are derived exactly as before.

    THE RECORDED EXIT PRICE IS NET OF THE EXIT-SIDE TAKER FEE
    ---------------------------------------------------------
    What gets written to storage is the EFFECTIVE fill price -- the quote
    minus Polymarket's taker fee on this leg -- not the raw quote. The
    raw quote overstates every exit by the fee, and since the fee is
    0.05 x (1 - price) x price per share it is worth several times a
    typical trailing-stop gain: booking exits gross is how a strategy
    shows a positive record while losing money. The gross quote and the
    fee are both preserved in the exit_reason text so nothing is lost.

    NOT applied to resolution closes: redeeming a resolved position pays
    par and is not a taker fill, so there is no fee to deduct.

    KNOWN ASYMMETRY: entry_price is still recorded gross, so a stored
    position's P&L is net of the exit fee but not the entry fee. Closing
    that gap means changing what open_position() writes, which rewrites
    the meaning of every historical entry_price in the database -- a
    separate migration, not a side effect of this change.
    """
    mode = EXECUTION_MODE.get(position.station_icao, "manual_review")
    exit_time = datetime.now(timezone.utc).isoformat()
    status = status or f"closed_{decision.reason}"

    gross_exit_price = decision.current_price
    if status == "closed_resolution":
        exit_fee_per_share = 0.0
    else:
        exit_fee_per_share = risk_manager.taker_fee_per_share(gross_exit_price)
    exit_price = max(gross_exit_price - exit_fee_per_share, 0.0)
    net_pnl_pct = risk_manager.compute_pnl_pct(position.entry_price, exit_price)
    fee_note = (
        f"gross {gross_exit_price:.4f} - exit fee {exit_fee_per_share:.4f}/share "
        f"= net {exit_price:.4f}"
    )

    if mode == "manual_review":
        # A resolved market can't be sold into -- the book is gone. Telling
        # an operator to SELL there sends them chasing a fill that cannot
        # exist, so say what actually needs doing instead.
        if status == "closed_resolution":
            action = "REDEEM this position -- the market has RESOLVED, there is nothing left to sell into."
        else:
            action = f"SELL {position.size_usd:.2f} USD of this position now."
        print(
            f"\n[ACTION NEEDED] {position.station_icao} {position.bucket_c}°C "
            f"({position.side}) -- {decision.reason.upper()}\n"
            f"  Entry: {position.entry_price:.3f}  Current: {gross_exit_price:.3f}  "
            f"P&L: {decision.pnl_pct:+.1%} gross, {net_pnl_pct:+.1%} net of the exit fee\n"
            f"  Recommended: {action}\n"
        )
        # Log as pending-manual so it doesn't get re-flagged identically
        # every single scan cycle while a human hasn't acted yet.
        storage.close_position(
            position_id=position.position_id,
            exit_price=exit_price,
            exit_time=exit_time,
            status=status,
            reason=exit_reason or f"{decision.reason} (manual review, pnl={net_pnl_pct:+.1%} net; {fee_note})",
        )
        return

    if mode == "paper":
        print(
            f"[executor] PAPER EXIT: {position.station_icao} {position.bucket_c}°{position.side} "
            f"-- {decision.reason.upper()} @ {gross_exit_price:.3f} ({fee_note}), "
            f"pnl={net_pnl_pct:+.1%} net of the exit fee -- zero real risk, auto-filled."
        )
        storage.close_position(
            position_id=position.position_id,
            exit_price=exit_price,
            exit_time=exit_time,
            status=status,
            reason=exit_reason or f"{decision.reason} (paper, pnl={net_pnl_pct:+.1%} net; {fee_note})",
        )
        return

    if mode == "auto":
        _place_sell_order(position, decision)
        storage.close_position(
            position_id=position.position_id,
            exit_price=exit_price,
            exit_time=exit_time,
            status=status,
            reason=exit_reason or f"{decision.reason} (auto, pnl={net_pnl_pct:+.1%} net; {fee_note})",
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
