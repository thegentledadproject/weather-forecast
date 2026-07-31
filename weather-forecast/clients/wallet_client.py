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

VERIFIED, NOT GUESSED: this module was written after actually
installing py-clob-client-v2 and inspecting its real classes/method
signatures in this environment -- ClobClient's constructor, OrderArgs'
fields, and SignatureTypeV2's values below are all confirmed against
the real installed package, not just documentation snippets.

LIBRARY CHOICE -- WHY v2, NOT THE NEWER py-sdk
-------------------------------------------------
Polymarket's own docs point toward Polymarket/py-sdk ("polymarket-client"
on PyPI) as the long-term unified SDK. But as of this build, current
third-party tutorials (dated within the last ~2 months) still use
py-clob-client-v2 for actual order placement, and py-sdk's authenticated
trading interface was not confirmed in available documentation search
results (only its public/read-only client was). Building against the
library with a confirmed, working order-placement path, and revisiting
once py-sdk's trading interface is documented and stable.

CRITICAL, CURRENT SAFETY NOTE -- SIGNATURE TYPE 3 IS BROKEN
--------------------------------------------------------------
A real, open GitHub issue (Polymarket/py-clob-client-v2 #70, filed
2026-05-19) confirms: signature_type=3 (POLY_1271, "deposit wallet")
order placement fails for new accounts in both the Python and Rust v2
SDKs -- L1 auth incorrectly binds the API key to the EOA instead of
the deposit wallet. The documented working path for a NEW account
right now is signature_type=1 (POLY_PROXY) via a Magic-wallet
email-based account, not signature_type=3. DEFAULT_SIGNATURE_TYPE
below reflects this -- do not change it to 3 without first checking
whether that upstream issue has been resolved.

SAFETY DEFAULTS
----------------
Every function defaults to DRY_RUN behavior: it constructs and logs
what WOULD be submitted, but does not call post_order(), unless
POLYMARKET_LIVE_TRADING=true is explicitly set in the environment.
This is intentionally a second gate below executor.py's manual_review
mode -- two separate switches have to agree before real money moves.

DEPENDENCIES
------------
py-clob-client-v2   (pip install py-clob-client-v2)
os, logging (standard library)
"""

import os
import logging
from typing import Optional

from py_clob_client_v2 import (
    ClobClient, ApiCreds, OrderArgs, OrderType, Side, SignatureTypeV2,
)

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# See CRITICAL, CURRENT SAFETY NOTE above -- do not change to 3 (POLY_1271)
# without confirming Polymarket/py-clob-client-v2#70 has been fixed.
DEFAULT_SIGNATURE_TYPE = SignatureTypeV2(1)  # POLY_PROXY, Magic-wallet email account

_client: Optional[ClobClient] = None


def _live_trading_enabled() -> bool:
    """
    Second, independent safety gate below executor.py's manual_review
    mode. Both must agree before any real order is placed -- a station
    being set to 'auto' in executor.EXECUTION_MODE is not sufficient
    on its own.
    """
    return os.environ.get("POLYMARKET_LIVE_TRADING", "").lower() == "true"


def get_client() -> ClobClient:
    """
    Lazily construct and cache the authenticated ClobClient, reading
    credentials from environment variables -- never hardcoded, never
    passed as function arguments (to avoid them ending up in logs).

    Required env vars:
      POLYMARKET_PRIVATE_KEY   -- EOA private key that controls the account
      POLYMARKET_FUNDER        -- address that actually holds the funds
                                   (the proxy/Magic wallet, per signature_type=1)

    Raises a clear error if credentials are missing, rather than
    silently constructing a client that will fail confusingly later.
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

    client = ClobClient(
        CLOB_HOST,
        chain_id=POLYGON_CHAIN_ID,
        key=private_key,
        signature_type=int(DEFAULT_SIGNATURE_TYPE),
        funder=funder,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    _client = client
    return _client


def place_limit_buy(token_id: str, price: float, size_usd: float) -> dict:
    """
    Place a GTC (good-till-cancelled) limit buy order -- the correct
    order type per the earlier order-execution mechanism analysis:
    entry_manager.py already validated the trade against a specific
    price and slippage tolerance, so a resting limit order at that
    price protects against a worse fill invalidating the EV
    calculation that approved the trade. A market/FOK order is
    deliberately NOT the default here.

    size_usd is converted to a share count via price -- e.g. $50 at
    price 0.32 buys 156.25 shares. Rounding/precision against
    Polymarket's actual tick size is NOT yet handled here (see
    CreateOrderOptions/tick_size in the installed library) -- treat
    this as a gap to close before real use, not an oversight to ignore.
    """
    size_shares = round(size_usd / price, 2)
    order_args = OrderArgs(token_id=token_id, price=price, size=size_shares, side=Side.BUY)

    if not _live_trading_enabled():
        logger.info(
            f"[wallet_client] DRY RUN (POLYMARKET_LIVE_TRADING not set) -- "
            f"would place GTC BUY: token={token_id} price={price} size={size_shares} shares (${size_usd})"
        )
        return {"dry_run": True, "token_id": token_id, "price": price, "size": size_shares}

    client = get_client()
    signed = client.create_order(order_args)
    resp = client.post_order(signed, OrderType.GTC)
    logger.info(f"[wallet_client] LIVE order placed: {resp}")
    return resp


def place_market_sell(token_id: str, size_shares: float) -> dict:
    """
    Place a FOK (fill-or-kill) market sell -- appropriate for exits
    (risk_manager.py's stop-loss/trailing-stop/take-profit signals),
    where speed of exit matters more than price precision, unlike entries.
    """
    from py_clob_client_v2 import MarketOrderArgs

    order_args = MarketOrderArgs(token_id=token_id, amount=size_shares, side=Side.SELL)

    if not _live_trading_enabled():
        logger.info(
            f"[wallet_client] DRY RUN (POLYMARKET_LIVE_TRADING not set) -- "
            f"would place FOK SELL: token={token_id} size={size_shares} shares"
        )
        return {"dry_run": True, "token_id": token_id, "size": size_shares}

    client = get_client()
    signed = client.create_market_order(order_args)
    resp = client.post_order(signed, OrderType.FOK)
    logger.info(f"[wallet_client] LIVE order placed: {resp}")
    return resp


def check_allowances_reminder() -> str:
    """
    Token allowances (USDC + conditional tokens, spender = the CLOB
    Exchange contract) must be set on-chain before ANY order can be
    placed -- this is a one-time, real on-chain transaction, not
    something this module performs automatically. Returns a reminder
    string rather than silently assuming allowances are already set.
    """
    return (
        "Token allowances must be set on-chain before trading, once per "
        "funding address. This is NOT automated by this module -- confirm "
        "allowances are set (see Polymarket's py-clob-client-v2 documentation) "
        "before calling place_limit_buy/place_market_sell with live trading enabled."
    )
