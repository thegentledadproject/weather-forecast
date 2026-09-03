"""
tests/test_settled_token_reconciliation.py

The 2026-08-22 live-trading halt, pinned.

WHAT HAPPENED. A losing position resolves and position_manager writes
`closed_resolution` / exit_price 0.0 -- a DATABASE write. Nothing touches
the wallet, because there is no redemption code in this repo and a
worthless token has no book to sell into, so the outcome tokens stay in
the funding wallet indefinitely.

reconcile_live_positions() built `recorded_tokens` from OPEN live
positions only. A closed row is not in that set, so its still-held
balance landed in `exchange_only` -- "held but NOT RECORDED" -- and
_live_budget_breach() fails closed on that. Every live entry was blocked
from 2026-08-22T21:00Z onward by two worthless WSSS 32YES tokens
(9.181817 and 8.416665 shares).

It was a limit cycle, not a one-off: RECONCILE_TRADE_LOOKBACK_HOURS ages
the dust out of the fill scan four days after purchase, so the halt lifts
by itself and then the next losing resolution re-arms it.

WHY WHITELISTING IS SOUND, and why it is this narrow. exchange_only
exists to catch UNRECORDED EXPOSURE -- exposure the LIVE_MAX_* caps
cannot see because they are computed from the positions table. A
`closed_resolution` token is recorded (there is a row) and is not
exposure (the market has settled; its value is fixed at 0 or $1/share,
with no price risk left to cap). It is outside the question this check
asks.

Every OTHER closed status must keep tripping, and TestTheWhitelistStaysNarrow
is what holds that line. A stop-loss, take-profit or trailing-stop close
exited via a SELL, so a residual balance above the dust tolerance means the
sell did not fully happen -- a genuine divergence. Only a resolution close
never involved a sell at all.
"""
import logging
from datetime import date

import pytest

import config
from clients import wallet_client
from models import SettledToken


def _settled(shares, exit_price):
    """
    A settled-token record with a fixed identity. These tests are about
    reconciliation (does it block, does it warn), not the human label --
    the station/date/bucket/side values don't matter to any assertion here.
    """
    return SettledToken(station_icao="WSSS", target_date=date(2026, 8, 20),
                         bucket_c=32, side="YES", size_shares=shares, exit_price=exit_price)


class _FakeLib:
    """Mirrors the py_clob_client_v2 surface reconciliation actually touches."""

    class BalanceAllowanceParams:
        def __init__(self, asset_type=None, token_id=None):
            self.asset_type, self.token_id = asset_type, token_id

    class AssetType:
        CONDITIONAL = "CONDITIONAL"

    class TradeParams:
        def __init__(self, after=None):
            self.after = after


class _FakeClient:
    """
    Balances keyed by token id, in BASE UNITS -- the scale the real API
    returns, so _held_shares' conversion stays inside the path under test.
    """

    def __init__(self, balances, trades):
        self._balances, self._trades = balances, trades

    def get_balance_allowance(self, params):
        raw = self._balances.get(params.token_id, 0.0)
        return {"balance": raw * wallet_client.BALANCE_BASE_UNITS}

    def get_trades(self, params):
        return [{"asset_id": t} for t in self._trades]


class _Pos:
    def __init__(self, position_id, token_id, size_shares):
        self.position_id = position_id
        self.token_id = token_id
        self.size_shares = size_shares


# The two tokens that actually caused the halt, at their actual share counts.
LOSER_TOKEN = "5712315774911947" + "0" * 20
LOSER_SHARES = 9.181817
SECOND_LOSER_TOKEN = "9657263278490997" + "0" * 20
SECOND_LOSER_SHARES = 8.416665


@pytest.fixture
def wired(monkeypatch):
    """
    Wires the fake exchange in and hands back a callable that reconciles.
    Credentials are faked because the function fails closed without them.
    """
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xtest")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xtestfunder")
    monkeypatch.setattr(wallet_client, "_clob", lambda: _FakeLib)
    monkeypatch.setattr(config, "RECONCILE_IGNORE_TRADES_BEFORE", None)

    def run(balances, trades, open_positions=(), settled_tokens=None):
        monkeypatch.setattr(
            wallet_client, "get_client",
            lambda: _FakeClient(balances, trades),
        )
        return wallet_client.reconcile_live_positions(
            list(open_positions), settled_tokens=settled_tokens,
        )

    return run


