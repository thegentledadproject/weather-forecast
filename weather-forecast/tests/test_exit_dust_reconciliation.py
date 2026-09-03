"""
tests/test_exit_dust_reconciliation.py

The 2026-08-30 live-trading halt, pinned.

WHAT HAPPENED. RCSS 2026-08-30 35 NO was bought live at 5.10714 shares and
stopped out the next morning. The exit went out as a FILL-OR-KILL SELL of
5.10 shares -- build_exit_order() floors onto the 2-decimal share grid --
and the first attempt was KILLED unfilled on a thin book. The retry a
minute later filled, but the exchange moved 5.09 shares, one grid step
short of the 5.10 submitted. Residue left in the wallet:

    5.10714 - 5.09 = 0.01714 shares, worth about half a cent.

RECONCILE_SHARE_TOLERANCE was 0.01, so that residue read as an unrecorded
holding, exchange_only was non-empty, and _live_budget_breach() fails
closed. Every live entry was blocked from 2026-08-30T21:01Z: 49 refused
candidates across WSSS and RCSS over two nights, and it would not have
cleared until the token aged out of the 96h fill scan on 2026-09-03.

WHY THE OLD BOUND WAS WRONG. Both wallet_client.SHARE_DECIMALS and
config.RECONCILE_SHARE_TOLERANCE documented the residue as "strictly less
than one grid step", derived from the flooring in build_exit_order(). That
derivation is sound and the arithmetic still holds -- but flooring is not
the only source of residue. The EXCHANGE decides the fill, and it can move
less than the size submitted. Two independent grid steps of shortfall put
the residue at 1.7x a bound that had no margin in it at all.

WHAT REPLACES IT. Not a bigger share tolerance -- a bound in the unit that
actually matters, which is EXPOSURE IN DOLLARS. An outcome token can never
settle above $1.00/share, so `held * $1.00` is a hard upper bound on what
a residue can ever be worth, needing no price lookup and no extra API call
that could fail. Below config.RECONCILE_DUST_MAX_VALUE_USD of worst-case
value, a holding is dust: reported, never counted as divergence.

WHY THAT IS SAFE. exchange_only exists to catch unrecorded EXPOSURE -- the
kind the LIVE_MAX_* caps cannot see. The exchange will not sell a position
smaller than config.ASSUMED_EXCHANGE_MIN_SHARES = 5 shares, so the
smallest position that can exist unrecorded reads as $5.00 of worst-case
value against a $0.50 bound: a 10x margin, where the old bound had none.
TestTheDustBoundKeepsItsMargin is what holds that line.
"""
import math
from datetime import date

import pytest

import config
from clients import wallet_client
from models import SettledToken


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
    """Balances keyed by token id in BASE UNITS -- the scale the API returns."""

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


# The token that actually caused the halt, at its actual numbers.
DUST_TOKEN = "6122870675273865" + "0" * 20
BOUGHT_SHARES = 5.10714
FILLED_SHARES = 5.09          # what the exchange moved, not what was submitted
DUST_SHARES = 0.01714         # verified on the box as 17,140 base units


@pytest.fixture
def wired(monkeypatch):
    """Wires the fake exchange in and hands back a callable that reconciles."""
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


class TestTheHaltItself:
    def test_a_partially_filled_exit_does_not_block_live_entries(self, wired):
        """
        The exact production state. closed_stop_loss is NOT whitelisted and
        must not be -- an unsold position is a real divergence. Half a cent
        of it is not.
        """
        recon = wired(
            balances={DUST_TOKEN: DUST_SHARES},
            trades=[DUST_TOKEN],
            settled_tokens={},
        )
        assert recon.ok, (
            "0.01714 shares worth under a cent must not stop the live book. "
            "This is the 2026-08-30 halt."
        )
        assert recon.exchange_only == []

    def test_the_residue_is_reported_not_silently_dropped(self, wired):
        recon = wired(
            balances={DUST_TOKEN: DUST_SHARES},
            trades=[DUST_TOKEN],
            settled_tokens={},
        )
        assert [t for t, *_ in recon.dust] == [DUST_TOKEN], (
            "ignored is not the same as invisible -- the shares are really "
            "held and must stay auditable"
        )

    def test_describe_names_the_dust(self, wired):
        recon = wired(
            balances={DUST_TOKEN: DUST_SHARES},
            trades=[DUST_TOKEN],
            settled_tokens={},
        )
        assert "dust" in recon.describe().lower()

    def test_the_arithmetic_of_the_incident(self):
        """The residue is one flooring step plus one step of fill shortfall."""
        factor = 10 ** wallet_client.SHARE_DECIMALS
        submitted = math.floor(BOUGHT_SHARES * factor + 1e-9) / factor
        assert submitted == pytest.approx(5.10)
        assert BOUGHT_SHARES - FILLED_SHARES == pytest.approx(DUST_SHARES)
        assert DUST_SHARES > config.RECONCILE_SHARE_TOLERANCE, (
            "if this ever stops being true the incident has been mis-recorded"
        )


