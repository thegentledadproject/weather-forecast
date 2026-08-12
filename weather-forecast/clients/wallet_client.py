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

SIGNATURE TYPE 3, SETTLED BY A REAL ORDER ON 2026-08-11
----------------------------------------------------------
DEFAULT_SIGNATURE_TYPE is 3 (POLY_1271, deposit wallet). It was 1 until
this codebase placed its first live order and got the answer directly.

This module used to cite Polymarket/py-clob-client-v2 #70 (2026-05-19)
-- type 3 failing for new accounts because L1 auth binds the API key to
the EOA rather than the deposit wallet -- and default to 1 on that
basis. hermes' .env.example, edited two months after that issue, said
the opposite: 0 and 1 both rejected since the CLOB V2 go-live, 3 the
only working option. hermes was right, at least for this account.

THE EVIDENCE, because "1 vs 3" is not something to re-litigate from
memory later:

  - type 1: L1 auth SUCCEEDS (create 400s because a key already exists,
    the derive fallback returns real creds) -- so an auth check does NOT
    detect the problem. The order is then rejected at submit with
    400 "maker address not allowed, please use the deposit wallet flow".
  - type 1: get_balance_allowance reads balance=0, allowances=0. THIS IS
    A WRONG-ADDRESS ARTIFACT, NOT AN EMPTY WALLET. _wait_for_balance()
    logs "balance still reads 0 after every propagation check" and
    blames funding or allowances; under the wrong signature type that
    message is misleading, so check the signature type FIRST.
  - type 3: same credentials, same account -- real nonzero balance, and
    max-uint256 allowances on all three spender contracts.

That last line also closes the old "allowances are unverified and not
detectable from the public book" caveat: they are set. get_balance_
allowance() under the correct signature type is how you check.

Still overridable via POLYMARKET_SIGNATURE_TYPE (see _signature_type())
-- a different account, e.g. a fresh Magic-wallet one that never walked
the deposit-wallet flow, may genuinely need 1.

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
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# See the SIGNATURE TYPE note above. Kept as a plain int so that importing
# this module does not require py_clob_client_v2 to be installed; it is
# converted at client-construction time. This is only the DEFAULT --
# POLYMARKET_SIGNATURE_TYPE overrides it, see _signature_type().
DEFAULT_SIGNATURE_TYPE = 3  # POLY_1271, deposit wallet -- proven 2026-08-11

# 0 = EOA/MetaMask, 1 = POLY_PROXY (Magic-wallet email), 3 = POLY_1271
# (deposit wallet). Anything else is a typo, not a mode.
KNOWN_SIGNATURE_TYPES = (0, 1, 3)

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


def _signature_type() -> int:
    """
    Resolve the wallet signature type, POLYMARKET_SIGNATURE_TYPE overriding
    DEFAULT_SIGNATURE_TYPE. Unset or empty keeps today's behaviour exactly.

    Exists so the value can be changed without editing the one module
    that can spend money. It earned its keep immediately: it is how 3 was
    tested against the live account before becoming the default, and it
    is the escape hatch for an account that needs a different type (a
    fresh Magic-wallet account that never walked the deposit-wallet flow
    would want 1).

    THE PARSE GUARD IS NOT DEFENSIVE PADDING. It is ported from hermes'
    build_client(), which learned it the hard way: systemd's
    EnvironmentFile= parser does NOT strip inline "# comment" text the way
    python-dotenv does (systemd issue #12527). If .env ever contains
    POLYMARKET_SIGNATURE_TYPE=1 # some comment on one line, systemd passes
    the ENTIRE remainder -- comment included -- as the literal value. That
    matters more here than it did there: this codebase loads no .env at
    all, so systemd's parser is the ONLY one that will ever read the file,
    and there is no dotenv fallback to paper over the difference. Without
    this guard the failure is an uncaught ValueError with no hint of the
    cause.
    """
    raw = os.environ.get("POLYMARKET_SIGNATURE_TYPE")
    if raw is None or not raw.strip():
        return DEFAULT_SIGNATURE_TYPE

    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(
            f"POLYMARKET_SIGNATURE_TYPE must be an integer, got: '{raw}'. "
            f"If that looks like a number with trailing text, check the env "
            f"file for an inline '# comment' on that line -- systemd's "
            f"EnvironmentFile parser does not strip those, so the comment "
            f"text is appended to the value. Put comments on their own line."
        ) from None

    if value not in KNOWN_SIGNATURE_TYPES:
        logger.warning(
            f"[wallet_client] POLYMARKET_SIGNATURE_TYPE={value} is not one of "
            f"{KNOWN_SIGNATURE_TYPES}; passing it through, but this is almost "
            f"certainly a typo and orders will be rejected."
        )
    return value


