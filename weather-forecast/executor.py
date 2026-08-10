"""
executor.py

PURPOSE
-------
The only module allowed to represent placing/closing real trades.

THE MODE LADDER
---------------
Each station runs in exactly one mode. Every rung adds one real thing:

  "manual_review" -- prints an ACTION NEEDED alert for a human to execute
                     by hand. The safest default and the right one before
                     any automated track record exists for a station.

  "paper"         -- auto-fills at the live decision price with ZERO real
                     risk (is_paper=True, never touches wallet_client).
                     Builds a genuine forward-only performance record, which
                     no backtest can supply since no historical Polymarket
                     price data exists in this codebase.

  "simulation"    -- NEW. Runs the REAL order-construction path against the
                     REAL market -- tick-size resolution, share rounding,
                     minimum-order-size checks, book reachability, and the
                     price-drift check -- and stops immediately before
                     submission. Zero real risk, and no credentials needed
                     on the box (everything it reads is a public endpoint).

                     This rung exists because "paper" validates the STRATEGY
                     but validates nothing at all about EXECUTION: it
                     fabricates a fill at the decision price, so a paper
                     track record stays perfect while the real order path
                     would be rejecting every order for being a cent under
                     the exchange minimum. Simulation is the only mode that
                     can catch that, and it catches it without spending
                     anything.

  "live"          -- real money. Requires all of: the station allowlisted in
                     config.LIVE_TRADING_STATIONS, STATION_MATURITY ==
                     "mature", the mode set here, and POLYMARKET_LIVE_TRADING
                     =true in the environment.

The legacy mode string "auto" is REFUSED rather than mapped onto "live" --
see _validated_mode(). Quietly accepting an old alias for the mode that
spends money is exactly the kind of convenience that turns a stale config
into a funded trade.

WHY WSSS AND NOTHING ELSE
--------------------------
WSSS is the only station with a confirmed, measured bias-correction edge,
and per the 2026-08-09 divergence review it carries the entire book while
the other twelve stations are collectively negative. config.py enforces the
allowlist-AND-maturity conjunction; this module refuses to run a station in
a real-money mode it has not earned rather than silently downgrading it.

DEPENDENCIES
------------
datetime, typing (standard library)
models.py, storage.py, config.py, risk_manager.py, clients/wallet_client.py
"""

from datetime import datetime, timezone
from typing import Optional

from models import Position, ExitDecision, EntryDecision
import storage
import risk_manager
import config
from clients import wallet_client

# Per-station execution mode. See the ladder in the module docstring.
#
# WSSS sits at "simulation": the real order path runs every cycle against
# the real book, and nothing is submitted. Promoting it to "live" is a
# one-word edit here PLUS setting POLYMARKET_LIVE_TRADING=true in the
# daemon environment -- deliberately two separate places, so neither a
# stray commit nor a stray environment variable is sufficient alone.
EXECUTION_MODE = {
    "WSSS": "simulation",
    "WMKK": "manual_review",
}

VALID_MODES = ("manual_review", "paper", "simulation", "live")

# Modes that represent a real position on the exchange. Only "live" does.
REAL_MONEY_MODES = ("live",)


def _validated_mode(station_icao: str) -> str:
    """
    The station's mode, having confirmed it is a mode this station is
    allowed to be in. Fails CLOSED in both directions:

      - an unknown mode string raises rather than defaulting to anything
      - a station set to simulation/live without satisfying
        config.live_mode_is_permitted() raises rather than quietly
        downgrading to paper

    The second one matters more than it looks. A silent downgrade would
    mean an operator who believes WMKK is trading live sees ordinary paper
    output and no error -- and the reverse mistake, a silent UPGRADE, is
    the one this guarantees can never happen at all.
    """
    mode = EXECUTION_MODE.get(station_icao, "manual_review")

    if mode == "auto":
        raise ValueError(
            f"Station {station_icao} is set to the legacy mode 'auto', which no "
            f"longer exists. Use 'live' for real money (and read the mode ladder "
            f"in executor.py first) or 'simulation' to exercise the order path "
            f"without spending anything. Refusing to interpret 'auto'."
        )
    if mode not in VALID_MODES:
        raise ValueError(
            f"Unknown execution mode '{mode}' for station {station_icao}. "
            f"Valid modes: {', '.join(VALID_MODES)}."
        )
    if not config.live_mode_is_permitted(station_icao, mode):
        raise ValueError(
            f"Station {station_icao} is set to '{mode}' but has not earned it: "
            f"a station may only run in simulation/live if it is BOTH listed in "
            f"config.LIVE_TRADING_STATIONS AND has STATION_MATURITY == 'mature'. "
            f"Refusing to run the real order path for it."
        )
    return mode


