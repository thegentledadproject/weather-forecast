"""
clients/redemption_client.py

Discovery and eligibility for redemption: what CAN be redeemed, from three
sources that must agree -- the database, the Polymarket data-api, and the
chain. No transaction building or signing lives here (onchain_client.py);
no CLI or operator interaction lives here (redeem.py).

WHY THE CHAIN BALANCE IS THE AUTHORITY, empirically, not by assumption.
Checked against the real funder wallet on 2026-09-03: of 10
database-tracked "settled but possibly unredeemed" tokens, 5 had a ZERO
on-chain balance -- including WSSS 2026-08-11 33YES, which the operator's
own record confirms was redeemed by hand that same day. Every one of
those 5 was ALSO absent from the data-api's positions listing; every
token with a NONZERO chain balance WAS present in it. So the data-api
appears to list only currently-held positions, and a database row
reading closed_resolution / exit_price 1.0 forever is NOT proof anything
is still owed -- it is a record of what happened at close, and
redemption can happen outside this system entirely (by hand, as it did
here). The chain balance is what decides whether a token needs action at
all; the data-api supplies the conditionId needed to act on the ones
that do.

THE THREE OUTCOME BUCKETS, and why a token is never silently dropped:
  redeemable       held on-chain, conditionId known, ready to redeem.
  already_cleared  zero on-chain balance -- already collected, somehow,
                    by something outside this module. No action needed.
  unresolvable     held on-chain but conditionId is NOT discoverable (data-api
                    down, or the API simply does not list it, or it reports
                    a non-neg-risk market for what should be one) -- real
                    value, cannot currently be redeemed via this path.
                    Reported, never dropped: a held token with no route to
                    its conditionId is still a held token.
"""
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import requests

from clients import onchain_client
import storage

DATA_API_BASE = "https://data-api.polymarket.com"


@dataclass
class RedeemableItem:
    """One resolved position confirmed still held on-chain, with a known
    conditionId -- everything encode_redeem_positions() needs."""
    token_id: str
    condition_id: bytes
    station_icao: str
    target_date: date
    bucket_c: int
    side: str
    size_shares: float          # from the CHAIN, not the database -- see the
                                 # module docstring on why the two can differ.
    is_winner: bool
    value_usd: float


@dataclass
class ClearedToken:
    """A database-tracked settled token with a zero on-chain balance.
    Already redeemed or otherwise cleared; no action needed."""
    token_id: str
    station_icao: str
    target_date: date
    bucket_c: int
    side: str


@dataclass
class UnresolvableToken:
    """Held on-chain, but this module could not determine a conditionId
    (or found an anomaly) it can act on. Real value, reported, not dropped."""
    token_id: str
    station_icao: str
    target_date: date
    bucket_c: int
    side: str
    size_shares: float
    reason: str


@dataclass
class RedemptionScan:
    redeemable: List[RedeemableItem]
    already_cleared: List[ClearedToken]
    unresolvable: List[UnresolvableToken]
    data_api_reachable: bool
    data_api_error: Optional[str] = None


def _fetch_data_api_positions(funder: str) -> List[Dict[str, Any]]:
    """
    GET data-api.polymarket.com/positions?user=<funder>. Raises on any
    failure -- find_redeemable() decides what "unreachable" means for the
    scan, this function just tries once and reports honestly.
    """
    resp = requests.get(
        f"{DATA_API_BASE}/positions",
        params={"user": funder, "sizeThreshold": 0, "limit": 500},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def find_redeemable(
    funder: str,
    conditional_tokens_address: str,
    rpc_url: str = onchain_client.DEFAULT_RPC_URL,
) -> RedemptionScan:
    """
    Sort every database-tracked settled token into redeemable /
    already_cleared / unresolvable, cross-checking the chain and the
    data-api. See the module docstring for why the chain balance decides
    everything and the data-api is discovery convenience, not authority.
    """
    db_tokens = storage.load_settled_live_tokens()

    if not db_tokens:
        return RedemptionScan(redeemable=[], already_cleared=[], unresolvable=[],
                               data_api_reachable=True)

    data_api_reachable = True
    data_api_error = None
    api_by_token: Dict[str, Dict[str, Any]] = {}
    try:
        positions = _fetch_data_api_positions(funder)
        api_by_token = {row["asset"]: row for row in positions}
    except Exception as exc:  # noqa: BLE001 -- any failure degrades to reduced confidence
        data_api_reachable = False
        data_api_error = str(exc)

    redeemable: List[RedeemableItem] = []
    already_cleared: List[ClearedToken] = []
    unresolvable: List[UnresolvableToken] = []

    for token_id, settled in db_tokens.items():
        held_base_units = onchain_client.get_erc1155_balance(
            contract_address=conditional_tokens_address, owner=funder,
            token_id=int(token_id), rpc_url=rpc_url,
        )

        if held_base_units == 0:
            already_cleared.append(ClearedToken(
                token_id=token_id, station_icao=settled.station_icao,
                target_date=settled.target_date, bucket_c=settled.bucket_c,
                side=settled.side,
            ))
            continue

        # CLOB/data-api balances are base units (1e6 = 1 share) -- see
        # check_holdings.py's own note on this exact conversion trap.
        held_shares = held_base_units / 1_000_000

        api_row = api_by_token.get(token_id)
        if api_row is None:
            reason = (
                "data-api unreachable" if not data_api_reachable else
                "held on-chain but absent from the data-api's positions listing "
                "-- no conditionId available to build a redemption from"
            )
            unresolvable.append(UnresolvableToken(
                token_id=token_id, station_icao=settled.station_icao,
                target_date=settled.target_date, bucket_c=settled.bucket_c,
                side=settled.side, size_shares=held_shares, reason=reason,
            ))
            continue

        if not api_row.get("negativeRisk", False):
            unresolvable.append(UnresolvableToken(
                token_id=token_id, station_icao=settled.station_icao,
                target_date=settled.target_date, bucket_c=settled.bucket_c,
                side=settled.side, size_shares=held_shares,
                reason=(
                    f"data-api reports negativeRisk=False for a weather-market "
                    f"token -- every registered market is neg-risk, so this is "
                    f"an anomaly worth refusing rather than routing to "
                    f"NegRiskAdapter on a guess"
                ),
            ))
            continue

        condition_id_hex = api_row["conditionId"]
        condition_id = bytes.fromhex(
            condition_id_hex[2:] if condition_id_hex.startswith("0x") else condition_id_hex
        )
        # is_winner from the DATABASE's exit_price, not the API's curPrice:
        # the database is this system's own settlement record, made at close
        # time from the settlement-grade reading; curPrice is Polymarket's
        # own live-market price feed and is not guaranteed to still reflect
        # a market that resolved some time ago (see execute()'s own
        # docstring on why the data-api is "not a contract").
        is_winner = (settled.exit_price or 0.0) > 0
        redeemable.append(RedeemableItem(
            token_id=token_id, condition_id=condition_id,
            station_icao=settled.station_icao, target_date=settled.target_date,
            bucket_c=settled.bucket_c, side=settled.side,
            size_shares=held_shares, is_winner=is_winner,
            value_usd=(held_shares if is_winner else 0.0),
        ))

    return RedemptionScan(
        redeemable=redeemable, already_cleared=already_cleared,
        unresolvable=unresolvable, data_api_reachable=data_api_reachable,
        data_api_error=data_api_error,
    )