def _explicit_creds(lib):
    """
    ApiCreds from CLOB_API_KEY / CLOB_SECRET / CLOB_PASS_PHRASE, or None if
    none of them are set. Raises if only some are.

    WHY THESE EXIST, HAVING ONCE BEEN DOCUMENTED AS POINTLESS. This module
    used to derive credentials with create_or_derive_api_key() and nothing
    else, and .env.example told operators these three variables would have
    no effect. That was wrong under signature type 3, and the failure is
    not obvious from either end. Read the SDK's own order builder:

        def _v2_order_signer(self) -> str:
            if self.signature_type == SignatureTypeV2.POLY_1271:
                return self.funder
            return self.signer.address()

    Under type 3 the order's `signer` field is the FUNDER (the deposit
    wallet), while `owner` is creds.api_key -- and a derived key is bound
    to whichever address performed L1 auth, which is the EOA behind
    POLYMARKET_PRIVATE_KEY. When funder != EOA, the exchange rejects the
    order with 400 "the order signer address has to be the address of the
    API KEY". That is py-clob-client-v2 #70 exactly, and no amount of
    re-deriving fixes it: L1 auth can only ever speak for the EOA.

    So a deposit-wallet account needs credentials REGISTERED TO THE
    DEPOSIT WALLET, minted out of band (hermes does this with
    generate_creds.py / setup_deposit_wallet.py plus builder credentials
    and the relayer; nothing in this repo provisions them). Supplying them
    here is the only way this codebase can trade such an account.

    Fails closed on a partial set, like the private key / funder check:
    two out of three silently falling back to derived credentials would
    reproduce the same rejection with none of the explanation.
    """
    key        = (os.environ.get("CLOB_API_KEY")     or "").strip()
    secret     = (os.environ.get("CLOB_SECRET")      or "").strip()
    passphrase = (os.environ.get("CLOB_PASS_PHRASE") or "").strip()

    present = {"CLOB_API_KEY": bool(key), "CLOB_SECRET": bool(secret),
               "CLOB_PASS_PHRASE": bool(passphrase)}
    if all(present.values()):
        return lib.ApiCreds(api_key=key, api_secret=secret, api_passphrase=passphrase)
    if any(present.values()):
        missing = [k for k, v in present.items() if not v]
        raise RuntimeError(
            f"Partial CLOB API credentials: {', '.join(missing)} not set. "
            f"Set all three or none. Falling back to derived credentials "
            f"here would silently produce orders the exchange rejects with "
            f"'the order signer address has to be the address of the API KEY' "
            f"whenever POLYMARKET_FUNDER is not the private key's own address."
        )
    return None


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

    signature_type = _signature_type()
    if signature_type != DEFAULT_SIGNATURE_TYPE:
        logger.warning(
            f"[wallet_client] signature_type={signature_type} from "
            f"POLYMARKET_SIGNATURE_TYPE (default is {DEFAULT_SIGNATURE_TYPE})"
        )

    lib = _clob()
    creds = _explicit_creds(lib)

    client = lib.ClobClient(
        CLOB_HOST,
        chain_id=POLYGON_CHAIN_ID,
        key=private_key,
        creds=creds,
        signature_type=signature_type,
        funder=funder,
    )

    if creds is None:
        # create_or_derive_api_KEY, not ..._api_creds. The previous name does
        # not exist on the installed client (verified against
        # py_clob_client_v2 1.0.2: dir(ClobClient) has create_or_derive_api_key
        # and no ..._creds), so the first authenticated call would have died
        # with an AttributeError -- proof this path had never been executed.
        client.set_api_creds(client.create_or_derive_api_key())
    else:
        # NOT overwritten with a derived key, which is the entire point --
        # see _explicit_creds(). Deriving here would discard credentials that
        # belong to the deposit wallet and replace them with EOA-bound ones,
        # reintroducing the rejection they were supplied to avoid.
        logger.info("[wallet_client] using explicit CLOB API credentials from the environment")

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
    limit_price: float             # tick-aligned, PADDED -- the worst price accepted
    size_shares: float             # on the 2-decimal grid the builder uses
    notional_usd: float            # expected_price * size_shares -- the likely cost
    tick_size: str = ""
    min_order_size: Optional[float] = None
    requested_size_usd: float = 0.0
    requested_price: float = 0.0
    reason: str = ""
    # The tick-aligned quote BEFORE padding: what this order is expected to
    # fill at, and what size_shares/notional_usd are computed from. Padding
    # widens only what we are willing to accept, never what we expect to
    # pay -- see _pad_limit().
    expected_price: float = 0.0

    @property
    def max_cost_usd(self) -> float:
        """Worst case: every share filling at the padded limit."""
        return round(self.size_shares * self.limit_price, 6)

    def describe(self) -> str:
        if not self.ok:
            return f"REFUSED ({self.reason})"
        pad = ""
        if self.expected_price and self.limit_price != self.expected_price:
            pad = f", limit {self.limit_price:.4f} worst-case ${self.max_cost_usd:.4f}"
        shown = self.expected_price or self.limit_price
        return (
            f"{self.side} {self.size_shares:.2f} shares @ {shown:.4f} "
            f"= ${self.notional_usd:.4f}{pad} (tick {self.tick_size}, "
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


def _pad_limit(price: float, tick_size: str, side: str) -> float:
    """
    Widen a tick-aligned limit in the direction that helps it fill.

    A Polymarket limit is a WORST-PRICE bound, not a target -- the FOK fills
    at the best price available up to it. Padding therefore does not mean
    paying more, it means being willing to, and it costs exactly nothing
    when the book has not moved. Submitting at the observed quote instead
    means one adverse tick between reading the book and matching kills an
    order that had already cleared every gate.

    The pad is the SMALLER of config.LIVE_LIMIT_PAD_TICKS ticks and
    LIVE_LIMIT_PAD_MAX_PCT of the price, and the result is then aligned
    INWARD -- down for a BUY, up for a SELL -- so snapping to the grid can
    never push it back past the cap. Aligning outward instead turned a
    capped 3% pad into 5.1% at ask 0.39 on a 0.01 tick, which is how a cap
    stops being one. Where the cap lands below a single tick the result is
    the unpadded price: the previous behaviour, and the right one there.

    BUY pads UP (accept paying more), SELL pads DOWN (accept receiving
    less), and both stay inside [tick, 1 - tick].
    """
    import config

    tick = float(tick_size)
    decimals = len(tick_size.split(".")[-1]) if "." in tick_size else 0
    by_ticks = config.LIVE_LIMIT_PAD_TICKS * tick
    by_pct = price * config.LIVE_LIMIT_PAD_MAX_PCT
    pad = min(by_ticks, by_pct)
    if pad < tick:
        return price

    if side == "BUY":
        # Align DOWN so the padded price stays at or under the cap.
        padded = math.floor((price + pad) / tick + 1e-9) * tick
    else:
        padded = math.ceil((price - pad) / tick - 1e-9) * tick

    padded = round(padded, decimals)
    padded = min(max(padded, tick), 1.0 - tick)
    # Never end up worse than where we started.
    return max(padded, price) if side == "BUY" else min(padded, price)


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

    expected_price = _align_price_to_tick(price, tick_size, "BUY")
    # The submitted limit is padded; the SIZE is computed from the expected
    # price. Sizing off the padded limit would buy fewer shares than the
    # money asked for, on the assumption of a worst case that usually does
    # not happen -- the pad is protection, not a forecast.
    limit_price = _pad_limit(expected_price, tick_size, "BUY")

    # Round shares UP onto the builder's 2-decimal grid so the submitted
    # notional is >= the requested one and survives round_down() intact.
    size_shares = _round_up_to_grid(size_usd / expected_price, SHARE_DECIMALS)
    notional = round(size_shares * expected_price, 6)

    spec = OrderSpec(
        ok=True, token_id=token_id, side="BUY", limit_price=limit_price,
        size_shares=size_shares, notional_usd=notional, tick_size=tick_size,
        min_order_size=min_order_size, requested_size_usd=size_usd,
        requested_price=price, expected_price=expected_price,
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
        bumped_notional = round(bumped_shares * expected_price, 6)
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
                f"{what} costs ${bumped_notional:.2f} at {expected_price:.4f}, past the "
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
        if _bump_to(ASSUMED_MIN_ORDER_USD / expected_price,
                    f"${ASSUMED_MIN_ORDER_USD:.2f} notional floor"):
            return spec

    if spec.notional_usd > config.LIVE_SIZE_OVERSHOOT_CEILING_USD:
        spec.ok = False
        spec.reason = (
            f"resolved notional ${spec.notional_usd:.2f} exceeds the "
            f"${config.LIVE_SIZE_OVERSHOOT_CEILING_USD:.2f} overshoot ceiling"
        )
        return spec

    # The padded limit is what the exchange may actually charge, so the
    # ceiling has to bind on the WORST case, not the expected one --
    # otherwise the pad is a hole in the only cap on trade size.
    if spec.max_cost_usd > config.LIVE_SIZE_OVERSHOOT_CEILING_USD:
        spec.ok = False
        spec.reason = (
            f"worst-case cost ${spec.max_cost_usd:.2f} at the padded limit "
            f"{spec.limit_price:.4f} exceeds the "
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

    expected_price = _align_price_to_tick(price, tick_size, "SELL")
    limit_price = _pad_limit(expected_price, tick_size, "SELL")
    # Round the SELL size DOWN: selling more shares than are held fails
    # outright, and the residual dust is worth far less than a failed exit.
    factor = 10 ** SHARE_DECIMALS
    sell_shares = math.floor(size_shares * factor + 1e-9) / factor

    spec = OrderSpec(
        ok=sell_shares > 0,
        token_id=token_id, side="SELL", limit_price=limit_price,
        size_shares=sell_shares, notional_usd=round(sell_shares * expected_price, 6),
        tick_size=tick_size, min_order_size=min_order_size,
        requested_price=price, expected_price=expected_price,
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
    _wait_for_balance(client)
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


def _wait_for_balance(client) -> bool:
    """
    Refresh the CLOB's balance/allowance cache and WAIT for the refresh to
    become visible before posting. Returns whether a nonzero balance was
    seen; posting proceeds either way.

    A 200 FROM update_balance_allowance() IS NOT A GUARANTEE. It only
    triggers a re-check on Polymarket's side, and the result does not
    necessarily reach what post_order() reads by the time it returns.
    Production logs from another bot on this machine show the exact
    sequence: update_balance_allowance() 200 OK, then post_order() rejected
    with "balance: 0" about 250ms later, then a separate
    get_balance_allowance() ~10s on reading the real, nonzero, on-chain-
    correct balance. The cache also goes stale MID-PROCESS, not only across
    restarts, so a sync done once at startup does not cover a daemon that
    has been up for hours.

    Polling costs at most BALANCE_POLL_ATTEMPTS x BALANCE_POLL_DELAY_SEC of
    latency against a FOK that is already price-protected. Losing an entry
    that cleared every gate to a stale cache costs the trade.

    Never raises: a balance check that itself fails must not block an order
    the operator asked for. It logs and lets post_order() be the authority,
    which is what it was before this function existed.
    """
    import config

    lib = _clob()
    try:
        params = lib.BalanceAllowanceParams(asset_type=lib.AssetType.COLLATERAL)
    except Exception as exc:  # noqa: BLE001 -- older/newer SDK shapes
        logger.warning(f"[wallet_client] could not build a balance query, skipping the check: {exc}")
        return False

    try:
        client.update_balance_allowance(params)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[wallet_client] balance/allowance sync failed: {exc}")

    for attempt in range(1, config.BALANCE_POLL_ATTEMPTS + 1):
        try:
            check = client.get_balance_allowance(params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wallet_client] balance read failed ({attempt}): {exc}")
            return False

        raw = check.get("balance") if isinstance(check, dict) else getattr(check, "balance", None)
        try:
            if raw is not None and float(raw) > 0:
                return True
        except (TypeError, ValueError):
            logger.warning(f"[wallet_client] unparseable balance {raw!r} -- letting post_order decide")
            return False

        logger.warning(
            f"[wallet_client] balance still reads 0 after sync "
            f"(propagation check {attempt}/{config.BALANCE_POLL_ATTEMPTS})"
        )
        if attempt < config.BALANCE_POLL_ATTEMPTS:
            time.sleep(config.BALANCE_POLL_DELAY_SEC)

    logger.error(
        "[wallet_client] balance still reads 0 after every propagation check -- "
        "posting anyway and letting the exchange be the authority. A rejection "
        "here means the funding wallet is genuinely empty or its allowances are unset."
    )
    return False


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

_collateral_cache = {"at": 0.0, "result": None}

# Balance is reported in the collateral token's base units. The library
# exposes COLLATERAL_TOKEN_DECIMALS = 6; hardcoding 1e6 here would silently
# misreport by 10^n if that ever changed, so read it when we can.
_COLLATERAL_UNITS_FALLBACK = 6


def _collateral_decimals(lib) -> int:
    try:
        from py_clob_client_v2 import config as _libcfg  # noqa: PLC0415
        return int(getattr(_libcfg, "COLLATERAL_TOKEN_DECIMALS",
                           _COLLATERAL_UNITS_FALLBACK))
    except Exception:  # noqa: BLE001
        return _COLLATERAL_UNITS_FALLBACK


def collateral_status(client=None, use_cache: bool = True) -> tuple:
    """
    (mark, detail) for "is this account funded and approved to trade?", where
    mark is 'ok' / '--' / '!!' exactly as preflight() renders it.

    THIS REPLACES A LINE THAT WAS WRONG. preflight() used to end with a
    permanent "[--] allowances ... NOT detectable from the public book --
    confirm manually", which was true of the public book and false of this
    endpoint: get_balance_allowance() reports the funding address's collateral
    balance AND its allowance per spender, and under the correct signature
    type those come back real (see the signature-type evidence at the top of
    this module). Because that line never changed state, it read as a
    permanent blocker on every simulation run -- the operator learns to
    ignore the one item that would matter if it were ever true, which is the
    failure mode a checklist exists to prevent.

    FAILS TOWARDS '--', NEVER TOWARDS 'ok'. Every kind of absence -- no
    credentials, no library, an endpoint that errors, a response shaped
    differently than expected -- reports "could not verify", because "I did
    not look" and "I looked and it was fine" must never render the same.

    Reads only. Setting an allowance is an on-chain transaction and belongs
    to the operator, not to a trading loop.
    """
    import time as _time

    import config

    now = _time.time()
    if use_cache and client is None:
        cached = _collateral_cache["result"]
        if cached is not None and now - _collateral_cache["at"] < config.COLLATERAL_STATUS_TTL_S:
            return cached

    def _finish(result):
        if use_cache and client is None:
            _collateral_cache.update({"at": now, "result": result})
        return result

    # _clob() RAISES on a missing library rather than returning None -- that is
    # the right behaviour for the order path and the wrong one for a checklist
    # line, so it is caught here.
    try:
        lib = _clob()
    except RuntimeError as exc:
        return _finish(("--", f"collateral/allowance unverified -- {str(exc)[:90]}"))

    if client is None:
        if not (os.environ.get("POLYMARKET_PRIVATE_KEY")
                and os.environ.get("POLYMARKET_FUNDER")):
            return _finish(("--", "collateral/allowance unverified -- no credentials "
                                  "on this box (simulation does not need them; a live "
                                  "order does)"))
        try:
            client = get_client()
        except Exception as exc:  # noqa: BLE001
            return _finish(("--", f"collateral/allowance unverified -- could not "
                                  f"authenticate: {type(exc).__name__}"))

    try:
        from py_clob_client_v2 import clob_types  # noqa: PLC0415
        params = clob_types.BalanceAllowanceParams(
            asset_type=clob_types.AssetType.COLLATERAL
        )
        resp = client.get_balance_allowance(params=params)
    except Exception as exc:  # noqa: BLE001
        return _finish(("--", f"collateral/allowance unverified -- endpoint failed: "
                              f"{type(exc).__name__}: {str(exc)[:120]}"))

    return _finish(_interpret_collateral(resp, decimals=_collateral_decimals(lib)))


def _interpret_collateral(resp, decimals: int) -> tuple:
    """
    Turn one get_balance_allowance() response into (mark, detail).

    SEPARATE FROM THE FETCH so it can be tested without the library, a
    private key, or a network -- the same reason _interpret_fill() is its own
    function. Every branch that cannot establish approval returns '--' or
    '!!'; none of them can return 'ok'.
    """
    import config

    if not isinstance(resp, dict):
        return ("--", f"collateral/allowance unverified -- unexpected response "
                      f"type {type(resp).__name__}")

    scale = 10 ** decimals
    try:
        balance = int(resp.get("balance"))  # base units, as a string
    except (TypeError, ValueError):
        return ("--", "collateral/allowance unverified -- response carried no "
                      "readable balance")

    # Two shapes in the wild: a per-spender dict, or a single scalar. Treat a
    # scalar as one unnamed spender rather than guessing it means "all".
    raw = resp.get("allowances", resp.get("allowance"))
    if isinstance(raw, dict):
        spenders = raw
    elif raw is None:
        spenders = {}
    else:
        spenders = {"(unnamed spender)": raw}

    usd = balance / scale
    if not spenders:
        return ("--", f"balance ${usd:,.2f} but the response listed no spender "
                      f"allowances -- cannot confirm approval")

    unset = []
    for spender, value in spenders.items():
        try:
            if int(value) <= 0:
                unset.append(spender)
        except (TypeError, ValueError):
            # Unparseable is not approved. An allowance we cannot read is
            # one we cannot vouch for.
            unset.append(spender)

    approved = len(spenders) - len(unset)
    if unset:
        return ("!!", f"{len(unset)} of {len(spenders)} spender allowance(s) NOT SET "
                      f"({', '.join(str(s)[:10] + '...' for s in unset)}) -- a live "
                      f"order will be rejected until they are approved on-chain; "
                      f"balance ${usd:,.2f}")

    # Approved, but the wallet still has to cover an order. The exchange
    # minimum is 5 SHARES, so the cheapest possible order is 5 x the tick --
    # tiny. What actually binds is the configured trade size.
    want = getattr(config, "LIVE_TRADE_SIZE_USD", 0.0)
    if usd < want:
        return ("!!", f"allowances set on all {approved} spender(s), but balance "
                      f"${usd:,.2f} is below LIVE_TRADE_SIZE_USD ${want:,.2f} -- the "
                      f"wallet, not the risk cap, is the binding constraint")

    return ("ok", f"collateral ${usd:,.2f}, allowances set on all {approved} "
                  f"spender(s) the exchange named")


def preflight(token_id: Optional[str] = None) -> list:
    """
    Checks to run BEFORE the first live order, returned as a list of
    human-readable "[ok]/[--]/[!!]" lines rather than raising. Called by
    executor on every simulation-mode entry so the report is produced
    continuously, not once at setup time and then assumed to still hold.

    The collateral/allowance line is a real query now (collateral_status()),
    not the permanent "[--] confirm manually" placeholder it used to be. A
    checklist item that never changes state trains the reader to skip it.

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

    mark, detail = collateral_status()
    lines.append(f"[{mark}] {detail}")
    if mark != "ok":
        lines.append(
            "[--] allowances are an on-chain transaction and are NOT set from "
            "here -- approve the funding address once, via the Polymarket UI or "
            "directly against the spender contracts."
        )
    return lines


def check_allowances_reminder() -> str:
    """
    Retained for callers of the previous API. preflight() is the fuller
    replacement.

    This used to return a fixed string asserting allowances could not be
    checked. It now reports what the exchange actually says, so a caller
    holding the old API gets a real answer rather than a stale warning.
    """
    mark, detail = collateral_status()
    if mark == "ok":
        return f"Allowances verified against the exchange: {detail}."
    return (
        f"Allowance check did not pass: {detail}. Allowances must be set "
        f"on-chain once per funding address; this module never sets them."
    )


# --------------------------------------------------------------------------
# Reconciliation against the exchange
# --------------------------------------------------------------------------

@dataclass
class Reconciliation:
    """
    What the exchange says, next to what the database says.

    `ok` is the only field callers should gate on, and it is False whenever
    the check could not run -- "I could not look" and "I looked and it was
    wrong" are the same answer for the purpose of authorising real money.
    """
    ok: bool
    checked: bool                      # did the comparison actually happen
    reason: str = ""
    verified: list = None              # (token_id, shares) agreeing on both sides
    db_only: list = None               # DB says open, exchange shows no/too few shares
    exchange_only: list = None         # exchange holds shares, DB has no open row

    def __post_init__(self):
        self.verified = self.verified or []
        self.db_only = self.db_only or []
        self.exchange_only = self.exchange_only or []

    def describe(self) -> str:
        if not self.checked:
            return f"NOT CHECKED ({self.reason})"
        if self.ok:
            return f"clean -- {len(self.verified)} position(s) agree with the exchange"
        bits = []
        if self.db_only:
            bits.append(
                f"{len(self.db_only)} recorded but not held: "
                + ", ".join(f"{t[:10]}... (db {d:.2f} sh, exchange {e:.2f})"
                            for t, d, e in self.db_only)
            )
        if self.exchange_only:
            bits.append(
                f"{len(self.exchange_only)} held but NOT RECORDED: "
                + ", ".join(f"{t[:10]}... ({s:.2f} sh)" for t, s in self.exchange_only)
            )
        return "; ".join(bits) or self.reason


_reconcile_cache = {"at": 0.0, "result": None}


def _held_shares(client, token_id: str) -> Optional[float]:
    """
    Outcome-token balance for one token, or None if it could not be read.
    None is NOT zero -- a failed read must never look like "holds nothing".
    """
    lib = _clob()
    try:
        resp = client.get_balance_allowance(
            lib.BalanceAllowanceParams(asset_type=lib.AssetType.CONDITIONAL, token_id=token_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[wallet_client] balance read failed for {token_id[:12]}...: {exc}")
        return None

    raw = resp.get("balance") if isinstance(resp, dict) else getattr(resp, "balance", None)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(f"[wallet_client] unparseable balance {raw!r} for {token_id[:12]}...")
        return None


def reconcile_live_positions(open_live_positions) -> Reconciliation:
    """
    Compare the database's open LIVE positions against what the funding
    wallet actually holds, and hunt for holdings the database has never
    heard of.

    WHY THIS EXISTS. Every live cap in executor is computed from the local
    positions table, which makes them caps on the database's recollection of
    exposure rather than on exposure itself. On 2026-08-10 a real position
    sat on the exchange while the daemon had no row for it; during that
    window all three caps were one position light. Reading DB_PATH from a
    different checkout produces the same blindness with none of the drama.

    Two directions, both of which matter:

      db_only        the DB says shares are held and the exchange disagrees.
                     Usually a close that happened outside this system; the
                     danger is the exit path trying to sell what is gone.
      exchange_only  shares are held that the DB has no open row for. THIS
                     IS THE ONE THAT BREAKS THE CAPS -- unrecorded exposure
                     is exposure the backstops cannot see.

    Discovery of exchange_only goes through get_trades() rather than any
    "list my positions" call, because the CLOB has none: fills are
    enumerable, holdings are only queryable one token at a time. A token
    that traded in the window and still shows a balance, with no open DB
    row, is a real unrecorded position. FOK entries mean get_open_orders()
    is always empty and would find nothing at all.

    FAILS CLOSED. Missing credentials, an unreadable balance, or an
    unreachable trade history all return ok=False. Callers must treat that
    as "do not open anything new", never as "nothing found".
    """
    import config

    if not (os.environ.get("POLYMARKET_PRIVATE_KEY") and os.environ.get("POLYMARKET_FUNDER")):
        return Reconciliation(
            ok=False, checked=False,
            reason="no credentials, so the exchange cannot be consulted",
        )

    try:
        lib = _clob()
        client = get_client()
    except Exception as exc:  # noqa: BLE001
        return Reconciliation(ok=False, checked=False, reason=f"client unavailable: {exc}")

    verified, db_only = [], []
    recorded_tokens = set()

    for pos in open_live_positions:
        token = getattr(pos, "token_id", None)
        if not token:
            return Reconciliation(
                ok=False, checked=True,
                reason=(f"open live position {pos.position_id} has no token_id -- "
                        f"it cannot be checked against the exchange at all"),
            )
        recorded_tokens.add(token)
        expected = getattr(pos, "size_shares", None) or 0.0
        held = _held_shares(client, token)
        if held is None:
            return Reconciliation(
                ok=False, checked=True,
                reason=(f"could not read the exchange balance for {token[:12]}... -- "
                        f"refusing to authorise against an unverified book"),
            )
        if held + config.RECONCILE_SHARE_TOLERANCE < expected:
            db_only.append((token, expected, held))
        else:
            verified.append((token, held))

    # Holdings the database has never recorded.
    exchange_only = []
    try:
        cutoff = int(time.time()) - config.RECONCILE_TRADE_LOOKBACK_HOURS * 3600
        floor_iso = getattr(config, "RECONCILE_IGNORE_TRADES_BEFORE", None)
        if floor_iso:
            import datetime as _dt

            floor_ts = int(_dt.datetime.fromisoformat(floor_iso)
                           .replace(tzinfo=_dt.timezone.utc).timestamp())
            if floor_ts > cutoff:
                logger.info(
                    f"[wallet_client] reconciliation scan floored at {floor_iso} "
                    f"(RECONCILE_IGNORE_TRADES_BEFORE), narrowing the "
                    f"{config.RECONCILE_TRADE_LOOKBACK_HOURS}h lookback. Holdings bought "
                    f"before that date are NOT visible to the unrecorded-position check."
                )
                cutoff = floor_ts
        trades = client.get_trades(lib.TradeParams(after=str(cutoff)))
    except Exception as exc:  # noqa: BLE001
        return Reconciliation(
            ok=False, checked=True,
            reason=(f"could not read trade history ({exc}) -- unrecorded positions "
                    f"cannot be ruled out"),
        )

    seen = set()
    for trade in trades or []:
        asset = (trade.get("asset_id") if isinstance(trade, dict)
                 else getattr(trade, "asset_id", None))
        if not asset or asset in recorded_tokens or asset in seen:
            continue
        seen.add(asset)
        held = _held_shares(client, asset)
        if held is None:
            return Reconciliation(
                ok=False, checked=True,
                reason=(f"traded token {asset[:12]}... has an unreadable balance -- "
                        f"cannot rule out an unrecorded position"),
            )
        if held > config.RECONCILE_SHARE_TOLERANCE:
            exchange_only.append((asset, held))

    diverged = bool(db_only or exchange_only)
    return Reconciliation(
        ok=not diverged,
        checked=True,
        verified=verified, db_only=db_only, exchange_only=exchange_only,
        reason="exchange and database agree" if not diverged else "divergence",
    )


def reconcile_cached(open_live_positions) -> Reconciliation:
    """
    reconcile_live_positions() with a short TTL. _live_budget_breach() runs
    once per candidate entry and several candidates can clear the screen in
    one cycle; without this each would re-scan every fill in the lookback
    window.
    """
    import config

    now = time.time()
    cached = _reconcile_cache["result"]
    if cached is not None and now - _reconcile_cache["at"] < config.RECONCILE_CACHE_TTL_S:
        return cached
    result = reconcile_live_positions(open_live_positions)
    _reconcile_cache.update({"at": now, "result": result})
    return result
