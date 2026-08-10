"""
clients/wallet_client.py

PURPOSE
-------
The one module in this codebase capable of moving real funds. Wraps
Polymarket's official py-clob-client-v2 for order signing/placement,
kept deliberately separate from market_client.py (read-only prices)
so the code path that can spend money is small, isolated, and easy
to audit -- consistent with how executor.py already separates
decision-making from execution.

VERIFIED AGAINST THE INSTALLED PACKAGE, NOT GUESSED
-----------------------------------------------------
Every library fact this module depends on was read out of
py_clob_client_v2 1.0.2 as installed in this environment, not from a
tutorial:

  - ClobClient.get_tick_size(token_id) -> one of
    "0.1"|"0.01"|"0.005"|"0.0025"|"0.001"|"0.0001"   (client.py:345)
  - OrderArgsV2(token_id, price, size, side, ...)     (clob_types.py)
  - ROUNDING_CONFIG[tick_size] -> RoundConfig(price=N, size=2, amount=M)
    (order_builder/builder.py:36) -- note size=2 AT EVERY TICK SIZE
  - build_order() applies round_down(size, round_config.size)
    (order_builder/builder.py:77, :87) -- share counts are ROUNDED DOWN
  - create_order() raises PolyException on a price that is not
    tick-valid                                        (client.py:726)
  - the public order book carries min_order_size       (utilities.py:17)

THE $1 PROBLEM, AND WHY THIS MODULE ROUNDS SHARES UP
-------------------------------------------------------
Because the order builder rounds share counts DOWN to 2 decimals, a
naive $1.00 order is submitted for slightly LESS than $1.00: at price
0.33, 1.00/0.33 = 3.0303 shares, rounded down to 3.03, i.e. $0.9999 of
notional. Wherever the market's minimum order size binds at exactly
$1, that order is rejected -- and it would be rejected intermittently,
depending on the price, which is the worst way for it to fail.

build_entry_order() therefore rounds the share count UP onto the same
2-decimal grid the builder rounds down to, so what gets submitted is
at or just above the requested notional and survives the builder's
rounding unchanged. config.LIVE_SIZE_OVERSHOOT_CEILING_USD bounds how
far that is allowed to push the order past the requested size.

WHAT IS STILL UNVERIFIED -- READ BEFORE GOING LIVE
-----------------------------------------------------
The exact semantics and units of the book's min_order_size field
(shares vs. dollars) have NOT been confirmed against a live response
in this environment, only against the parsing code in utilities.py.
preflight_entry() treats it as a SHARE count and separately enforces a
dollar floor, which is the conservative reading -- but confirm it
against a real book pull before the first live order, and do not
assume a passing simulation run has confirmed it.

SIGNATURE TYPE 3 IS BROKEN (unchanged, still current)
--------------------------------------------------------
Polymarket/py-clob-client-v2 #70 (filed 2026-05-19): signature_type=3
(POLY_1271, "deposit wallet") order placement fails for new accounts
-- L1 auth binds the API key to the EOA instead of the deposit wallet.
The documented working path for a NEW account is signature_type=1
(POLY_PROXY) via a Magic-wallet email account. DEFAULT_SIGNATURE_TYPE
reflects this; do not change it to 3 without checking that issue.

THE TWO GATES
--------------
No real order is submitted unless BOTH are true:
  1. executor's per-station mode for the station is "live"
  2. POLYMARKET_LIVE_TRADING=true in the environment
"simulation" mode satisfies neither and runs every step below except
the final submit -- deliberately, so the code that will move money is
the code being exercised.

DEPENDENCIES
------------
py-clob-client-v2 (LAZY-IMPORTED -- see _clob() below)
market_client.py (local, read-only book access)
os, logging, math, dataclasses (standard library)
"""

import math
import os
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# See SIGNATURE TYPE 3 note above. Kept as a plain int so that importing
# this module does not require py_clob_client_v2 to be installed; it is
# converted at client-construction time.
DEFAULT_SIGNATURE_TYPE = 1  # POLY_PROXY, Magic-wallet email account

