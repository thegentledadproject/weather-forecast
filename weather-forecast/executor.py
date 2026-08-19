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
from typing import Dict, Optional

from models import Position, ExitDecision, EntryDecision
import storage
import risk_manager
import config
from clients import market_client, wallet_client

# Per-station execution mode. See the ladder in the module docstring.
#
# THIS LITERAL IS THE FALLBACK, NOT THE DEPLOYED CONFIGURATION. scheduler.py
# overwrites EXECUTION_MODE for every station in config.STATIONS on startup,
# in all four of its --mode branches, and manual_trigger.py sets it directly.
# So editing the dict below changes nothing about the running daemon, and an
# earlier version of this comment -- which said promoting WSSS was "a
# one-word edit here" -- was simply wrong. A reader trusting it would believe
# repo state was deployed state.
#
# The deployed mode lives in /etc/polyweather/mode.env on the box, which
# deploy_daemon.sh creates once and never rewrites. What this dict actually
# governs is any process that does NOT go through scheduler.py's CLI.
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

    # THE CAPS BELOW ARE ONLY AS TRUE AS THIS TABLE, so ask the exchange
    # before trusting them. Every limit in this function is derived from
    # storage, which makes them caps on the database's RECOLLECTION of
    # exposure rather than on exposure -- and the two provably diverge: on
    # 2026-08-10 a real position sat on the exchange while the daemon had no
    # row for it, and during that window all three read one position light.
    #
    # Fails closed, including when the check itself cannot run. "I could not
    # look" and "I looked and it was wrong" are the same answer when the
    # question is whether to spend more money. This is also why it is here
    # and not in close_position(): an exit must never be blocked by a
    # bookkeeping doubt, only an entry.
    recon = wallet_client.reconcile_cached(live_positions)
    if not recon.ok:
        return (
            f"exchange reconciliation did not pass -- {recon.describe()}. "
            f"Refusing to open anything new until the database and the exchange "
            f"agree, because every backstop below is computed from the database"
        )

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

    # SUBMISSIONS, not fills. This used to count rows in `positions`, and an
    # unfilled FOK writes no position -- deliberately, since a stored position
    # with no shares behind it is the worst thing this module can produce. The
    # consequence was that a day of two hundred killed orders consumed none of
    # the ten-order budget: a rate limit meant to bound how hard this system
    # hammers the exchange after an upstream fault, measured on the one
    # outcome a fault does not produce.
    #
    # Exits are counted in the same table but NOT against this cap. An exit
    # must never be rate-limited; the audit trail still wants them.
    today = datetime.now(timezone.utc).date().isoformat()
    submitted = storage.count_live_order_attempts("entry", today)
    if submitted is None:
        return (
            "could not read today's live order count -- refusing to authorise on an "
            "unenforceable rate limit (a cap that fails open is not a cap)"
        )
    if submitted >= config.LIVE_MAX_ORDERS_PER_DAY:
        return (
            f"{submitted} live order(s) already SUBMITTED today (filled or not), at the "
            f"LIVE_MAX_ORDERS_PER_DAY limit of {config.LIVE_MAX_ORDERS_PER_DAY}"
        )
    return None


def _fmt_net_ev(value) -> str:
    """
    Net EV for display, tolerating None.

    None is not 0.0 here: manual_trigger.py bypasses the model entirely and
    reports None because no EV was computed, while 0.0 would claim it was
    measured at exactly break-even. Formatting one as the other is how a
    sentinel becomes a number nobody questions.
    """
    return "unknown (no model ran)" if value is None else format(value, "+.1%")