# --------------------------------------------------------------------------
# Live-track blast-radius checks
# --------------------------------------------------------------------------

def _live_budget_breach(size_usd: float) -> Optional[str]:
    """
    Whether opening one more live position would breach a config backstop.
    Returns the reason string, or None if the entry is within budget.

    These gate NEW ENTRIES ONLY. Exits are never blocked by a budget check
    anywhere in this module: refusing to close a real position because an
    exposure counter says so would strand actual money on the exchange,
    which is strictly worse than the exposure that opened it.
    """
    live_positions = [
        p for p in storage.load_open_positions(is_paper=False)
        if getattr(p, "execution_mode", "paper") == "live"
    ]

    if len(live_positions) >= config.LIVE_MAX_CONCURRENT_POSITIONS:
        return (
            f"{len(live_positions)} live position(s) already open, at the "
            f"LIVE_MAX_CONCURRENT_POSITIONS limit of {config.LIVE_MAX_CONCURRENT_POSITIONS}"
        )

    exposure = sum(p.size_usd for p in live_positions)
    if exposure + size_usd > config.LIVE_MAX_TOTAL_EXPOSURE_USD:
        return (
            f"${exposure:.2f} live exposure + ${size_usd:.2f} would exceed the "
            f"LIVE_MAX_TOTAL_EXPOSURE_USD ceiling of ${config.LIVE_MAX_TOTAL_EXPOSURE_USD:.2f}"
        )

    today = datetime.now(timezone.utc).date().isoformat()
    todays_orders = [
        p for p in _all_live_positions()
        if p.entry_time and p.entry_time[:10] == today
    ]
    if len(todays_orders) >= config.LIVE_MAX_ORDERS_PER_DAY:
        return (
            f"{len(todays_orders)} live order(s) already submitted today, at the "
            f"LIVE_MAX_ORDERS_PER_DAY limit of {config.LIVE_MAX_ORDERS_PER_DAY}"
        )
    return None


def _all_live_positions() -> list:
    """
    Every live position, open or closed. The daily order counter has to
    include closed ones: a position opened and stopped out an hour later
    still consumed one of the day's orders, and counting only open
    positions would let a bad day reset its own limit by losing.
    """
    positions = list(storage.load_open_positions(is_paper=False))
    for icao in config.STATIONS:
        positions += storage.load_position_history(icao, limit=1000, is_paper=False)
    return [p for p in positions if getattr(p, "execution_mode", "paper") == "live"]


def _price_drift_ok(spec_price: float, decided_price: float) -> tuple:
    """
    Whether the tick-aligned limit price still resembles the price the
    trade was approved at.

    Tick alignment rounds a BUY limit UP, and on a 0.01-tick market that is
    up to a full cent -- on a $0.30 share, 3.3%, which is a third of the
    entire MAX_ACCEPTABLE_SLIPPAGE_PCT budget before the book has moved at
    all. entry_manager approved a specific net EV at a specific price; if
    alignment plus drift has eaten past the slippage the strategy allows,
    the approval no longer describes this trade.

    THIS IS DELIBERATELY MEASURED ON THE PADDED LIMIT, not the expected
    fill. config.LIVE_LIMIT_PAD_MAX_PCT (3%) is money the exchange is
    permitted to take, so it must be spent from the same slippage budget as
    a real adverse move -- otherwise the pad is a quiet 3% widening of the
    only price protection the entry has. It leaves roughly 7 points of the
    10% budget for genuine drift, and that ordering (pad capped well below
    the budget) is what keeps the two compatible.
    """
    if not decided_price or decided_price <= 0:
        return False, "no decision price to compare the resolved limit against"
    drift = (spec_price - decided_price) / decided_price
    if drift > config.MAX_ACCEPTABLE_SLIPPAGE_PCT:
        return False, (
            f"resolved limit {spec_price:.4f} is {drift:+.1%} above the approved "
            f"{decided_price:.4f}, past the {config.MAX_ACCEPTABLE_SLIPPAGE_PCT:.0%} "
            f"slippage budget the entry was sized under"
        )
    return True, f"limit {spec_price:.4f} vs approved {decided_price:.4f} ({drift:+.1%})"


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------

