"""
tests/test_onchain_client.py

Calldata encoding, pinned byte-for-byte. Per the 2026-09-01 redemption
design's own testing section: "A silent change in either [encoding] is a
wrong transaction, so this is the load-bearing test."

No test here touches the network. Encoding tests call eth_abi directly as
an independent oracle -- the same library the implementation uses, but
invoked separately in the test so a bug in argument order, type-string
syntax, or byte layout inside the wrapper is caught rather than the test
just echoing the implementation's own output back at it.
"""
import eth_abi
import pytest

from clients import onchain_client


CONDITION_ID = b"\x11" * 32
SIGNER = "0x" + "aa" * 20
CALL_TO = "0x" + "bb" * 20


class TestSelectors:
    def test_redeem_positions_selector(self):
        assert onchain_client.REDEEM_POSITIONS_SELECTOR == bytes.fromhex("dbeccb23")

    def test_execute_selector(self):
        """
        Pinned against the design doc's own confirmed value -- recovered
        from on-chain bytecode plus an openchain.xyz signature lookup, not
        inferred. If this ever drifts, the type-string canonicalization
        below it is wrong, not the reference value.
        """
        assert onchain_client.EXECUTE_SELECTOR == bytes.fromhex("e8c8bf64")


class TestEncodeRedeemPositions:
    def test_matches_direct_abi_encoding(self):
        amounts = [10**18, 2 * 10**18]
        calldata = onchain_client.encode_redeem_positions(CONDITION_ID, amounts)

        expected = onchain_client.REDEEM_POSITIONS_SELECTOR + eth_abi.encode(
            ["bytes32", "uint256[]"], [CONDITION_ID, amounts],
        )
        assert calldata == expected

    def test_a_single_amount(self):
        calldata = onchain_client.encode_redeem_positions(CONDITION_ID, [5 * 10**17])
        expected = onchain_client.REDEEM_POSITIONS_SELECTOR + eth_abi.encode(
            ["bytes32", "uint256[]"], [CONDITION_ID, [5 * 10**17]],
        )
        assert calldata == expected

    def test_condition_id_must_be_32_bytes(self):
        with pytest.raises(ValueError, match="32 bytes"):
            onchain_client.encode_redeem_positions(b"\x11" * 31, [1])

    def test_condition_id_as_a_hex_string_is_accepted(self):
        """Callers naturally have this as a 0x-prefixed hex string from an API response."""
        hex_form = "0x" + CONDITION_ID.hex()
        assert (onchain_client.encode_redeem_positions(hex_form, [1])
                == onchain_client.encode_redeem_positions(CONDITION_ID, [1]))


class TestEncodeExecute:
    def test_matches_direct_abi_encoding(self):
        calls = [(CALL_TO, 0, b"\xde\xad\xbe\xef")]
        signature = b"\x99" * 65
        calldata = onchain_client.encode_execute(
            signer=SIGNER, nonce=7, deadline=1893456000, calls=calls, signature=signature,
        )

        expected = onchain_client.EXECUTE_SELECTOR + eth_abi.encode(
            ["(address,uint256,uint256,(address,uint256,bytes)[])", "bytes"],
            [(SIGNER, 7, 1893456000, calls), signature],
        )
        assert calldata == expected

    def test_multiple_calls_in_one_envelope(self):
        calls = [(CALL_TO, 0, b"\x01"), (CALL_TO, 0, b"\x02")]
        calldata = onchain_client.encode_execute(
            signer=SIGNER, nonce=0, deadline=2000000000, calls=calls, signature=b"\x00" * 65,
        )
        expected = onchain_client.EXECUTE_SELECTOR + eth_abi.encode(
            ["(address,uint256,uint256,(address,uint256,bytes)[])", "bytes"],
            [(SIGNER, 0, 2000000000, calls), b"\x00" * 65],
        )
        assert calldata == expected

    def test_a_redemption_wrapped_in_one_call_carries_the_inner_calldata_verbatim(self):
        """
        The actual shape this feature submits: one execute() envelope
        wrapping one call into NegRiskAdapter.redeemPositions().
        """
        redeem_calldata = onchain_client.encode_redeem_positions(CONDITION_ID, [5 * 10**17])
        calls = [(CALL_TO, 0, redeem_calldata)]
        calldata = onchain_client.encode_execute(
            signer=SIGNER, nonce=0, deadline=2000000000, calls=calls, signature=b"\x00" * 65,
        )
        assert calldata.startswith(onchain_client.EXECUTE_SELECTOR)
        assert redeem_calldata in calldata

    def test_signature_must_be_65_bytes(self):
        with pytest.raises(ValueError, match="65 bytes"):
            onchain_client.encode_execute(
                signer=SIGNER, nonce=0, deadline=1, calls=[], signature=b"\x00" * 64,
            )


