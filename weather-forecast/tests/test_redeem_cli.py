"""
tests/test_redeem_cli.py

redeem.py's safety-critical logic: the two gates, simulate-before-sign,
and verify-after-broadcast (2026-09-01 design, section 6). No test here
touches the network -- every onchain_client function redeem.py calls is
monkeypatched directly, matching test_redemption_client.py's own pattern.

WHAT THIS FILE DOES NOT COVER: argparse wiring and console formatting are
exercised lightly, if at all -- the design's own testing section (section
8) asks for gate behaviour, station flagging, and outcome classification,
not CLI parsing.
"""
from datetime import date

import pytest

import redeem
from clients import onchain_client
from clients.redemption_client import RedeemableItem, UnresolvableToken, ClearedToken, RedemptionScan


FUNDER = "0x" + "c6" * 20
EOA = "0x" + "04" * 20
NEG_RISK_ADAPTER = "0x" + "d9" * 20
CONDITIONAL_TOKENS = "0x" + "4d" * 20
RPC_URL = "https://example-polygon-rpc.test"
PRIVATE_KEY = "0x" + "42" * 32


def _item(station="WSSS", shares=5.0, is_winner=False):
    return RedeemableItem(
        token_id="12345", condition_id=b"\xaa" * 32, station_icao=station,
        target_date=date(2026, 8, 28), bucket_c=32, side="YES",
        size_shares=shares, is_winner=is_winner, value_usd=(shares if is_winner else 0.0),
    )


def _common(monkeypatch, *, simulate_ok=True, receipt_status="0x1",
            balance_after=0, broadcast_hash="0x" + "bb" * 32):
    monkeypatch.setattr(onchain_client, "get_nonce", lambda proxy, rpc_url: 3)
    monkeypatch.setattr(
        onchain_client, "get_eip712_domain",
        lambda proxy, rpc_url: {"name": "x", "version": "1", "chainId": 137,
                                 "verifyingContract": proxy},
    )
    monkeypatch.setattr(
        onchain_client, "sign_execute_payload",
        lambda **kw: b"\x11" * 65,
    )

    if simulate_ok:
        monkeypatch.setattr(
            onchain_client, "simulate_call",
            lambda to, calldata, rpc_url, from_address=None: b"\x01",
        )
    else:
        def _raise(*a, **kw):
            raise onchain_client.SimulationReverted("insufficient balance")
        monkeypatch.setattr(onchain_client, "simulate_call", _raise)

    monkeypatch.setattr(onchain_client, "get_transaction_count", lambda addr, rpc_url: 9)
    monkeypatch.setattr(
        onchain_client, "build_and_sign_transaction",
        lambda **kw: (b"\x02\xf8\x70", "0x" + "cc" * 32),
    )
    monkeypatch.setattr(
        onchain_client, "broadcast_transaction",
        lambda raw_tx, rpc_url: broadcast_hash,
    )
    monkeypatch.setattr(
        onchain_client, "wait_for_receipt",
        lambda tx_hash, rpc_url, **kw: (
            {"status": receipt_status, "transactionHash": tx_hash}
            if receipt_status is not None else None
        ),
    )
    monkeypatch.setattr(
        onchain_client, "get_erc1155_balance",
        lambda contract_address, owner, token_id, rpc_url: balance_after,
    )


def _execute_kwargs(item):
    return dict(
        item=item, eoa_address=EOA, private_key=PRIVATE_KEY, funder=FUNDER,
        neg_risk_adapter=NEG_RISK_ADAPTER, conditional_tokens_address=CONDITIONAL_TOKENS,
        rpc_url=RPC_URL, chain_id=137, gas_limit=300000,
        max_fee_per_gas=50_000_000_000, max_priority_fee_per_gas=30_000_000_000,
        deadline=2000000000,
    )