def _resolved_size_ok(spec, decision) -> tuple:
    """
    Re-check the size-dependent gates at the notional that will ACTUALLY be
    submitted. Returns (ok, note); an empty note means the size did not move.

    THE HOLE THIS CLOSES. config.LIVE_TRADE_SIZE_USD is clamped in
    entry_manager.size_position() ABOVE the depth, slippage and net-EV
    re-checks, and config.py argues at length that this keeps "every
    downstream number about the order that actually gets built". That
    argument is defeated one layer down: wallet_client.build_entry_order()
    then raises the share count to the exchange minimum, which at
    MAX_ENTRY_PRICE and a 5-share floor is $3.75 -- up to 3.75x the size
    every one of those gates cleared. _price_drift_ok() already re-validates
    PRICE after resolution; nothing re-validated SIZE.

    Only the ABSOLUTE gates are re-run. The original min_net_ev threshold is
    a per-window figure that does not reach this layer, so the net-EV test
    here is the weaker "still positive" floor rather than the bar the entry
    was approved against -- stated plainly because it is a real gap, not a
    silently equivalent substitute. The two config-driven gates
    (MAX_ACCEPTABLE_SLIPPAGE_PCT, MAX_DEPTH_UTILIZATION_PCT) are absolute and
    are re-run exactly as entry_manager runs them.

    Net EV is re-derived rather than recomputed: net_ev = edge/price -
    slippage - fee, and of those only slippage moves with size, so the
    change is exactly the slippage increase. That needs no field the
    EntryDecision does not already carry.
    """
    requested = decision.recommended_size_usd or 0.0
    resolved = spec.notional_usd
    if resolved <= requested + 1e-9:
        return True, ""

    note = f"${requested:.2f} -> ${resolved:.2f} by the exchange minimum"

    depth = decision.available_depth_usd
    if depth is not None:
        ceiling = depth * config.MAX_DEPTH_UTILIZATION_PCT
        if resolved > ceiling:
            return False, (
                f"resolved notional ${resolved:.2f} is past {config.MAX_DEPTH_UTILIZATION_PCT:.0%} "
                f"of the ${depth:.2f} visible depth (${ceiling:.2f}); the entry was sized "
                f"at ${requested:.2f} and never cleared the book at this size"
            )

    try:
        slippage = market_client.estimate_slippage(decision.token_id, resolved)
    except Exception as exc:  # noqa: BLE001 -- a failed re-check must not pass by default
        return False, f"could not re-estimate slippage at ${resolved:.2f} ({exc}) -- refusing to guess"

    if slippage > config.MAX_ACCEPTABLE_SLIPPAGE_PCT:
        return False, (
            f"slippage at the resolved ${resolved:.2f} is {slippage:.1%}, past the "
            f"{config.MAX_ACCEPTABLE_SLIPPAGE_PCT:.0%} hard gate (was "
            f"{(decision.slippage_at_size_pct or 0):.1%} at ${requested:.2f})"
        )

    if decision.net_ev_at_size is not None and decision.slippage_at_size_pct is not None:
        net_ev = decision.net_ev_at_size - (slippage - decision.slippage_at_size_pct)
        if net_ev <= 0:
            return False, (
                f"net EV falls to {net_ev:+.1%} at the resolved ${resolved:.2f} "
                f"(was {decision.net_ev_at_size:+.1%} at ${requested:.2f}) -- the size the "
                f"exchange forces is not the trade that was approved"
            )
        note += f", slippage {slippage:.1%}, net EV {decision.net_ev_at_size:+.1%} -> {net_ev:+.1%}"

    return True, note


def _record_attempt(kind, station_icao, spec, result, target_date=None,
                    bucket_c=None, side="") -> None:
    """
    Append one real-money submission to the audit trail.

    Called for LIVE submissions only, on every outcome -- filled, killed and
    errored alike. That is the whole point: the daily cap counts submissions,
    and a rejected order previously left no trace anywhere outside the
    process log.

    Never raises. Failing to write the audit row must not undo an order that
    has already reached the exchange; the loud complaint is the right
    outcome, a traceback here is not.
    """
    if result.filled:
        outcome = "filled"
    elif result.submitted:
        outcome = "killed"
    else:
        outcome = "not_submitted"
    try:
        storage.record_live_order_attempt(
            kind=kind, station_icao=station_icao, outcome=outcome,
            target_date=target_date, bucket_c=bucket_c, side=side,
            notional_usd=spec.notional_usd, size_shares=spec.size_shares,
            limit_price=spec.limit_price, order_id=result.order_id,
            detail=result.error or spec.reason,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[executor] WARNING: could not record the live {kind} attempt for "
            f"{station_icao} ({outcome}): {exc}. The order itself is unaffected, but "
            f"LIVE_MAX_ORDERS_PER_DAY is now under-counting."
        )


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
# Refused live closes
# --------------------------------------------------------------------------

# Consecutive cycles a live position's close has been refused because this
# process is not authorised for live, keyed by position_id. In-memory and
# per-process, matching position_manager._consecutive_price_failures: the
# condition is about THIS process's authority, so a restart re-deciding from
# scratch is correct rather than something to persist.
_consecutive_live_close_refusals: Dict[str, int] = {}

# Refusals before the log escalates from a routine line to the full
# ACTION NEEDED block. Deliberately 1: unlike an unreadable price feed, which
# is usually transient and self-heals, an unauthorised process does not
# become authorised by waiting. There is nothing to give the benefit of the
# doubt to, and the first refusal already means a real stop-loss went
# unhonoured on real shares.
LIVE_CLOSE_REFUSAL_ESCALATE_AFTER = 1


