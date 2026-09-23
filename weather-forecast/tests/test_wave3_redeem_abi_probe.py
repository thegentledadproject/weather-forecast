"""
Wave 3 item 3f. The probe must build the selector redeem.py's encoder uses,
scan PUSH4 immediates correctly (a selector inside another PUSH's data is
not a dispatcher entry), read the amounts lines out of a redeemPositions
body, run offline, and never import signing code.
"""
import ast
import pathlib

import redeem_abi_probe as probe
from clients import onchain_client

SRC = pathlib.Path(probe.__file__).read_text(encoding="utf-8")

ALLOWED_IMPORTS = {"argparse", "json", "os", "sys", "urllib", "urllib.error", "urllib.request", "eth_utils"}


def test_the_selector_is_the_one_redeem_py_encodes():
    assert probe.selector("redeemPositions(bytes32,uint256[])") == "0xdbeccb23"
    assert probe.selector("redeemPositions(bytes32,uint256[])") == "0x" + onchain_client.REDEEM_POSITIONS_SELECTOR.hex()
    assert probe.selector("safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)") == "0x2eb2c2d6"


def test_push4_scanner_skips_selectors_buried_in_other_push_data():
    # PUSH5 carrying 63dbeccb23 as DATA, then a real PUSH4 dbeccb23, then a real PUSH4 2eb2c2d6.
    code = "0x" + "64" + "63dbeccb23" + "80" + "63" + "dbeccb23" + "14" + "63" + "2eb2c2d6"
    assert probe.push4_immediates(code) == {"0xdbeccb23", "0x2eb2c2d6"}


def test_source_reader_finds_the_two_element_indexing():
    source = """
contract NegRiskAdapter {
    function redeemPositions(bytes32 _conditionId, uint256[] calldata _amounts) external {
        uint256[] memory positionIds = new uint256[](2);
        positionIds[0] = getPositionId(_conditionId, true);
        positionIds[1] = getPositionId(_conditionId, false);
        ctf.safeBatchTransferFrom(msg.sender, address(this), positionIds, _amounts, "");
        uint256 payout = _amounts[0] + _amounts[1];
    }
    function other() external { uint256 amounts = 1; }
}
"""
    lines = probe.redeem_positions_lines(source)
    assert len(lines) == 3 and "function redeemPositions" in lines[0]
    assert any("_amounts[1]" in l for l in lines)
    assert not any("function other" in l for l in lines)


def test_offline_prints_the_selector_and_the_manual_steps(capsys):
    assert probe.main(["--offline"]) == 0
    out = capsys.readouterr().out
    assert "0xdbeccb23" in out and "redeemPositions(bytes32,uint256[])" in out
    assert "--api-key" in out and "OFFLINE" in out


def test_the_probe_imports_no_signing_code():
    tree = ast.parse(SRC)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= ALLOWED_IMPORTS, sorted(imported - ALLOWED_IMPORTS)
    for forbidden in ("eth_account", "clients", "redeem", "wallet_client", "py_clob_client_v2", "onchain_client"):
        assert forbidden not in imported
    assert "private_key" not in SRC
    assert "eth_sendRawTransaction" not in SRC and "Account" not in SRC
