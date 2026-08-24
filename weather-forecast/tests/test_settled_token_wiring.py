"""
tests/test_settled_token_wiring.py

The whitelist in wallet_client is inert unless the settled tokens actually
reach it. These cover the two links in that chain:

  storage.load_settled_live_tokens()   reads the closed_resolution rows
  executor._live_budget_breach()       passes them to reconciliation

The second is the one that matters -- the 2026-08-22 halt was an
executor-level symptom ("entry BLOCKED by a risk backstop"), so it is
proved fixed at that level, through the real call path, not by asserting
that a function was called with an argument.
"""
import sqlite3

import pytest

import config
from clients import wallet_client


HELD_TOKEN = "5712315774911947" + "0" * 20
SOLD_TOKEN = "7258321014310216" + "0" * 20


@pytest.fixture
def db(tmp_path, monkeypatch):
    """
    A throwaway trading db. config.DB_PATH is read at call time by
    storage._connect(), so patching it here covers both the seeding and
    the code under test.
    """
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    import storage

    storage.load_open_positions()  # forces schema creation
    return storage


def _insert(storage, position_id, token_id, status, exit_price,
            shares=9.181817, execution_mode="live", is_paper=0):
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO positions (position_id, station_icao, target_date, "
            "bucket_c, side, entry_price, size_usd, entry_time, status, "
            "high_water_mark, exit_price, token_id, is_paper, size_shares, "
            "execution_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, "WSSS", "2026-08-21", 32, "YES", 0.11, 1.01,
             "2026-08-20T21:40:54+00:00", status, 0.11, exit_price, token_id,
             is_paper, shares, execution_mode),
        )


class TestLoadSettledLiveTokens:
    def test_a_closed_resolution_live_row_is_returned_with_shares_and_exit(self, db):
        _insert(db, "WSSS:a", HELD_TOKEN, "closed_resolution", 0.0)
        assert db.load_settled_live_tokens() == {HELD_TOKEN: (9.181817, 0.0)}

    def test_a_winning_resolution_carries_its_exit_price(self, db):
        _insert(db, "WSSS:w", HELD_TOKEN, "closed_resolution", 1.0, shares=5.0)
        assert db.load_settled_live_tokens() == {HELD_TOKEN: (5.0, 1.0)}

    def test_a_stop_loss_row_is_not_returned(self, db):
        """The narrowness of the whitelist starts here, at the query."""
        _insert(db, "WSSS:s", SOLD_TOKEN, "closed_stop_loss", 0.27)
        assert db.load_settled_live_tokens() == {}

    def test_an_open_row_is_not_returned(self, db):
        _insert(db, "WSSS:o", HELD_TOKEN, "open", None)
        assert db.load_settled_live_tokens() == {}

    def test_a_paper_resolution_is_not_returned(self, db):
        """Paper rows never had tokens on the exchange to reconcile against."""
        _insert(db, "WSSS:p", HELD_TOKEN, "closed_resolution", 0.0,
                execution_mode="paper", is_paper=1)
        assert db.load_settled_live_tokens() == {}

    def test_a_row_with_no_token_id_is_skipped_not_keyed_on_none(self, db):
        _insert(db, "WSSS:n", None, "closed_resolution", 0.0)
        assert db.load_settled_live_tokens() == {}

    def test_no_rows_is_an_empty_dict_not_none(self, db):
        assert db.load_settled_live_tokens() == {}