# Share counts are rounded to 2 decimals by the order builder at EVERY
# tick size (ROUNDING_CONFIG, order_builder/builder.py:36). Mirrored here
# because build_entry_order() has to round UP onto the same grid.
SHARE_DECIMALS = 2

# Fallback floor used only when the book does not report min_order_size.
# Polymarket has historically enforced a $1 minimum; treating an absent
# field as "no minimum" would be the unsafe direction to guess in.
ASSUMED_MIN_ORDER_USD = 1.0

_client = None


# --------------------------------------------------------------------------
# Library access -- lazy, so a paper-only box need not install the package
# --------------------------------------------------------------------------

def _clob():
    """
    Import py_clob_client_v2 on demand and return the module.

    LAZY ON PURPOSE. deploy/deploy_daemon.sh deliberately does NOT install
    py-clob-client-v2 on the paper-trading box, and executor.py imports
    this module unconditionally. A module-level `from py_clob_client_v2
    import ...` therefore takes down the entire scheduler on every paper
    deployment the moment executor grows an import of this file -- an
    outage caused purely by code that box will never execute.
    """
    try:
        import py_clob_client_v2
        return py_clob_client_v2
    except ImportError as exc:
        raise RuntimeError(
            "py-clob-client-v2 is not installed, so no order can be built or "
            "submitted. Install it (pip install py-clob-client-v2) on any box "
            "running a station in 'simulation' or 'live' mode. Paper and "
            "manual_review modes never reach this code."
        ) from exc


def library_available() -> bool:
    """Whether the CLOB library can be imported, without raising if it can't."""
    try:
        _clob()
        return True
    except RuntimeError:
        return False


# --------------------------------------------------------------------------
# Safety gates
# --------------------------------------------------------------------------

def live_trading_enabled() -> bool:
    """
    The environment half of the two-gate check. Independent of executor's
    per-station mode: a station set to "live" in EXECUTION_MODE is NOT
    sufficient on its own, and neither is this.
    """
    return os.environ.get("POLYMARKET_LIVE_TRADING", "").lower() == "true"


def get_client():
    """
    Lazily construct and cache the authenticated ClobClient, reading
    credentials from environment variables -- never hardcoded, never
    passed as function arguments (so they cannot end up in a log line or
    a traceback frame).

    Required env vars:
      POLYMARKET_PRIVATE_KEY   -- EOA private key that controls the account
      POLYMARKET_FUNDER        -- address holding the funds (the proxy /
                                  Magic wallet, per signature_type=1)

    NOT called by simulation mode. Everything simulation needs (tick size,
    minimum order size, book depth) comes from the PUBLIC endpoints via
    market_client, so a simulation run needs no private key on the box at
    all. That is the point: credentials arrive only at the last step.
    """
    global _client
    if _client is not None:
        return _client

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    funder = os.environ.get("POLYMARKET_FUNDER")

    if not private_key or not funder:
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER must both be set as "
            "environment variables before wallet_client can authenticate. "
            "Refusing to proceed with partial/missing credentials."
        )

    lib = _clob()
    client = lib.ClobClient(
        CLOB_HOST,
        chain_id=POLYGON_CHAIN_ID,
        key=private_key,
        signature_type=DEFAULT_SIGNATURE_TYPE,
        funder=funder,
    )
    # create_or_derive_api_KEY, not ..._api_creds. The previous name does not
    # exist on the installed client (verified against py_clob_client_v2 1.0.2:
    # dir(ClobClient) has create_or_derive_api_key and no ..._creds), so the
    # first authenticated call would have died with an AttributeError -- proof
    # this path had never actually been executed.
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)

    _client = client
    return _client


# --------------------------------------------------------------------------
# Order construction -- the part simulation mode exists to exercise
# --------------------------------------------------------------------------

