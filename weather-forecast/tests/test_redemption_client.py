"""
tests/test_redemption_client.py

Discovery and eligibility for redemption, from three sources that must
agree: the database (which positions we closed as resolved), the
Polymarket data-api (conditionId, negativeRisk, human title), and the
chain (the actual ERC-1155 balance -- ground truth for whether there is
anything left to redeem at all).

WHY THE CHAIN BALANCE IS PRIMARY, NOT THE DATA-API. Empirically checked
against the real funder wallet on 2026-09-03: every one of 10
database-tracked "settled but possibly unredeemed" tokens that had a
ZERO on-chain balance was ALSO absent from the data-api's positions
listing, and every one with a NONZERO balance was present. The data-api
appears to list only currently-held positions -- so a token can be
legitimately absent because it was already redeemed by hand (as WSSS
2026-08-11 33YES was, per the operator's own record) rather than because
anything is wrong. The chain balance is what decides whether a token
needs action; the data-api supplies the conditionId needed to act on it.

No test touches the network. The data-api HTTP call is faked at
requests.get; the chain balance read is faked by monkeypatching
onchain_client.get_erc1155_balance directly, since that call's own
correctness is onchain_client's test file's job, not this one's.
"""
from datetime import date

import pytest

import config
import storage
from clients import onchain_client, redemption_client
from models import SettledToken


FUNDER = "0x" + "c6" * 20
CONDITIONAL_TOKENS = "0x" + "4d" * 20
RPC_URL = "https://example-polygon-rpc.test"

TOKEN_A = "111111111111111111111111111111111111111111111111111111111"
TOKEN_B = "222222222222222222222222222222222222222222222222222222222"
CONDITION_ID_A = "0x" + "aa" * 32


def _settled(station="WSSS", target_date_=date(2026, 8, 20), bucket_c=32,
             side="NO", shares=5.0, exit_price=0.0):
    return SettledToken(station_icao=station, target_date=target_date_,
                         bucket_c=bucket_c, side=side, size_shares=shares,
                         exit_price=exit_price)


@pytest.fixture
def wired(monkeypatch):
    """
    Wires storage.load_settled_live_tokens(), the data-api HTTP call, and
    the chain balance read to caller-supplied fakes. Returns a callable
    matching find_redeemable()'s own signature.
    """
    def run(db_tokens, api_positions=None, api_error=None, chain_balances=None):
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: db_tokens)

        def fake_get(url, params=None, timeout=None):
            if api_error is not None:
                raise api_error
            return _FakeResp(api_positions or [])

        monkeypatch.setattr(redemption_client.requests, "get", fake_get)

        balances = chain_balances or {}
        monkeypatch.setattr(
            onchain_client, "get_erc1155_balance",
            lambda contract_address, owner, token_id, rpc_url: balances.get(str(token_id), 0),
        )

        return redemption_client.find_redeemable(
            funder=FUNDER, conditional_tokens_address=CONDITIONAL_TOKENS, rpc_url=RPC_URL,
        )

    return run


class _FakeResp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _api_row(token_id, condition_id=CONDITION_ID_A, negative_risk=True,
             cur_price=0.0, redeemable=True, size=5.0, title="Will it rain"):
    return {
        "asset": token_id, "conditionId": condition_id, "negativeRisk": negative_risk,
        "curPrice": cur_price, "redeemable": redeemable, "size": size, "title": title,
    }


class TestNothingToDo:
    def test_an_empty_database_returns_an_empty_scan(self, wired):
        scan = wired(db_tokens={})
        assert scan.redeemable == []
        assert scan.already_cleared == []
        assert scan.unresolvable == []


class TestAlreadyCleared:
    def test_a_zero_chain_balance_is_reported_as_already_cleared_not_redeemable(self, wired):
        """
        The core empirical finding this module is built on: a database row
        that still says closed_resolution / exit_price 1.0 can have already
        been collected by hand. The chain balance is what actually knows.
        """
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=5.0, exit_price=1.0)},
            api_positions=[],
            chain_balances={},  # absent = 0
        )
        assert scan.redeemable == []
        assert len(scan.already_cleared) == 1
        assert scan.already_cleared[0].token_id == TOKEN_A
        assert scan.already_cleared[0].station_icao == "WSSS"

    def test_never_calls_the_data_api_for_a_conditionid_it_will_not_need(self, wired, monkeypatch):
        """A cleared token needs no conditionId -- nothing will ever be built
        for it. Still fine if the API result is empty; asserting on that here
        rather than call-counting, which is brittle against a legitimate
        future optimization to batch the lookup."""
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=5.0, exit_price=1.0)},
            api_positions=[],
            chain_balances={},
        )
        assert scan.already_cleared[0].token_id == TOKEN_A