# ---------------------------------------------------------------------------
# RPC primitives. Transport faked at requests.post -- never a real socket.
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, json_body, status_code=200):
        self._json = json_body
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _rpc_result(hex_result):
    return _Resp({"jsonrpc": "2.0", "id": 1, "result": hex_result})


def _rpc_error(message, data=None):
    err = {"code": -32000, "message": message}
    if data is not None:
        err["data"] = data
    return _Resp({"jsonrpc": "2.0", "id": 1, "error": err})


TEST_RPC_URL = "https://example-polygon-rpc.test"


class TestSimulateCall:
    def test_a_successful_call_returns_the_raw_result_bytes(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _rpc_result("0x0000000000000000000000000000000000000000000000000000000000000001")

        monkeypatch.setattr(onchain_client.requests, "post", fake_post)

        result = onchain_client.simulate_call(
            to="0x" + "22" * 20, calldata=b"\xab\xcd", rpc_url=TEST_RPC_URL,
        )

        assert result == bytes.fromhex("00" * 31 + "01")
        assert captured["url"] == TEST_RPC_URL
        assert captured["json"]["method"] == "eth_call"
        call_obj = captured["json"]["params"][0]
        assert call_obj["to"] == "0x" + "22" * 20
        assert call_obj["data"] == "0xabcd"

    def test_from_address_is_included_when_given(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: (captured.update(json=json), _rpc_result("0x"))[-1],
        )
        onchain_client.simulate_call(
            to="0x" + "22" * 20, calldata=b"", rpc_url=TEST_RPC_URL, from_address="0x" + "33" * 20,
        )
        assert captured["json"]["params"][0]["from"] == "0x" + "33" * 20

    def test_a_revert_with_a_decodable_reason_raises_it(self, monkeypatch):
        """
        Error(string) selector 0x08c379a0 followed by the ABI-encoded reason
        -- what most nodes return for a require()/revert("...") failure.
        """
        reason_data = "0x08c379a0" + eth_abi.encode(["string"], ["insufficient balance"]).hex()
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_error("execution reverted", data=reason_data),
        )

        with pytest.raises(onchain_client.SimulationReverted, match="insufficient balance"):
            onchain_client.simulate_call(to="0x" + "22" * 20, calldata=b"", rpc_url=TEST_RPC_URL)

    def test_a_revert_with_no_decodable_data_falls_back_to_the_message(self, monkeypatch):
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_error("execution reverted"),
        )

        with pytest.raises(onchain_client.SimulationReverted, match="execution reverted"):
            onchain_client.simulate_call(to="0x" + "22" * 20, calldata=b"", rpc_url=TEST_RPC_URL)


class TestGetNonce:
    def test_decodes_the_returned_uint256(self, monkeypatch):
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_result(
                "0x" + (42).to_bytes(32, "big").hex()
            ),
        )
        assert onchain_client.get_nonce("0x" + "22" * 20, TEST_RPC_URL) == 42

    def test_calls_the_nonce_selector(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: (
                captured.update(json=json), _rpc_result("0x" + (0).to_bytes(32, "big").hex())
            )[-1],
        )
        onchain_client.get_nonce("0x" + "22" * 20, TEST_RPC_URL)
        assert captured["json"]["params"][0]["data"] == "0x" + onchain_client._selector("nonce()").hex()