@dataclass
class OrderSpec:
    """
    A fully-resolved, submittable order -- or a refusal explaining why one
    could not be built. `ok` is the only field a caller should branch on.

    size_shares is what actually gets submitted; notional_usd is what it
    will really cost, which is NOT necessarily the size_usd that was
    requested (see the rounding note in the module docstring). Callers
    must record notional_usd as the position size, not the requested
    figure, or stored P&L drifts from reality on every single trade.
    """
    ok: bool
    token_id: str
    side: str                      # "BUY" or "SELL"
    limit_price: float             # tick-aligned
    size_shares: float             # on the 2-decimal grid the builder uses
    notional_usd: float            # limit_price * size_shares
    tick_size: str = ""
    min_order_size: Optional[float] = None
    requested_size_usd: float = 0.0
    requested_price: float = 0.0
    reason: str = ""

    def describe(self) -> str:
        if not self.ok:
            return f"REFUSED ({self.reason})"
        return (
            f"{self.side} {self.size_shares:.2f} shares @ {self.limit_price:.4f} "
            f"= ${self.notional_usd:.4f} (tick {self.tick_size}, "
            f"requested ${self.requested_size_usd:.2f} @ {self.requested_price:.4f})"
        )


def _round_up_to_grid(value: float, decimals: int) -> float:
    """
    Round UP onto a decimal grid. The mirror of the order builder's
    round_down(size, 2): submitting a value already on the grid means the
    builder's rounding is a no-op and what we computed is what executes.
    """
    factor = 10 ** decimals
    return math.ceil(value * factor - 1e-9) / factor


def _align_price_to_tick(price: float, tick_size: str, side: str) -> float:
    """
    Snap a limit price onto the market's tick grid.

    Direction matters and is NOT symmetric. A BUY limit rounds UP and a
    SELL limit rounds DOWN -- i.e. both round in the direction that keeps
    the order marketable. Rounding a buy limit down instead would push it
    below the ask and guarantee a fill-or-kill order gets killed, which
    looks exactly like "no liquidity" while actually being a rounding bug.
    The cost of rounding the safe way is at most one tick.
    """
    tick = float(tick_size)
    steps = price / tick
    if side == "BUY":
        aligned = math.ceil(steps - 1e-9) * tick
    else:
        aligned = math.floor(steps + 1e-9) * tick
    # Re-round to kill binary float dust (0.30000000000000004 is not
    # tick-valid as far as the library's price_valid() is concerned).
    decimals = max(0, len(tick_size.split(".")[-1])) if "." in tick_size else 0
    aligned = round(aligned, decimals)
    return min(max(aligned, tick), 1.0 - tick)


def _book_constraints(token_id: str) -> tuple:
    """
    Read (tick_size, min_order_size) from the PUBLIC order book -- no
    credentials required, which is what lets simulation mode validate a
    real order without a private key on the box.

    Returns (tick_size_str_or_None, min_order_size_or_None).
    """
    try:
        import market_client
    except ImportError:
        from clients import market_client  # type: ignore

    book = market_client.get_order_book(token_id)
    if not book:
        return None, None

    tick = book.get("tick_size")
    min_size = book.get("min_order_size")
    try:
        min_size = float(min_size) if min_size is not None else None
    except (TypeError, ValueError):
        min_size = None
    return (str(tick) if tick is not None else None), min_size