class TestARealDivergenceStillBlocks:
    def test_a_whole_unsold_position_still_blocks(self, wired):
        recon = wired(
            balances={DUST_TOKEN: 5.0},
            trades=[DUST_TOKEN],
            settled_tokens={},
        )
        assert not recon.ok
        assert [t for t, _ in recon.exchange_only] == [DUST_TOKEN]

    def test_an_entirely_unknown_token_still_blocks(self, wired):
        recon = wired(balances={"deadbeef": 7.5}, trades=["deadbeef"])
        assert not recon.ok
        assert [t for t, _ in recon.exchange_only] == ["deadbeef"]

    def test_the_db_only_direction_is_untouched(self, wired):
        """An OPEN position whose shares are gone is still a divergence."""
        recon = wired(
            balances={DUST_TOKEN: 0.0},
            trades=[],
            open_positions=[_Pos("RCSS:open", DUST_TOKEN, 5.0)],
        )
        assert not recon.ok
        assert [t for t, _, _ in recon.db_only] == [DUST_TOKEN]


class TestTheDustBoundKeepsItsMargin:
    """
    The old bound failed because it had no margin: 0.01 against a residue
    whose real worst case was 0.02. These assert the new one does.
    """

    def test_the_smallest_position_the_exchange_will_trade_is_not_dust(self):
        worst_case = config.ASSUMED_EXCHANGE_MIN_SHARES * wallet_client.MAX_SHARE_VALUE_USD
        assert worst_case > config.RECONCILE_DUST_MAX_VALUE_USD, (
            "a real unrecorded position must never read as dust"
        )

    def test_the_margin_is_at_least_tenfold(self):
        worst_case = config.ASSUMED_EXCHANGE_MIN_SHARES * wallet_client.MAX_SHARE_VALUE_USD
        assert worst_case >= 10 * config.RECONCILE_DUST_MAX_VALUE_USD, (
            "the 2026-08-30 halt happened because a bound held EXACTLY, with "
            "nothing in reserve for a second source of residue nobody had "
            "thought of. Keep real headroom here."
        )

    @pytest.mark.parametrize("extra_steps", [0, 1, 2, 3])
    def test_any_plausible_exit_residue_is_covered(self, extra_steps):
        """
        Flooring costs under one grid step; each step of fill shortfall costs
        one more. Three spare steps beyond the observed two is still dust.
        """
        grid_step = 10 ** -wallet_client.SHARE_DECIMALS
        residue = grid_step * (1 + extra_steps)
        assert wallet_client._is_dust(residue), (
            f"a residue of {residue} shares should not be able to halt the book"
        )

    def test_the_bound_is_expressed_in_dollars_not_shares(self):
        """
        Regression guard on the shape of the fix. A share count is the wrong
        unit -- it was the wrong unit that produced the halt.
        """
        assert wallet_client._is_dust(0.4) and not wallet_client._is_dust(0.6), (
            "with MAX_SHARE_VALUE_USD = 1.00 the $0.50 bound sits at 0.5 shares"
        )


class TestBackwardCompatibility:
    def test_a_clean_book_still_reconciles_clean(self, wired):
        recon = wired(
            balances={DUST_TOKEN: 5.0},
            trades=[DUST_TOKEN],
            open_positions=[_Pos("RCSS:open", DUST_TOKEN, 5.0)],
        )
        assert recon.ok
        assert recon.dust == []

    def test_dust_on_a_settled_token_is_dust_not_an_unredeemed_holding(self, wired):
        """The settled whitelist and the dust bound must not double-count."""
        recon = wired(
            balances={DUST_TOKEN: 0.008885},
            trades=[DUST_TOKEN],
            settled_tokens={DUST_TOKEN: SettledToken(
                station_icao="RCSS", target_date=date(2026, 8, 30),
                bucket_c=35, side="NO", size_shares=5.0, exit_price=0.0,
            )},
        )
        assert recon.ok
        assert recon.settled_unredeemed == []
        assert [t for t, *_ in recon.dust] == [DUST_TOKEN]