def open_position(decision: EntryDecision) -> None:
    """
    Open a new position per entry_manager's EntryDecision. The single
    required path for opening a position, symmetric with close_position().

    Does nothing if decision.approved is False -- callers pass every
    EntryDecision through here, approved or not, for one consistent log of
    what was considered each cycle.
    """
    if not decision.approved:
        print(f"[executor] {decision.station_icao} {decision.bucket_c}°{decision.side}: not opened -- {decision.reason}")
        return

    if decision.entry_price is None:
        print(f"[executor] {decision.station_icao} {decision.bucket_c}°{decision.side}: approved but no entry_price recorded -- refusing to open blind.")
        return

    mode = _validated_mode(decision.station_icao)
    entry_time = datetime.now(timezone.utc).isoformat()
    position_id = f"{decision.station_icao}:{decision.target_date}:{decision.bucket_c}:{decision.side}:{entry_time}"

    def _position(size_usd: float, size_shares=None, entry_price=None, order_id=None) -> Position:
        return Position(
            position_id=position_id,
            station_icao=decision.station_icao,
            target_date=decision.target_date,
            bucket_c=decision.bucket_c,
            side=decision.side,
            entry_price=entry_price if entry_price is not None else decision.entry_price,
            size_usd=size_usd,
            entry_time=entry_time,
            status="open",
            token_id=decision.token_id,
            # is_paper stays True for every mode except live, so existing
            # paper-vs-real queries keep meaning what they meant before
            # simulation existed.
            is_paper=(mode != "live"),
            size_shares=size_shares,
            execution_mode=mode,
            order_id=order_id,
        )

    if mode == "manual_review":
        print(
            f"\n[ACTION NEEDED] {decision.station_icao} {decision.bucket_c}°C ({decision.side}) -- OPEN ENTRY\n"
            f"  Price: {decision.entry_price:.3f}  Size: ${decision.recommended_size_usd:.2f}  "
            f"({decision.station_maturity} station)\n"
            f"  Net EV at size: {decision.net_ev_at_size:+.1%}\n"
            f"  Recommended: BUY {decision.recommended_size_usd:.2f} USD of this position now.\n"
        )
        storage.open_position(_position(decision.recommended_size_usd))
        return

    if mode == "paper":
        print(
            f"[executor] PAPER FILL: {decision.station_icao} {decision.bucket_c}°{decision.side} "
            f"@ {decision.entry_price:.3f}, size=${decision.recommended_size_usd:.2f} "
            f"(net EV at entry: {decision.net_ev_at_size:+.1%}) -- zero real risk, auto-filled."
        )
        storage.open_position(_position(decision.recommended_size_usd))
        return

    _open_via_order_path(decision, mode, _position)