def build_entry_order(token_id: str, price: float, size_usd: float) -> OrderSpec:
    """
    Resolve an approved entry into an exactly-submittable order, or refuse
    with a reason. Performs no network writes and needs no credentials --
    this is the whole of what simulation mode runs.

    Refuses (rather than silently adjusting) when the order cannot be made
    to satisfy the market's minimum without exceeding
    config.LIVE_SIZE_OVERSHOOT_CEILING_USD. At a $1 trade size that
    refusal is a REAL and EXPECTED outcome on higher-priced buckets, not
    an error: at price 0.90 the minimum viable order is already $0.90+ and
    a market minimum of 5 shares would demand $4.50. Better to decline the
    trade than to quietly place one 4x the intended size.
    """
    import config

    if price is None or price <= 0 or price >= 1:
        return OrderSpec(
            ok=False, token_id=token_id, side="BUY", limit_price=0.0,
            size_shares=0.0, notional_usd=0.0, requested_size_usd=size_usd,
            requested_price=price or 0.0,
            reason=f"price {price} is outside (0, 1) -- not a tradeable quote",
        )

    tick_size, min_order_size = _book_constraints(token_id)
    if tick_size is None:
        return OrderSpec(
            ok=False, token_id=token_id, side="BUY", limit_price=0.0,
            size_shares=0.0, notional_usd=0.0, requested_size_usd=size_usd,
            requested_price=price, min_order_size=min_order_size,
            reason="order book unavailable -- cannot resolve tick size, refusing to guess it",
        )

    limit_price = _align_price_to_tick(price, tick_size, "BUY")

    # Round shares UP onto the builder's 2-decimal grid so the submitted
    # notional is >= the requested one and survives round_down() intact.
    size_shares = _round_up_to_grid(size_usd / limit_price, SHARE_DECIMALS)
    notional = round(size_shares * limit_price, 6)

    spec = OrderSpec(
        ok=True, token_id=token_id, side="BUY", limit_price=limit_price,
        size_shares=size_shares, notional_usd=notional, tick_size=tick_size,
        min_order_size=min_order_size, requested_size_usd=size_usd,
        requested_price=price,
    )

    # Minimum order size. Treated as a SHARE count -- probed against the live
    # API on 2026-08-10 ("mos":5 on the WSSS buckets), with an independent
    # dollar floor underneath it in case a separate notional minimum exists
    # that the market metadata does not advertise. See the extended note in
    # config.py: at a 5-share minimum a $1 order is legal only at price <=
    # 0.20, so the refusal below is the EXPECTED outcome on most buckets.
    def _bump_to(target_shares: float, what: str) -> bool:
        """Raise the order to `target_shares`, or refuse. True if refused."""
        bumped_shares = _round_up_to_grid(target_shares, SHARE_DECIMALS)
        bumped_notional = round(bumped_shares * limit_price, 6)
        if not config.LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE:
            spec.ok = False
            spec.reason = (
                f"{what} requires ${bumped_notional:.2f} at {limit_price:.4f}, above "
                f"the requested ${size_usd:.2f}, and "
                f"LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE is off -- declining rather "
                f"than spending more than the configured trade size"
            )
            return True
        if bumped_notional > config.LIVE_SIZE_OVERSHOOT_CEILING_USD:
            spec.ok = False
            spec.reason = (
                f"{what} costs ${bumped_notional:.2f} at {limit_price:.4f}, past the "
                f"${config.LIVE_SIZE_OVERSHOOT_CEILING_USD:.2f} overshoot ceiling "
                f"on a ${size_usd:.2f} trade -- declining rather than oversizing"
            )
            return True
        spec.size_shares = bumped_shares
        spec.notional_usd = bumped_notional
        return False

    if min_order_size is not None and size_shares < min_order_size:
        if _bump_to(min_order_size, f"market minimum of {min_order_size} shares"):
            return spec

    if spec.notional_usd < ASSUMED_MIN_ORDER_USD:
        if _bump_to(ASSUMED_MIN_ORDER_USD / limit_price,
                    f"${ASSUMED_MIN_ORDER_USD:.2f} notional floor"):
            return spec

    if spec.notional_usd > config.LIVE_SIZE_OVERSHOOT_CEILING_USD:
        spec.ok = False
        spec.reason = (
            f"resolved notional ${spec.notional_usd:.2f} exceeds the "
            f"${config.LIVE_SIZE_OVERSHOOT_CEILING_USD:.2f} overshoot ceiling"
        )
        return spec

    spec.reason = "order resolved"
    return spec