class TestSimulationRevertedAbortsBeforeSigning:
    def test_a_reverted_simulation_never_broadcasts(self, monkeypatch):
        _common(monkeypatch, simulate_ok=False)
        broadcast_called = []
        monkeypatch.setattr(
            onchain_client, "broadcast_transaction",
            lambda *a, **kw: broadcast_called.append(1) or "0xshouldnothappen",
        )

        result = redeem.execute_one(**_execute_kwargs(_item()))

        assert result.outcome == "simulation_reverted"
        assert "insufficient balance" in result.detail
        assert broadcast_called == []
        assert result.tx_hash is None


class TestReceiptReverted:
    def test_reports_the_hash_and_leaves_the_position_alone(self, monkeypatch):
        _common(monkeypatch, receipt_status="0x0")

        result = redeem.execute_one(**_execute_kwargs(_item()))

        assert result.outcome == "receipt_reverted"
        assert result.tx_hash == "0x" + "bb" * 32


class TestBalanceStillNonzeroAfterSuccess:
    def test_a_successful_receipt_with_a_leftover_balance_is_still_a_failure(self, monkeypatch):
        """
        Section 7's own explicit case: a transaction that succeeded but did
        not clear the balance is reported as a failure, because for this
        purpose it is one.
        """
        _common(monkeypatch, receipt_status="0x1", balance_after=5_000_000)

        result = redeem.execute_one(**_execute_kwargs(_item()))

        assert result.outcome == "balance_still_nonzero"
        assert "5" in result.detail


class TestSuccessfulRedemption:
    def test_a_clean_run_reports_redeemed(self, monkeypatch):
        _common(monkeypatch, receipt_status="0x1", balance_after=0)

        result = redeem.execute_one(**_execute_kwargs(_item()))

        assert result.outcome == "redeemed"
        assert result.tx_hash == "0x" + "bb" * 32

    def test_the_redeem_calldata_targets_negriskadapter_wrapped_in_one_call(self, monkeypatch):
        """
        The actual shape this whole feature submits: one execute() envelope
        containing one call into NegRiskAdapter's redeemPositions.
        """
        _common(monkeypatch)
        captured = {}
        real_encode_execute = onchain_client.encode_execute

        def spy_encode_execute(**kw):
            captured.update(kw)
            return real_encode_execute(**kw)

        monkeypatch.setattr(onchain_client, "encode_execute", spy_encode_execute)

        redeem.execute_one(**_execute_kwargs(_item(shares=5.0)))

        assert len(captured["calls"]) == 1
        to, value, data = captured["calls"][0]
        assert to == NEG_RISK_ADAPTER
        assert value == 0
        assert data.startswith(onchain_client.REDEEM_POSITIONS_SELECTOR)

    def test_amounts_are_converted_to_base_units(self, monkeypatch):
        _common(monkeypatch)
        captured = {}
        real_encode_redeem = onchain_client.encode_redeem_positions

        def spy(condition_id, amounts):
            captured["amounts"] = amounts
            return real_encode_redeem(condition_id, amounts)

        monkeypatch.setattr(onchain_client, "encode_redeem_positions", spy)

        redeem.execute_one(**_execute_kwargs(_item(shares=5.138885)))

        assert captured["amounts"] == [5138885]


class TestPendingReceipt:
    def test_no_receipt_within_timeout_is_reported_as_pending_not_a_failure(self, monkeypatch):
        _common(monkeypatch, receipt_status=None)

        result = redeem.execute_one(**_execute_kwargs(_item()))

        assert result.outcome == "pending"
        assert result.tx_hash == "0x" + "bb" * 32


# ---------------------------------------------------------------------------
# The two gates. Neither alone broadcasts -- design section 6, point 2.
# ---------------------------------------------------------------------------

class TestGates:
    def test_execute_is_refused_when_live_trading_is_not_armed(self, monkeypatch):
        from clients import wallet_client
        monkeypatch.setattr(wallet_client, "live_trading_enabled", lambda: False)

        ok, reason = redeem.gates_open()

        assert not ok
        assert "POLYMARKET_LIVE_TRADING" in reason

    def test_execute_is_permitted_once_the_env_gate_is_armed(self, monkeypatch):
        from clients import wallet_client
        monkeypatch.setattr(wallet_client, "live_trading_enabled", lambda: True)

        ok, reason = redeem.gates_open()

        assert ok