def _open_via_order_path(decision: EntryDecision, mode: str, make_position) -> None:
    """
    The shared simulation/live entry path. Identical in both modes right up
    to the submit call -- that is the entire design intent of the
    simulation rung, and it is why this is one function and not two.
    """
    tag = mode.upper()
    label = f"{decision.station_icao} {decision.bucket_c}°{decision.side}"

    if not decision.token_id:
        print(f"[executor] {tag}: {label} has no token_id -- cannot build an order, skipping.")
        return

    spec = wallet_client.build_entry_order(
        token_id=decision.token_id,
        price=decision.entry_price,
        size_usd=decision.recommended_size_usd,
    )

    if mode == "simulation":
        for line in wallet_client.preflight(decision.token_id):
            print(f"[executor] SIMULATION preflight: {line}")

    if not spec.ok:
        print(f"[executor] {tag}: {label} order NOT placeable -- {spec.reason}")
        return

    drift_ok, drift_note = _price_drift_ok(spec.limit_price, decision.entry_price)
    if not drift_ok:
        print(f"[executor] {tag}: {label} order abandoned -- {drift_note}")
        return

    if mode == "live":
        breach = _live_budget_breach(spec.notional_usd)
        if breach:
            print(f"[executor] LIVE: {label} entry BLOCKED by a risk backstop -- {breach}")
            return

    result = wallet_client.submit_order(spec, live=(mode == "live"))

    if mode == "simulation":
        print(
            f"[executor] SIMULATION: {label} order fully resolved but NOT submitted -- "
            f"{spec.describe()}; {drift_note}. Recording at the resolved size so the "
            f"simulated book matches what a real order would actually have cost."
        )
        # Recorded at the RESOLVED notional and share count, not the
        # requested $1: the whole point of this rung is to find out what the
        # real order would have been, so storing the requested figure would
        # discard the one number the mode exists to produce.
        storage.open_position(make_position(
            size_usd=spec.notional_usd,
            size_shares=spec.size_shares,
            entry_price=spec.limit_price,
        ))
        return

    if not result.filled:
        # NOTHING is written. An unfilled order means no shares exist, and a
        # stored "open" position with no shares behind it is a position the
        # exit path will later try to sell -- the single worst failure this
        # module can produce.
        print(
            f"[executor] LIVE: {label} order did NOT fill -- {result.error or 'killed unfilled'}. "
            f"No position recorded (correct: there are no shares to record)."
        )
        return

    fill_price = result.fill_price or spec.limit_price
    fill_shares = result.fill_shares or spec.size_shares
    print(
        f"[executor] LIVE FILL: {label} {fill_shares:.2f} shares @ {fill_price:.4f} "
        f"= ${fill_price * fill_shares:.2f} (order {result.order_id}). REAL MONEY."
    )
    storage.open_position(make_position(
        size_usd=round(fill_price * fill_shares, 4),
        size_shares=fill_shares,
        entry_price=fill_price,
        order_id=result.order_id,
    ))


# --------------------------------------------------------------------------
# Exits
# --------------------------------------------------------------------------