def build_exit_order(token_id: str, price: float, size_shares: float) -> OrderSpec:
    """
    Resolve an exit into a submittable SELL. Sizing is a SHARE count read
    off the position, never re-derived from dollars: the number of shares
    to sell is exactly the number bought, and recomputing it from
    size_usd/price at exit time would use the wrong price and try to sell
    shares that do not exist.
    """
    import config  # noqa: F401  (kept symmetric with build_entry_order)

    if price is None or price <= 0 or price >= 1:
        return OrderSpec(
            ok=False, token_id=token_id, side="SELL", limit_price=0.0,
            size_shares=0.0, notional_usd=0.0, requested_price=price or 0.0,
            reason=f"price {price} is outside (0, 1) -- not a tradeable quote",
        )
    if not size_shares or size_shares <= 0:
        return OrderSpec(
            ok=False, token_id=token_id, side="SELL", limit_price=0.0,
            size_shares=0.0, notional_usd=0.0, requested_price=price,
            reason="position carries no recorded share count -- refusing to guess how much to sell",
        )

    tick_size, min_order_size = _book_constraints(token_id)
    if tick_size is None:
        return OrderSpec(
            ok=False, token_id=token_id, side="SELL", limit_price=0.0,
            size_shares=0.0, notional_usd=0.0, requested_price=price,
            reason="order book unavailable -- cannot resolve tick size, refusing to guess it",
        )

    limit_price = _align_price_to_tick(price, tick_size, "SELL")
    # Round the SELL size DOWN: selling more shares than are held fails
    # outright, and the residual dust is worth far less than a failed exit.
    factor = 10 ** SHARE_DECIMALS
    sell_shares = math.floor(size_shares * factor + 1e-9) / factor

    spec = OrderSpec(
        ok=sell_shares > 0,
        token_id=token_id, side="SELL", limit_price=limit_price,
        size_shares=sell_shares, notional_usd=round(sell_shares * limit_price, 6),
        tick_size=tick_size, min_order_size=min_order_size,
        requested_price=price,
        reason="order resolved" if sell_shares > 0 else "share count rounds to zero",
    )

    # THE EXIT LEG CARRIES THE SAME MINIMUM AS THE ENTRY LEG.
    #
    # This is the check that makes small sizing dangerous rather than merely
    # conservative: a position too small to sell cannot be exited AT ALL --
    # every stop-loss, trailing stop and profit-take is dead for its whole
    # life, and it can only come off the book by resolving. build_entry_order()
    # is what actually prevents this, by refusing to open a position whose
    # share count is under the market minimum in the first place. This check
    # is the backstop for positions that predate that rule or were opened by
    # hand, and it fails LOUDLY rather than submitting a doomed order.
    if spec.ok and min_order_size is not None and sell_shares < min_order_size:
        spec.ok = False
        spec.reason = (
            f"{sell_shares} shares is below the market minimum of {min_order_size} "
            f"-- this position cannot be sold and can only come off the book by "
            f"resolving. It should never have been opened at this size"
        )
    return spec


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------

@dataclass
class OrderResult:
    """
    Outcome of a submission attempt. `filled` is the field that decides
    whether a position exists -- NOT `submitted`, and never the mere
    presence of an order id.
    """
    submitted: bool
    filled: bool
    simulated: bool
    spec: OrderSpec
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_shares: Optional[float] = None
    raw: Optional[dict] = None
    error: str = ""