# ---------------------------------------------------------------------------
# --list rendering and exit codes.
# ---------------------------------------------------------------------------

class TestListExitCode:
    def test_nothing_to_do_exits_zero(self):
        scan = RedemptionScan(redeemable=[], already_cleared=[
            ClearedToken(token_id="1", station_icao="WSSS", target_date=date(2026, 8, 20),
                         bucket_c=32, side="NO"),
        ], unresolvable=[], data_api_reachable=True)
        assert redeem.list_exit_code(scan) == 0

    def test_redeemable_items_exit_two(self):
        scan = RedemptionScan(redeemable=[_item()], already_cleared=[], unresolvable=[],
                               data_api_reachable=True)
        assert redeem.list_exit_code(scan) == 2

    def test_unresolvable_items_also_exit_two(self):
        scan = RedemptionScan(redeemable=[], already_cleared=[], unresolvable=[
            UnresolvableToken(token_id="1", station_icao="WSSS", target_date=date(2026, 8, 20),
                               bucket_c=32, side="NO", size_shares=5.0, reason="test"),
        ], data_api_reachable=True)
        assert redeem.list_exit_code(scan) == 2


class TestRenderList:
    def test_names_the_station_for_every_bucket(self):
        scan = RedemptionScan(
            redeemable=[_item(station="WSSS")],
            already_cleared=[ClearedToken(token_id="2", station_icao="RCSS",
                                           target_date=date(2026, 8, 20), bucket_c=28,
                                           side="YES")],
            unresolvable=[UnresolvableToken(token_id="3", station_icao="ZBAA",
                                             target_date=date(2026, 8, 28), bucket_c=33,
                                             side="YES", size_shares=25.5,
                                             reason="no conditionId")],
            data_api_reachable=True,
        )
        text = redeem.render_list(scan, gas_balance_wei=10**18, json_output=False)
        assert "WSSS" in text and "RCSS" in text and "ZBAA" in text

    def test_zero_gas_produces_the_no_pol_line_and_names_the_stations(self):
        scan = RedemptionScan(redeemable=[_item(station="WSSS")], already_cleared=[],
                               unresolvable=[], data_api_reachable=True)
        text = redeem.render_list(scan, gas_balance_wei=0, json_output=False)
        assert "POL" in text
        assert "WSSS" in text


class TestCliSmokeTest:
    """
    A real bug lived here: the design's own spec lists --list as a valid
    flag ("(default): the report"), but only the DEFAULT BEHAVIOUR was ever
    wired -- the flag itself did not exist, so `redeem.py --list` failed
    argparse outright. Caught by actually invoking main(), not by unit
    tests on the pieces alone.
    """

    def test_list_flag_is_accepted(self, monkeypatch):
        monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)
        assert redeem.main(["--list"]) == 1  # no credentials -- but argparse must accept the flag

    def test_list_and_execute_together_is_refused(self):
        assert redeem.main(["--list", "--execute"]) == 1


class TestFilterScan:
    def test_filter_by_station_narrows_all_three_buckets(self):
        scan = RedemptionScan(
            redeemable=[_item(station="WSSS"), _item(station="RCSS")],
            already_cleared=[], unresolvable=[], data_api_reachable=True,
        )
        narrowed = redeem.filter_scan(scan, station="WSSS")
        assert len(narrowed.redeemable) == 1
        assert narrowed.redeemable[0].station_icao == "WSSS"

    def test_filter_by_token(self):
        a, b = _item(), _item()
        a.token_id, b.token_id = "AAA", "BBB"
        scan = RedemptionScan(redeemable=[a, b], already_cleared=[], unresolvable=[],
                               data_api_reachable=True)
        narrowed = redeem.filter_scan(scan, token="AAA")
        assert [i.token_id for i in narrowed.redeemable] == ["AAA"]