class TestASettledLoserDoesNotBlockEntries:
    """The halt itself: a worthless resolved token must not fail the check."""

    def test_a_resolved_loser_still_held_reconciles_clean(self, wired):
        recon = wired(
            balances={LOSER_TOKEN: LOSER_SHARES},
            trades=[LOSER_TOKEN],
            settled_tokens={LOSER_TOKEN: _settled(LOSER_SHARES, 0.0)},
        )
        assert recon.ok, (
            "a token whose position row is closed_resolution is recorded and "
            "carries no price exposure -- it must not block a live entry. This "
            "is the 2026-08-22 halt."
        )
        assert recon.exchange_only == []

    def test_the_settled_token_is_still_reported_not_silently_dropped(self, wired):
        recon = wired(
            balances={LOSER_TOKEN: LOSER_SHARES},
            trades=[LOSER_TOKEN],
            settled_tokens={LOSER_TOKEN: _settled(LOSER_SHARES, 0.0)},
        )
        assert [t for t, *_ in recon.settled_unredeemed] == [LOSER_TOKEN], (
            "whitelisted is not the same as invisible -- the holding is real "
            "and must stay auditable"
        )

    def test_both_tokens_from_the_real_halt_reconcile_clean(self, wired):
        """The exact state the box was in, by token and share count."""
        recon = wired(
            balances={
                LOSER_TOKEN: LOSER_SHARES,
                SECOND_LOSER_TOKEN: SECOND_LOSER_SHARES,
            },
            trades=[LOSER_TOKEN, SECOND_LOSER_TOKEN],
            settled_tokens={
                LOSER_TOKEN: _settled(LOSER_SHARES, 0.0),
                SECOND_LOSER_TOKEN: _settled(SECOND_LOSER_SHARES, 0.0),
            },
        )
        assert recon.ok
        assert len(recon.settled_unredeemed) == 2

    def test_describe_names_the_settled_holdings_on_the_clean_path(self, wired):
        recon = wired(
            balances={LOSER_TOKEN: LOSER_SHARES},
            trades=[LOSER_TOKEN],
            settled_tokens={LOSER_TOKEN: _settled(LOSER_SHARES, 0.0)},
        )
        assert "settled" in recon.describe().lower()


class TestAnUnredeemedWinnerWarnsButDoesNotBlock:
    """
    Nothing in this repo redeems, so a winning resolution leaves real
    dollars sitting in the wallet. That is a message to the operator, not
    a reason to stop trading: the value is fixed and the tokens stay
    redeemable indefinitely.
    """

    def test_a_resolved_winner_does_not_block_entries(self, wired):
        recon = wired(
            balances={LOSER_TOKEN: 5.0},
            trades=[LOSER_TOKEN],
            settled_tokens={LOSER_TOKEN: _settled(5.0, 1.0)},
        )
        assert recon.ok

    def test_a_resolved_winner_logs_the_uncollected_dollars(self, wired, caplog):
        with caplog.at_level(logging.WARNING):
            wired(
                balances={LOSER_TOKEN: 5.0},
                trades=[LOSER_TOKEN],
                settled_tokens={LOSER_TOKEN: _settled(5.0, 1.0)},
            )
        warnings = "\n".join(
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "5.00" in warnings, (
            "the operator has to be told the dollar value sitting uncollected "
            "-- redemption is a manual step in this system"
        )

    def test_a_resolved_loser_does_not_cry_about_money(self, wired, caplog):
        """A worthless token is routine. Warning on it would train the eye to skip."""
        with caplog.at_level(logging.WARNING):
            wired(
                balances={LOSER_TOKEN: LOSER_SHARES},
                trades=[LOSER_TOKEN],
                settled_tokens={LOSER_TOKEN: _settled(LOSER_SHARES, 0.0)},
            )
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


class TestTheWhitelistStaysNarrow:
    """
    The line that keeps this a fix and not a hole. Anything that closed via
    a SELL must keep tripping on a residual balance.
    """

    def test_a_stop_loss_token_with_a_full_balance_still_blocks(self, wired):
        recon = wired(
            balances={LOSER_TOKEN: 5.0},
            trades=[LOSER_TOKEN],
            settled_tokens={},  # closed_stop_loss is NOT whitelisted
        )
        assert not recon.ok, (
            "a stop-loss close sold the shares. Shares still held means the "
            "sell did not happen, and that must keep blocking."
        )
        assert [t for t, _ in recon.exchange_only] == [LOSER_TOKEN]

    def test_an_entirely_unknown_token_still_blocks(self, wired):
        recon = wired(
            balances={"deadbeef": 7.5},
            trades=["deadbeef"],
            settled_tokens={LOSER_TOKEN: _settled(LOSER_SHARES, 0.0)},
        )
        assert not recon.ok
        assert [t for t, _ in recon.exchange_only] == ["deadbeef"]

    def test_dust_on_a_settled_token_is_not_reported_as_a_holding(self, wired):
        """Below tolerance is below tolerance, whitelist or not."""
        recon = wired(
            balances={LOSER_TOKEN: 0.008885},
            trades=[LOSER_TOKEN],
            settled_tokens={LOSER_TOKEN: _settled(LOSER_SHARES, 0.0)},
        )
        assert recon.ok
        assert recon.settled_unredeemed == []

    def test_the_db_only_direction_is_untouched(self, wired):
        """
        A settled whitelist must not blind the other half of the check: an
        OPEN position whose shares are gone is still a divergence.
        """
        recon = wired(
            balances={LOSER_TOKEN: 0.0},
            trades=[],
            open_positions=[_Pos("WSSS:open", LOSER_TOKEN, 5.0)],
            settled_tokens={LOSER_TOKEN: _settled(5.0, 0.0)},
        )
        assert not recon.ok
        assert [t for t, _, _ in recon.db_only] == [LOSER_TOKEN]


class TestBackwardCompatibility:
    def test_settled_tokens_defaults_to_none_and_nothing_is_whitelisted(self, wired):
        """Callers that pass no settled map get exactly today's behaviour."""
        recon = wired(
            balances={LOSER_TOKEN: LOSER_SHARES},
            trades=[LOSER_TOKEN],
        )
        assert not recon.ok
        assert [t for t, _ in recon.exchange_only] == [LOSER_TOKEN]