class TestGetErc1155Balance:
    def test_decodes_the_returned_uint256(self, monkeypatch):
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_result(
                "0x" + (9181817).to_bytes(32, "big").hex()
            ),
        )
        balance = onchain_client.get_erc1155_balance(
            contract_address="0x" + "44" * 20, owner="0x" + "22" * 20,
            token_id=123456, rpc_url=TEST_RPC_URL,
        )
        assert balance == 9181817

    def test_calls_balanceof_with_owner_and_token_id(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: (
                captured.update(json=json), _rpc_result("0x" + (0).to_bytes(32, "big").hex())
            )[-1],
        )
        onchain_client.get_erc1155_balance(
            contract_address="0x" + "44" * 20, owner="0x" + "22" * 20,
            token_id=123456, rpc_url=TEST_RPC_URL,
        )
        data = captured["json"]["params"][0]["data"]
        expected = "0x" + onchain_client._selector("balanceOf(address,uint256)").hex() + eth_abi.encode(
            ["address", "uint256"], ["0x" + "22" * 20, 123456],
        ).hex()
        assert data == expected


class TestGetPolBalance:
    def test_reads_via_eth_getbalance(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: (
                captured.update(json=json),
                _rpc_result("0x" + (0).to_bytes(32, "big").hex()),
            )[-1],
        )
        onchain_client.get_pol_balance("0x" + "22" * 20, TEST_RPC_URL)
        assert captured["json"]["method"] == "eth_getBalance"
        assert captured["json"]["params"] == ["0x" + "22" * 20, "latest"]

    def test_returns_wei_as_an_int(self, monkeypatch):
        one_pol_wei = 10**18
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_result(hex(one_pol_wei)),
        )
        assert onchain_client.get_pol_balance("0x" + "22" * 20, TEST_RPC_URL) == one_pol_wei


class TestGetEip712Domain:
    """
    EIP-5267's eip712Domain() returns
    (bytes1 fields, string name, string version, uint256 chainId,
     address verifyingContract, bytes32 salt, uint256[] extensions).
    """

    def test_decodes_name_version_chainid_and_verifying_contract(self, monkeypatch):
        encoded = eth_abi.encode(
            ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"],
            [b"\x0f", "Polymarket Proxy", "1", 137, "0x" + "55" * 20, b"\x00" * 32, []],
        )
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_result("0x" + encoded.hex()),
        )

        domain = onchain_client.get_eip712_domain("0x" + "22" * 20, TEST_RPC_URL)

        assert domain["name"] == "Polymarket Proxy"
        assert domain["version"] == "1"
        assert domain["chainId"] == 137
        assert domain["verifyingContract"].lower() == "0x" + "55" * 20


# ---------------------------------------------------------------------------
# EIP-712 signing.
#
# WHAT THIS CAN AND CANNOT PROVE. There is no published ABI for this proxy,
# so nothing here can prove the signature is valid against the REAL
# contract -- that is what mandatory simulation is for (see the module
# docstring). What these tests prove instead: sign_execute_payload()
# constructs the EIP-712 struct it CLAIMS to, consistently, with the domain
# and field values it was given -- verified by building the identical
# struct independently with eth_account.Account.sign_typed_data and
# checking the two signatures match byte-for-byte.
# ---------------------------------------------------------------------------

from eth_account import Account

TEST_PRIVATE_KEY = "0x" + "42" * 32
TEST_DOMAIN = {
    "name": "Polymarket Proxy", "version": "1", "chainId": 137,
    "verifyingContract": "0x" + "22" * 20,
}