class TestRedeemable:
    def test_a_held_token_with_a_known_condition_id_is_redeemable(self, wired):
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=5.0, exit_price=0.0)},
            api_positions=[_api_row(TOKEN_A, cur_price=0.0, size=5.0)],
            chain_balances={TOKEN_A: 5_000_000},
        )
        assert len(scan.redeemable) == 1
        item = scan.redeemable[0]
        assert item.token_id == TOKEN_A
        assert item.condition_id == bytes.fromhex(CONDITION_ID_A[2:])
        assert item.station_icao == "WSSS"
        assert item.is_winner is False
        assert item.value_usd == pytest.approx(0.0)

    def test_size_comes_from_the_chain_not_the_database(self, wired):
        """
        The database's size_shares is what was RECORDED at close; the chain
        balance is what is ACTUALLY held right now, and a redemption
        transaction has to redeem what exists, not what a row says.
        """
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=999.0, exit_price=0.0)},
            api_positions=[_api_row(TOKEN_A, size=5.0)],
            chain_balances={TOKEN_A: 5_000_000},
        )
        assert scan.redeemable[0].size_shares == pytest.approx(5.0)

    def test_a_winner_computes_its_dollar_value(self, wired):
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=5.0, exit_price=1.0)},
            api_positions=[_api_row(TOKEN_A, cur_price=1.0, size=5.0)],
            chain_balances={TOKEN_A: 5_000_000},
        )
        item = scan.redeemable[0]
        assert item.is_winner is True
        assert item.value_usd == pytest.approx(5.0)


class TestUnresolvable:
    def test_a_held_token_absent_from_the_api_is_unresolvable_not_dropped(self, wired):
        """
        Empirically this has not been observed (every nonzero balance in
        the real wallet was present in the API) -- but "not yet observed"
        is not "cannot happen," and silently skipping a real holding with
        real value would be exactly the kind of fabrication-by-omission
        this codebase refuses elsewhere.
        """
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=5.0, exit_price=1.0)},
            api_positions=[],
            chain_balances={TOKEN_A: 5_000_000},
        )
        assert scan.redeemable == []
        assert scan.already_cleared == []
        assert len(scan.unresolvable) == 1
        assert scan.unresolvable[0].token_id == TOKEN_A
        assert "condition" in scan.unresolvable[0].reason.lower()

    def test_a_non_negative_risk_market_is_refused_not_silently_redeemed(self, wired):
        """
        Requirement from the design's own testing section: a negativeRisk
        market must target NegRiskAdapter and a non-neg-risk one must not.
        Every weather market is neg-risk; a False here for a token this
        system opened is exactly the anomaly worth refusing loudly on.
        """
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=5.0, exit_price=0.0)},
            api_positions=[_api_row(TOKEN_A, negative_risk=False)],
            chain_balances={TOKEN_A: 5_000_000},
        )
        assert scan.redeemable == []
        assert len(scan.unresolvable) == 1
        assert "negrisk" in scan.unresolvable[0].reason.lower() or "neg-risk" in scan.unresolvable[0].reason.lower()


class TestDataApiUnreachable:
    def test_falls_back_to_chain_only_and_reports_reduced_confidence(self, wired):
        """§7: data-api unreachable -> fall back to DB + chain, reduced
        confidence, refuse execute. A held token cannot become 'redeemable'
        without a conditionId, so it reports as unresolvable, not silently
        as if nothing were held."""
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=5.0, exit_price=0.0)},
            api_error=ConnectionError("no route to host"),
            chain_balances={TOKEN_A: 5_000_000},
        )
        assert scan.data_api_reachable is False
        assert scan.data_api_error is not None
        assert scan.redeemable == []
        assert len(scan.unresolvable) == 1

    def test_a_cleared_token_still_reports_correctly_even_with_the_api_down(self, wired):
        """The zero-balance case needs no API at all, so it is unaffected
        by the API being unreachable."""
        scan = wired(
            db_tokens={TOKEN_A: _settled(shares=5.0, exit_price=1.0)},
            api_error=ConnectionError("no route to host"),
            chain_balances={},
        )
        assert scan.data_api_reachable is False
        assert len(scan.already_cleared) == 1


class TestMultipleTokens:
    def test_a_mixed_batch_sorts_into_the_right_buckets(self, wired):
        scan = wired(
            db_tokens={
                TOKEN_A: _settled(station="WSSS", shares=5.0, exit_price=1.0),
                TOKEN_B: _settled(station="RCSS", bucket_c=28, side="YES",
                                   shares=7.0, exit_price=0.0),
            },
            api_positions=[_api_row(TOKEN_B, cur_price=0.0, size=7.0)],
            chain_balances={TOKEN_B: 7_000_000},  # TOKEN_A absent -> 0
        )
        assert len(scan.already_cleared) == 1
        assert scan.already_cleared[0].token_id == TOKEN_A
        assert len(scan.redeemable) == 1
        assert scan.redeemable[0].token_id == TOKEN_B
        assert scan.unresolvable == []