def _note_live_close_refused(position: Position, decision: ExitDecision) -> int:
    """
    Record and announce one refused live close. Returns the new consecutive
    count.

    WHY THIS IS LOUD. Refusing is correct -- see the dispatch comment in
    close_position() -- but the refusal used to be a single print() and a
    return, repeated every cycle forever, with no counter and no operator
    instruction. position_manager escalates its two comparable conditions
    (_note_price_failure, _note_unknown_resolution) and the manual_review
    branch of this very function emits an ACTION NEEDED block; the one path
    where REAL SHARES have an unhonoured stop-loss did neither.

    The asymmetry the original design got right is that leaving the position
    open beats recording a close that did not happen. The asymmetry it got
    wrong is that a safe failure still has to be a NOISY one.
    """
    count = _consecutive_live_close_refusals.get(position.position_id, 0) + 1
    _consecutive_live_close_refusals[position.position_id] = count
    running_mode = EXECUTION_MODE.get(position.station_icao, "manual_review")

    if count < LIVE_CLOSE_REFUSAL_ESCALATE_AFTER:
        print(
            f"[executor] refused to close LIVE position {position.position_id} "
            f"({running_mode} process, needs live) -- refusal {count}"
        )
        return count

    shares = getattr(position, "size_shares", None)
    shares_text = f"{shares:.2f} shares" if shares else "an unrecorded share count"
    print(
        f"\n[ACTION NEEDED] {position.station_icao} {position.bucket_c}°C "
        f"({position.side}) -- LIVE POSITION CANNOT BE CLOSED BY THIS PROCESS\n"
        f"  Position:  {position.position_id}\n"
        f"  Held:      {shares_text} @ {position.entry_price:.4f} "
        f"(${position.size_usd:.2f}), order {position.order_id or 'unrecorded'}\n"
        f"  Signal:    {decision.reason.upper()} at {decision.current_price:.4f} "
        f"({decision.pnl_pct:+.1%})\n"
        f"  Why:       this process has {position.station_icao} in '{running_mode}' mode, "
        f"not 'live', so it may not act on real shares.\n"
        f"  REAL SHARES ARE HELD AND THIS EXIT SIGNAL IS GOING UNHONOURED. "
        f"Refused {count} cycle(s) in a row.\n"
        f"  Do one of: SELL/REDEEM this position on the exchange by hand and close its\n"
        f"  row, or set POLYWEATHER_MODE=live in /etc/polyweather/mode.env and restart\n"
        f"  so the daemon may act on it.\n"
    )
    return count


def forget_live_close_refusals(position_id: str) -> None:
    """Clear a position's refusal streak once it is genuinely dealt with."""
    _consecutive_live_close_refusals.pop(position_id, None)


def unmanageable_live_positions() -> list:
    """
    Open live positions this process is NOT authorised to close.

    A startup check, so an operator learns at boot rather than from a log
    line buried in the next scan cycle -- the failure this reports is silent
    by nature, since the daemon otherwise runs perfectly normally while real
    shares sit with no working stop-loss behind them.
    """
    try:
        positions = storage.load_open_positions(is_paper=False)
    except Exception as exc:  # noqa: BLE001 -- a startup check must not stop the daemon
        print(f"[executor] could not check for unmanageable live positions: {exc}")
        return []
    return [
        p for p in positions
        if getattr(p, "execution_mode", "paper") == "live"
        and EXECUTION_MODE.get(p.station_icao) != "live"
    ]