class TestExecutorNoLongerBlocksOnSettledTokens:
    """
    End of the chain, and the actual regression. Seeds the state the box was
    in on 2026-08-22 -- a resolved worthless position whose tokens the wallet
    still holds -- and asserts an entry is no longer refused.
    """

    @pytest.fixture
    def exchange_holding(self, db, monkeypatch):
        """A wallet that still holds HELD_TOKEN, and nothing else."""
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xtest")
        monkeypatch.setenv("POLYMARKET_FUNDER", "0xtestfunder")
        monkeypatch.setattr(config, "RECONCILE_IGNORE_TRADES_BEFORE", None)
        monkeypatch.setattr(wallet_client, "_reconcile_cache",
                            {"at": 0.0, "result": None})

        class _Lib:
            class BalanceAllowanceParams:
                def __init__(self, asset_type=None, token_id=None):
                    self.token_id = token_id

            class AssetType:
                CONDITIONAL = "CONDITIONAL"

            class TradeParams:
                def __init__(self, after=None):
                    self.after = after

        class _Client:
            def get_balance_allowance(self, params):
                held = 9.181817 if params.token_id == HELD_TOKEN else 0.0
                return {"balance": held * wallet_client.BALANCE_BASE_UNITS}

            def get_trades(self, params):
                return [{"asset_id": HELD_TOKEN}]

        monkeypatch.setattr(wallet_client, "_clob", lambda: _Lib)
        monkeypatch.setattr(wallet_client, "get_client", lambda: _Client())
        return db

    def test_a_resolved_worthless_holding_no_longer_blocks_an_entry(
        self, exchange_holding, monkeypatch
    ):
        _insert(exchange_holding, "WSSS:2026-08-21:32:YES", HELD_TOKEN,
                "closed_resolution", 0.0)
        import executor

        monkeypatch.setattr(executor.storage, "count_live_order_attempts",
                            lambda kind, since: 0)
        breach = executor._live_budget_breach(1.00)
        assert breach is None or "reconciliation" not in breach, (
            f"the 2026-08-22 halt is back: {breach}"
        )

    def test_an_unrecorded_holding_still_blocks_the_entry(
        self, exchange_holding, monkeypatch
    ):
        """
        Same wallet, but the database has no row at all for the token it
        holds. That is the case the backstop exists for and it must still
        fire, or this fix has disabled the check rather than narrowed it.
        """
        import executor

        monkeypatch.setattr(executor.storage, "count_live_order_attempts",
                            lambda kind, since: 0)
        breach = executor._live_budget_breach(1.00)
        assert breach is not None and "reconciliation" in breach


class TestPreflightSurfacesUncollectedWinnings:
    """
    The mid-cycle warning only helps someone reading the journal at the
    right moment. preflight() is the block an operator actually reads after
    a restart, so an uncollected winner has to appear there too.

    It takes EXCHANGE-VERIFIED holdings -- Reconciliation.settled_unredeemed
    -- and not the raw database map, because the database cannot tell the
    two winner states apart. A redeemed winner's row still reads
    closed_resolution / exit_price 1.0 forever (WSSS:2026-08-20:32:NO is
    exactly that row), so a DB-only line would nag about money already
    collected. Only a non-zero balance distinguishes them.
    """

    def test_an_unredeemed_winner_gets_a_preflight_line(self):
        lines = wallet_client.preflight(
            settled_unredeemed=[("abc123def456789", 5.0, 1.0)],
        )
        winner = [ln for ln in lines if "REDEEM" in ln.upper()]
        assert winner, f"no redemption line in preflight: {lines}"
        assert "5.00" in winner[0]

    def test_a_worthless_settled_holding_gets_no_preflight_line(self):
        lines = wallet_client.preflight(
            settled_unredeemed=[("abc123def456789", 9.18, 0.0)],
        )
        assert not [ln for ln in lines if "REDEEM" in ln.upper()]

    def test_a_redeemed_winner_cannot_produce_a_line_because_it_is_not_held(self):
        """
        The guard against nagging forever. A redeemed winner has a zero
        balance, so reconciliation never puts it in settled_unredeemed --
        an empty list is the only thing preflight can be handed for it.
        """
        assert not [
            ln for ln in wallet_client.preflight(settled_unredeemed=[])
            if "REDEEM" in ln.upper()
        ]

    def test_preflight_without_the_argument_is_unchanged(self):
        assert not [ln for ln in wallet_client.preflight() if "REDEEM" in ln.upper()]