def submit_order(spec: OrderSpec, live: bool) -> OrderResult:
    """
    Submit a resolved OrderSpec as a FILL-OR-KILL order.

    WHY FOK, NOT GTC
    ----------------
    The previous implementation posted a GTC limit order and returned. A
    GTC order that comes back with an order id has been ACCEPTED, not
    FILLED -- it rests on the book until someone crosses it. Recording
    that as an open position creates a position whose shares do not
    exist, and the exit path will later try to sell them. FOK collapses
    submission and fill into a single event: it either fills completely
    and immediately, or it is killed and nothing happened. There is no
    intermediate state to reconcile, and therefore no way to get the
    reconciliation wrong.

    The price protection that GTC was chosen for is preserved: this is
    still a LIMIT order at spec.limit_price, so it cannot fill worse than
    the price entry_manager approved. FOK only removes the waiting.

    live=False runs every step up to (not including) submission and
    returns simulated=True. Both gates must agree for live=True to
    actually submit -- this function re-checks the environment gate
    itself rather than trusting its caller.
    """
    if not spec.ok:
        return OrderResult(
            submitted=False, filled=False, simulated=not live, spec=spec,
            error=f"refusing to submit an unresolved order: {spec.reason}",
        )

    if not live:
        logger.info(f"[wallet_client] SIMULATION -- not submitting: {spec.describe()}")
        return OrderResult(submitted=False, filled=False, simulated=True, spec=spec)

    if not live_trading_enabled():
        logger.warning(
            "[wallet_client] live=True but POLYMARKET_LIVE_TRADING is not 'true' -- "
            "second gate closed, refusing to submit. This is the safe outcome, not an error."
        )
        return OrderResult(
            submitted=False, filled=False, simulated=True, spec=spec,
            error="POLYMARKET_LIVE_TRADING is not set to 'true' (second gate closed)",
        )

    lib = _clob()
    client = get_client()
    order_args = lib.OrderArgs(
        token_id=spec.token_id,
        price=spec.limit_price,
        size=spec.size_shares,
        side=lib.Side.BUY if spec.side == "BUY" else lib.Side.SELL,
    )

    try:
        signed = client.create_order(order_args)
        resp = client.post_order(signed, lib.OrderType.FOK)
    except Exception as exc:  # noqa: BLE001 -- any failure here means "no fill"
        logger.error(f"[wallet_client] order submission FAILED: {spec.describe()} -- {exc}")
        return OrderResult(
            submitted=True, filled=False, simulated=False, spec=spec,
            error=f"{type(exc).__name__}: {exc}",
        )

    filled, fill_price, fill_shares = _interpret_fill(resp, spec)
    logger.info(
        f"[wallet_client] LIVE order {'FILLED' if filled else 'NOT FILLED'}: "
        f"{spec.describe()} -- response={resp}"
    )
    return OrderResult(
        submitted=True, filled=filled, simulated=False, spec=spec,
        order_id=_extract(resp, "orderID", "orderId", "order_id", "id"),
        fill_price=fill_price, fill_shares=fill_shares,
        raw=resp if isinstance(resp, dict) else {"response": str(resp)},
    )


def _extract(resp, *keys):
    if not isinstance(resp, dict):
        return None
    for key in keys:
        if resp.get(key) is not None:
            return resp[key]
    return None