def warn_about_unmanageable_live_positions() -> int:
    """
    Print a startup banner for every live position this process cannot
    close. Returns how many there were, so a caller can decide to be
    louder still.
    """
    stranded = unmanageable_live_positions()
    if not stranded:
        return 0

    total = sum(p.size_usd for p in stranded)
    print(
        f"\n[ACTION NEEDED] {len(stranded)} OPEN LIVE POSITION(S) THIS PROCESS CANNOT CLOSE "
        f"-- ${total:.2f} of real exposure\n"
        f"  Their stop-losses and profit-takes will NOT fire while this daemon runs in a "
        f"non-live mode.\n"
    )
    for p in stranded:
        print(
            f"    {p.station_icao} {p.bucket_c}°{p.side}  {p.size_shares or '?'} shares "
            f"@ {p.entry_price:.4f} (${p.size_usd:.2f})  order {p.order_id or 'unrecorded'}\n"
            f"      {p.position_id}"
        )
    print(
        f"  Close them on the exchange and close their rows, or set POLYWEATHER_MODE=live\n"
        f"  in /etc/polyweather/mode.env and restart.\n"
    )
    return len(stranded)

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
            # What the model believed, persisted alongside the trade so a
            # closed position can be scored against its own prediction and
            # not only against P&L. Copied, never recomputed: re-deriving
            # model_prob at any later point would read a calibration that
            # has since seen the outcome. None flows through untouched --
            # manual_trigger bypasses the model, and "no model ran" must
            # stay distinguishable from "the model said 0".
            model_prob=decision.model_prob,
            raw_edge=decision.raw_edge,
            net_ev_at_size=decision.net_ev_at_size,
        )

    if mode == "manual_review":
        print(
            f"\n[ACTION NEEDED] {decision.station_icao} {decision.bucket_c}°C ({decision.side}) -- OPEN ENTRY\n"
            f"  Price: {decision.entry_price:.3f}  Size: ${decision.recommended_size_usd:.2f}  "
            f"({decision.station_maturity} station)\n"
            f"  Net EV at size: {_fmt_net_ev(decision.net_ev_at_size)}\n"
            f"  Recommended: BUY {decision.recommended_size_usd:.2f} USD of this position now.\n"
        )
        storage.open_position(_position(decision.recommended_size_usd))
        return

    if mode == "paper":
        print(
            f"[executor] PAPER FILL: {decision.station_icao} {decision.bucket_c}°{decision.side} "
            f"@ {decision.entry_price:.3f}, size=${decision.recommended_size_usd:.2f} "
            f"(net EV at entry: {_fmt_net_ev(decision.net_ev_at_size)}) -- zero real risk, auto-filled."
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

    size_ok, size_note = _resolved_size_ok(spec, decision)
    if not size_ok:
        print(f"[executor] {tag}: {label} order abandoned -- {size_note}")
        return
    if size_note:
        print(f"[executor] {tag}: {label} resized -- {size_note}")

    if mode == "live":
        breach = _live_budget_breach(spec.notional_usd)
        if breach:
            print(f"[executor] LIVE: {label} entry BLOCKED by a risk backstop -- {breach}")
            return

    result = wallet_client.submit_order(spec, live=(mode == "live"))

    if mode == "live":
        _record_attempt("entry", decision.station_icao, spec, result,
                        target_date=decision.target_date, bucket_c=decision.bucket_c,
                        side=decision.side)

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
        #
        # entry_price is spec.expected_price, NOT spec.limit_price. The limit
        # is padded, and OrderSpec says what each field is: limit_price is
        # "the worst price accepted" while notional_usd is "expected_price *
        # size_shares -- the likely cost". Storing the padded limit as the
        # price PAID contradicts _pad_limit's own docstring ("padding does not
        # mean paying more, it means being willing to") and, worse, made the
        # row internally inconsistent: size_usd was the cost at one price and
        # entry_price was another, so size_usd / entry_price disagreed with
        # size_shares by exactly the pad. Measured 2026-08-17 across the
        # stored simulation rows: 2 of 7 were off, by one tick and two ticks,
        # and only those two had a pad above a single tick.
        #
        # The live rung has never had this gap -- it stores entry_price =
        # fill_price alongside size_usd = fill_price * fill_shares, and all 8
        # stored live rows satisfy size_usd == entry_price * size_shares.
        # Simulation now records on the same basis, so the invariant holds on
        # both rungs and anything that multiplies shares by a price agrees
        # with anything that reads the stake.
        #
        # spec.expected_price is always set here: the ok=False return above
        # gates every path that leaves it at its 0.0 default.
        storage.open_position(make_position(
            size_usd=spec.notional_usd,
            size_shares=spec.size_shares,
            entry_price=spec.expected_price,
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
        _note_live_close_refused(position, decision)
        return

    exit_time = datetime.now(timezone.utc).isoformat()
    status = status or f"closed_{decision.reason}"

    def _economics(gross_exit_price: float):
        """
        (net exit price, net P&L, fee note) for one candidate gross price.

        Deliberately a function of the gross price rather than a value
        computed once: WHICH price is the truth differs per rung, and is
        not known until that rung has run its order. See _record().
        """
        if status == "closed_resolution":
            exit_fee_per_share = 0.0
        else:
            exit_fee_per_share = risk_manager.taker_fee_per_share(gross_exit_price)
        exit_price = max(gross_exit_price - exit_fee_per_share, 0.0)
        return (
            exit_price,
            risk_manager.compute_pnl_pct(position.entry_price, exit_price),
            f"gross {gross_exit_price:.4f} - exit fee {exit_fee_per_share:.4f}/share "
            f"= net {exit_price:.4f}",
        )

    def _record(reason_tag: str, gross_exit_price: float) -> None:
        """
        THE GROSS PRICE IS AN ARGUMENT BECAUSE IT IS NOT decision.current_price.
        ------------------------------------------------------------------
        It used to be. decision.current_price is the quote read BEFORE the
        order was built, and a FOK limit is a worst-price bound rather than
        a target -- so on every rung that actually places an order the quote
        is the one number that is certainly not what happened:

          live       -> result.fill_price (what the exchange matched at),
                        falling back to spec.limit_price exactly as the
                        entry path does when the response carries no price.
          simulation -> spec.expected_price, the tick-aligned price the
                        order would have been priced at. Same basis the
                        entry rung moved to on 2026-08-17, so the two agree.
          paper /
          manual_review /
          resolution -> decision.current_price. No order exists on these,
                        so the observed quote is the whole available truth.

        Measured on the live book 2026-08-19 (WSSS 33 YES, 12.5 shares):
        quote 0.92, limit 0.90, booked 0.9163 net. A fill at the limit was
        worth 0.8955 net -- that one exit overstated by ~$0.26 on a $1.00
        stake, and every live exit in the table carried the same bias. The
        log line at the foot of _close_via_order_path() has always PRINTED
        the fill price while the stored row said something else.
        """
        exit_price, net_pnl_pct, fee_note = _economics(gross_exit_price)
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
        _, net_pnl_pct, _ = _economics(decision.current_price)
        print(
            f"\n[ACTION NEEDED] {position.station_icao} {position.bucket_c}°C "
            f"({position.side}) -- {decision.reason.upper()}\n"
            f"  Entry: {position.entry_price:.3f}  Current: {decision.current_price:.3f}  "
            f"P&L: {decision.pnl_pct:+.1%} gross, {net_pnl_pct:+.1%} net of the exit fee\n"
            f"  Recommended: {action}\n"
        )
        # Logged as closed so it doesn't get re-flagged identically every
        # scan cycle while a human hasn't acted yet.
        _record("manual review", decision.current_price)
        return

    if mode == "paper":
        _, net_pnl_pct, fee_note = _economics(decision.current_price)
        print(
            f"[executor] PAPER EXIT: {position.station_icao} {position.bucket_c}°{position.side} "
            f"-- {decision.reason.upper()} @ {decision.current_price:.3f} ({fee_note}), "
            f"pnl={net_pnl_pct:+.1%} net of the exit fee -- zero real risk, auto-filled."
        )
        _record("paper", decision.current_price)
        return

    _close_via_order_path(position, decision, mode, status, _economics, _record)


def _close_via_order_path(
    position, decision, mode, status, economics, record,
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
        record(mode, decision.current_price)
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

    if mode == "live":
        _record_attempt("exit", position.station_icao, spec, result,
                        target_date=position.target_date, bucket_c=position.bucket_c,
                        side=position.side)

    if mode == "simulation":
        # spec.expected_price, not decision.current_price: the tick-aligned
        # price this order would have carried. Same basis as the simulated
        # ENTRY rung, so size_usd / price and size_shares agree on both legs.
        _, net_pnl_pct, fee_note = economics(spec.expected_price)
        print(
            f"[executor] SIMULATION EXIT: {label} -- {decision.reason.upper()} "
            f"order resolved but NOT submitted: {spec.describe()}; "
            f"pnl={net_pnl_pct:+.1%} net ({fee_note})."
        )
        record("simulation", spec.expected_price)
        return

    if not result.filled:
        print(
            f"[executor] LIVE: {label} exit order did NOT fill -- "
            f"{result.error or 'killed unfilled'}. Position left OPEN "
            f"(the shares are still held) and will be retried next cycle."
        )
        return

    # The one price the exchange actually matched at. The fallback to the
    # padded limit mirrors the entry path: a fill with no price attached is
    # still a fill, and the limit is its worst-case bound.
    fill_price = result.fill_price or spec.limit_price
    _, net_pnl_pct, fee_note = economics(fill_price)
    print(
        f"[executor] LIVE EXIT: {label} -- {decision.reason.upper()} "
        f"{spec.size_shares:.2f} shares @ {fill_price:.4f} "
        f"(order {result.order_id}), pnl={net_pnl_pct:+.1%} net ({fee_note}). REAL MONEY."
    )
    record("live", fill_price)