class TestSignExecutePayload:
    def test_matches_an_independent_eth_account_signature_of_the_same_struct(self):
        calls = [(CALL_TO, 0, b"\xde\xad\xbe\xef")]
        signer = Account.from_key(TEST_PRIVATE_KEY).address

        signature = onchain_client.sign_execute_payload(
            private_key=TEST_PRIVATE_KEY, domain=TEST_DOMAIN,
            signer=signer, nonce=3, deadline=1893456000, calls=calls,
        )

        expected = Account.sign_typed_data(
            TEST_PRIVATE_KEY, domain_data=TEST_DOMAIN,
            message_types=onchain_client.EXECUTE_TYPED_DATA_TYPES,
            message_data={
                "signer": signer, "nonce": 3, "deadline": 1893456000,
                "calls": [{"to": t, "value": v, "data": d} for t, v, d in calls],
            },
        )
        assert signature == expected.signature

    def test_the_signature_is_65_bytes_ready_for_encode_execute(self):
        signer = Account.from_key(TEST_PRIVATE_KEY).address
        signature = onchain_client.sign_execute_payload(
            private_key=TEST_PRIVATE_KEY, domain=TEST_DOMAIN,
            signer=signer, nonce=0, deadline=2000000000, calls=[],
        )
        assert len(signature) == 65
        # encode_execute() must accept it without raising
        onchain_client.encode_execute(
            signer=signer, nonce=0, deadline=2000000000, calls=[], signature=signature,
        )

    def test_different_nonces_produce_different_signatures(self):
        """A sanity check against the signing path silently ignoring nonce --
        the ONE field whose entire purpose is replay protection."""
        signer = Account.from_key(TEST_PRIVATE_KEY).address
        sig_a = onchain_client.sign_execute_payload(
            private_key=TEST_PRIVATE_KEY, domain=TEST_DOMAIN,
            signer=signer, nonce=1, deadline=2000000000, calls=[],
        )
        sig_b = onchain_client.sign_execute_payload(
            private_key=TEST_PRIVATE_KEY, domain=TEST_DOMAIN,
            signer=signer, nonce=2, deadline=2000000000, calls=[],
        )
        assert sig_a != sig_b


# ---------------------------------------------------------------------------
# The EOA's transaction: building, signing, broadcasting, and the receipt.
#
# broadcast_transaction() is the one function in this module that moves real
# assets when actually called against a real endpoint. Nothing here calls it
# against anything but a fake transport -- see the module docstring's
# "NEVER" and clients/onchain_client.py's own separation of chain plumbing
# from redemption policy.
# ---------------------------------------------------------------------------

class TestGetTransactionCount:
    def test_reads_the_eoa_nonce_at_pending(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: (
                captured.update(json=json), _rpc_result(hex(11))
            )[-1],
        )
        count = onchain_client.get_transaction_count("0x" + "22" * 20, TEST_RPC_URL)
        assert count == 11
        assert captured["json"]["method"] == "eth_getTransactionCount"
        assert captured["json"]["params"] == ["0x" + "22" * 20, "pending"]


class TestEstimateGas:
    def test_decodes_the_returned_gas_estimate(self, monkeypatch):
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_result(hex(123456)),
        )
        gas = onchain_client.estimate_gas(
            to="0x" + "22" * 20, calldata=b"\xab", from_address="0x" + "33" * 20,
            rpc_url=TEST_RPC_URL,
        )
        assert gas == 123456


class TestBuildAndSignTransaction:
    def test_produces_a_raw_transaction_and_matching_hash(self):
        raw_tx, tx_hash = onchain_client.build_and_sign_transaction(
            private_key=TEST_PRIVATE_KEY, to="0x" + "22" * 20, calldata=b"\xab\xcd",
            chain_id=137, nonce=5, gas_limit=200000,
            max_fee_per_gas=50_000_000_000, max_priority_fee_per_gas=30_000_000_000,
        )
        assert isinstance(raw_tx, bytes) and len(raw_tx) > 0
        assert tx_hash.startswith("0x") and len(tx_hash) == 66

    def test_matches_an_independent_eth_account_signature(self):
        from eth_account import Account

        raw_tx, tx_hash = onchain_client.build_and_sign_transaction(
            private_key=TEST_PRIVATE_KEY, to="0x" + "22" * 20, calldata=b"\xab\xcd",
            chain_id=137, nonce=5, gas_limit=200000,
            max_fee_per_gas=50_000_000_000, max_priority_fee_per_gas=30_000_000_000,
        )
        expected = Account.from_key(TEST_PRIVATE_KEY).sign_transaction({
            "type": 2, "chainId": 137, "nonce": 5, "to": "0x" + "22" * 20,
            "value": 0, "data": b"\xab\xcd", "gas": 200000,
            "maxFeePerGas": 50_000_000_000, "maxPriorityFeePerGas": 30_000_000_000,
        })
        assert raw_tx == bytes(expected.raw_transaction)
        assert tx_hash == "0x" + expected.hash.hex()