def close_position(
    position: Position,
    decision: ExitDecision,
    status: Optional[str] = None,
    exit_reason: Optional[str] = None,
) -> None:
    """
    Close an open position per risk_manager's ExitDecision. Fires
    immediately when the decision says to exit, independent of whether the
    underlying weather market has resolved yet.

    status/exit_reason override what gets written to storage, for exits
    that are NOT a risk_manager price decision -- position_manager.py uses
    them for market resolution (status="closed_resolution",
    exit_reason="market_resolved") so a resolved market can never be filed
    under the derived "closed_{decision.reason}" of a stop-loss.

    THE RECORDED EXIT PRICE IS NET OF THE EXIT-SIDE TAKER FEE
    ---------------------------------------------------------
    What gets written is the EFFECTIVE fill price -- the quote minus
    Polymarket's taker fee on this leg -- not the raw quote. The raw quote
    overstates every exit by the fee, and since the fee is
    0.05 x (1 - price) x price per share it is worth several times a
    typical trailing-stop gain: booking exits gross is how a strategy shows
    a positive record while losing money. The gross quote and the fee are
    both preserved in the exit_reason text so nothing is lost.

    NOT applied to resolution closes: redeeming a resolved position pays
    par and is not a taker fill, so there is no fee to deduct.

    KNOWN ASYMMETRY: entry_price is still recorded gross, so a stored
    position's P&L is net of the exit fee but not the entry fee. Closing
    that gap rewrites the meaning of every historical entry_price in the
    database -- a separate migration, not a side effect of this change.
    """
    # DISPATCH ON HOW THE POSITION WAS OPENED, NOT ON THE STATION'S CURRENT
    # MODE. This is the single most dangerous line in the module and it used
    # to read EXECUTION_MODE.get(position.station_icao).
    #
    # The failure: a real live position is open, the daemon restarts without
    # the live flag (a redeploy, a crash, a forgotten CLI argument), and the
    # station's mode is now "manual_review". The exit check then routes a
    # REAL position through the manual_review branch, which prints an alert
    # and immediately writes storage.close_position(). The database now says
    # closed. Nothing ever looks at it again -- while the shares are still
    # held on the exchange, with no stop-loss, invisible to every report and
    # every exposure cap. The mirror-image failure is just as bad: starting
    # WITH the live flag would route pre-existing paper positions into a real
    # sell order for shares that do not exist.
    #
    # A position's execution mode is a fact about that position, fixed at the
    # moment it was opened. It is not a runtime setting.
    mode = getattr(position, "execution_mode", None) or "paper"
    if mode not in VALID_MODES:
        mode = "paper"

    # A live position may only be closed by a process authorized for live.
    # Leaving it open is the safe failure: it stays visible, stays monitored,
    # and gets closed correctly by the next authorized run.
    if mode == "live" and EXECUTION_MODE.get(position.station_icao) != "live":
        print(
            f"[executor] REFUSING to close LIVE position {position.position_id}: this "
            f"process has {position.station_icao} in "
            f"'{EXECUTION_MODE.get(position.station_icao, 'manual_review')}' mode, not "
            f"'live'. Real shares are held. Leaving the position OPEN rather than "
            f"recording a close that did not happen -- restart with --mode live to act on it."
        )
        return

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

    def _record(reason_tag: str) -> None:
        storage.close_position(
            position_id=position.position_id,
            exit_price=exit_price,
            exit_time=exit_time,
            status=status,
            reason=exit_reason or f"{decision.reason} ({reason_tag}, pnl={net_pnl_pct:+.1%} net; {fee_note})",
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
        # Logged as closed so it doesn't get re-flagged identically every
        # scan cycle while a human hasn't acted yet.
        _record("manual review")
        return

    if mode == "paper":
        print(
            f"[executor] PAPER EXIT: {position.station_icao} {position.bucket_c}°{position.side} "
            f"-- {decision.reason.upper()} @ {gross_exit_price:.3f} ({fee_note}), "
            f"pnl={net_pnl_pct:+.1%} net of the exit fee -- zero real risk, auto-filled."
        )
        _record("paper")
        return

    _close_via_order_path(position, decision, mode, status, exit_price, net_pnl_pct, fee_note, _record)


def _close_via_order_path(
    position, decision, mode, status, exit_price, net_pnl_pct, fee_note, record,
) -> None:
    """
    The shared simulation/live exit path.

    RESOLUTION CLOSES NEVER PLACE AN ORDER, in any mode. A resolved market
    has no book to sell into; the position is redeemed for par instead.
    Sending a sell order there is not merely useless, it is a guaranteed
    unfilled order that would then block the close and strand the position.
    """
    label = f"{position.station_icao} {position.bucket_c}°{position.side}"
    tag = mode.upper()

    if status == "closed_resolution":
        note = (
            "market RESOLVED -- no order placed (nothing to sell into). "
            + ("Position redeems for par; REDEEM IT ON THE EXCHANGE." if mode == "live"
               else "Recorded as redeemed.")
        )
        print(f"[executor] {tag}: {label} -- {note}")
        record(mode)
        return

    spec = wallet_client.build_exit_order(
        token_id=position.token_id,
        price=decision.current_price,
        size_shares=getattr(position, "size_shares", None),
    )

    if not spec.ok:
        # Refusing to record a close we could not place is the whole point.
        # A position marked closed while its shares are still on the
        # exchange is invisible to every later exit check.
        print(
            f"[executor] {tag}: {label} exit could NOT be built -- {spec.reason}. "
            f"Position left OPEN and will be re-evaluated next cycle."
        )
        return

    result = wallet_client.submit_order(spec, live=(mode == "live"))

    if mode == "simulation":
        print(
            f"[executor] SIMULATION EXIT: {label} -- {decision.reason.upper()} "
            f"order resolved but NOT submitted: {spec.describe()}; "
            f"pnl={net_pnl_pct:+.1%} net ({fee_note})."
        )
        record("simulation")
        return

    if not result.filled:
        print(
            f"[executor] LIVE: {label} exit order did NOT fill -- "
            f"{result.error or 'killed unfilled'}. Position left OPEN "
            f"(the shares are still held) and will be retried next cycle."
        )
        return

    print(
        f"[executor] LIVE EXIT: {label} -- {decision.reason.upper()} "
        f"{spec.size_shares:.2f} shares @ {result.fill_price or spec.limit_price:.4f} "
        f"(order {result.order_id}), pnl={net_pnl_pct:+.1%} net ({fee_note}). REAL MONEY."
    )
    record("live")