def _interpret_fill(resp, spec: OrderSpec) -> tuple:
    """
    Decide from a post_order response whether the order actually filled.

    DEFAULTS TO "NOT FILLED". The response shape is not pinned by a live
    capture in this environment, and the two errors are not symmetric:
    treating a real fill as unfilled leaves an untracked position that a
    human will notice on the exchange, while treating a kill as a fill
    writes a phantom position the exit path will try to sell. Only an
    explicitly successful, explicitly matched response counts.
    """
    if not isinstance(resp, dict):
        return False, None, None

    success = resp.get("success")
    status = str(resp.get("status", "")).lower()

    # THE MATCHED AMOUNT IS PARSED AS A NUMBER, NEVER TESTED FOR TRUTHINESS.
    # This used to read `bool(resp.get("takingAmount"))`, and the exchange
    # returns these as STRINGS -- so a killed FOK reporting takingAmount='0'
    # was read as a FILL, because bool('0') is True in Python. That wrote a
    # position with no shares behind it, which the exit path would later try
    # to sell: the single worst outcome this module can produce.
    #
    # The response shape is not guessed. A real matched FOK returns
    #   {'status': 'matched', 'success': True, 'takingAmount': '14.285713',
    #    'makingAmount': '0.999999', 'transactionsHashes': ['0x...'],
    #    'orderID': '0x...', 'errorMsg': ''}
    # with NO 'size_matched' key at all -- confirmed against a real on-chain
    # fill by a separate Polymarket bot on this machine
    # (~/Downloads/hermes/core/execution.py::_parse_fill_status), whose own
    # history is the mirror-image bug: it read size_matched, always got the
    # 0.0 default, and logged every genuine fill as a rejection. size_matched
    # is still honoured first in case a different shape ever supplies it.
    matched_raw = resp.get("size_matched")
    if matched_raw is None:
        matched_raw = resp.get("takingAmount") or resp.get("makingAmount")
    try:
        matched = float(matched_raw) if matched_raw is not None else 0.0
    except (TypeError, ValueError):
        matched = 0.0

    # An affirmative status AND a positive matched amount. "live"/"delayed"
    # mean the order is resting or queued, which for a FOK should not happen
    # -- and if it does, it is emphatically not a fill.
    if success is False or status in ("unmatched", "cancelled", "canceled", "killed"):
        return False, None, None
    if status not in ("matched", "filled") or matched <= 0:
        return False, None, None

    fill_shares = None
    fill_price = None
    taking = resp.get("takingAmount")
    making = resp.get("makingAmount")
    try:
        if taking is not None and making is not None:
            taking, making = float(taking), float(making)
            # BUY: taking = shares received, making = USDC paid.
            if spec.side == "BUY" and taking > 0:
                fill_shares, fill_price = taking, making / taking
            elif spec.side == "SELL" and making > 0:
                fill_shares, fill_price = making, taking / making
    except (TypeError, ValueError, ZeroDivisionError):
        fill_shares, fill_price = None, None

    return True, fill_price or spec.limit_price, fill_shares or spec.size_shares


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def preflight(token_id: Optional[str] = None) -> list:
    """
    Checks to run BEFORE the first live order, returned as a list of
    human-readable "[ok]/[--]/[!!]" lines rather than raising. Called by
    executor on every simulation-mode entry so the report is produced
    continuously, not once at setup time and then assumed to still hold.

    Deliberately does NOT attempt to set allowances: that is a real
    on-chain transaction and belongs to the operator, not to a trading
    loop.
    """
    lines = []
    lines.append(
        f"[{'ok' if library_available() else '!!'}] py-clob-client-v2 importable"
    )
    lines.append(
        f"[{'ok' if live_trading_enabled() else '--'}] POLYMARKET_LIVE_TRADING=true "
        f"(gate 2 of 2; '--' means live orders are blocked)"
    )
    has_creds = bool(os.environ.get("POLYMARKET_PRIVATE_KEY")) and bool(
        os.environ.get("POLYMARKET_FUNDER")
    )
    lines.append(
        f"[{'ok' if has_creds else '--'}] POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER set "
        f"(not needed for simulation)"
    )

    if token_id:
        tick, min_size = _book_constraints(token_id)
        lines.append(
            f"[{'ok' if tick else '!!'}] order book reachable -- tick_size={tick}, "
            f"min_order_size={min_size}"
        )

    lines.append(
        "[--] on-chain USDC + conditional-token allowances (spender: the CLOB "
        "Exchange contract) must be set once per funding address. NOT automated "
        "here and NOT detectable from the public book -- confirm manually before "
        "the first live order."
    )
    return lines


def check_allowances_reminder() -> str:
    """
    Retained for callers of the previous API. preflight() is the fuller
    replacement.
    """
    return (
        "Token allowances must be set on-chain before trading, once per "
        "funding address. This is NOT automated by this module -- confirm "
        "allowances are set before enabling live trading."
    )