class TestBroadcastTransaction:
    def test_sends_the_raw_transaction_and_returns_the_hash(self, monkeypatch):
        captured = {}
        tx_hash = "0x" + "ab" * 32
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: (
                captured.update(json=json), _rpc_result(tx_hash)
            )[-1],
        )
        result = onchain_client.broadcast_transaction(b"\x02\xf8\x70", TEST_RPC_URL)
        assert result == tx_hash
        assert captured["json"]["method"] == "eth_sendRawTransaction"
        assert captured["json"]["params"] == ["0x02f870"]

    def test_a_broadcast_rejected_by_the_node_raises(self, monkeypatch):
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_error("nonce too low"),
        )
        with pytest.raises(onchain_client.RpcError, match="nonce too low"):
            onchain_client.broadcast_transaction(b"\x02\xf8\x70", TEST_RPC_URL)


class TestWaitForReceipt:
    def test_returns_the_receipt_once_mined(self, monkeypatch):
        calls = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return _rpc_result(None)
            return _rpc_result({"status": "0x1", "transactionHash": "0x" + "ab" * 32})

        monkeypatch.setattr(onchain_client.requests, "post", fake_post)
        monkeypatch.setattr(onchain_client.time, "sleep", lambda s: None)

        receipt = onchain_client.wait_for_receipt(
            "0x" + "ab" * 32, TEST_RPC_URL, timeout_s=10, poll_interval_s=0.01,
        )
        assert receipt["status"] == "0x1"
        assert calls["n"] == 3

    def test_returns_none_if_never_mined_within_the_timeout(self, monkeypatch):
        monkeypatch.setattr(
            onchain_client.requests, "post",
            lambda url, json=None, timeout=None: _rpc_result(None),
        )
        fake_time = {"t": 0.0}
        monkeypatch.setattr(onchain_client.time, "sleep", lambda s: fake_time.__setitem__("t", fake_time["t"] + s))
        monkeypatch.setattr(onchain_client.time, "monotonic", lambda: fake_time["t"])

        receipt = onchain_client.wait_for_receipt(
            "0x" + "ab" * 32, TEST_RPC_URL, timeout_s=1, poll_interval_s=0.3,
        )
        assert receipt is None


class TestGetGasPriceSuggestion:
    def test_computes_max_fee_as_double_base_plus_priority(self, monkeypatch):
        """
        ONE reading, no escalation policy -- see the function's own
        docstring. max_fee = 2*baseFee + priorityFee is a conventional
        margin, not a formula this test should treat as sacred; it exists
        so the assertion has something concrete to check.
        """
        def fake_post(url, json=None, timeout=None):
            if json["method"] == "eth_maxPriorityFeePerGas":
                return _rpc_result(hex(30_000_000_000))
            if json["method"] == "eth_getBlockByNumber":
                return _rpc_result({"baseFeePerGas": hex(50_000_000_000)})
            raise AssertionError(f"unexpected method {json['method']}")

        monkeypatch.setattr(onchain_client.requests, "post", fake_post)

        suggestion = onchain_client.get_gas_price_suggestion(TEST_RPC_URL)

        assert suggestion["max_priority_fee_per_gas"] == 30_000_000_000
        assert suggestion["max_fee_per_gas"] == 2 * 50_000_000_000 + 30_000_000_000
