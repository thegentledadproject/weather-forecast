"""
tests/test_reconciliation_units.py

The 2026-08-15 reconciliation-units bug, pinned from both directions.

WHAT HAPPENED. _held_shares() returned the CLOB balance verbatim. That
balance is raw on-chain base units (6 decimals), so every reading was
1e6 too large:

  * exchange_only, loudly: 0.008885 shares of dust left behind by a
    floored exit read as "8885.00 sh held but NOT RECORDED". Live entries
    fail closed on a reconciliation mismatch, so WSSS took none of the
    candidates it cleared through every cycle of the 2026-08-15 window.
  * db_only, silently and worse: the test is
    `held + RECONCILE_SHARE_TOLERANCE < expected`. A real 5.14-share
    position read as ~5_140_000, so it could never be true -- the check
    that catches "the database thinks we hold shares that are gone" had
    never been able to fire.

The dust itself was NOT the bug and is not fixed: build_exit_order()
floors the sell size onto the share grid on purpose, because selling more
shares than are held fails outright. The dust is bounded below one grid
step, and the tolerance is exactly that bound -- which is the invariant
the last test here defends, since the two constants live in different
modules and currently agree with no margin at all.
"""
import pytest

import config
from clients import wallet_client


class TestBalanceScaling:
    def test_base_units_are_converted_to_shares(self, monkeypatch):
        """The exact number that blocked the live track."""
        monkeypatch.setattr(wallet_client, "_clob", lambda: _FakeLib())
        client = _FakeClient(balance="8885")
        assert wallet_client._held_shares(client, "TOK") == pytest.approx(0.008885)

    def test_a_real_position_reads_back_as_its_share_count(self, monkeypatch):
        """5.138885 shares on the wire is 5138885 base units."""
        monkeypatch.setattr(wallet_client, "_clob", lambda: _FakeLib())
        client = _FakeClient(balance="5138885")
        assert wallet_client._held_shares(client, "TOK") == pytest.approx(5.138885)

    def test_zero_stays_zero(self, monkeypatch):
        monkeypatch.setattr(wallet_client, "_clob", lambda: _FakeLib())
        assert wallet_client._held_shares(_FakeClient(balance="0"), "TOK") == 0.0

    def test_an_unreadable_balance_is_none_not_zero(self, monkeypatch):
        """None must never be mistaken for 'holds nothing' -- it fails closed."""
        monkeypatch.setattr(wallet_client, "_clob", lambda: _FakeLib())
        assert wallet_client._held_shares(_FakeClient(balance="not-a-number"), "TOK") is None

    def test_a_failed_read_is_none_not_zero(self, monkeypatch):
        monkeypatch.setattr(wallet_client, "_clob", lambda: _FakeLib())
        assert wallet_client._held_shares(_FakeClient(raises=True), "TOK") is None


class TestBothDirectionsCanNowFire:
    """
    Reconciliation compares held against expected. These pin the two
    comparisons at the scale they will actually see, so a regression to raw
    units fails here rather than in production.
    """

    def test_exit_dust_does_not_look_like_a_holding(self):
        held = 8885 / wallet_client.BALANCE_BASE_UNITS
        assert not held > config.RECONCILE_SHARE_TOLERANCE, (
            "dust from a floored exit must not trip exchange_only -- this is the "
            "comparison that blocked every live entry on 2026-08-15"
        )

    def test_a_genuine_unrecorded_holding_still_trips(self):
        """The check must keep working for a real position."""
        held = 5_138_885 / wallet_client.BALANCE_BASE_UNITS
        assert held > config.RECONCILE_SHARE_TOLERANCE

    def test_db_only_can_fire_at_all(self):
        """
        The silent half. With raw units this condition was unreachable for
        any real position; at share scale a sold-out position must trip it.
        """
        expected = 5.138885
        held = 0.0
        assert held + config.RECONCILE_SHARE_TOLERANCE < expected

    def test_db_only_does_not_fire_on_a_matching_position(self):
        expected = 5.138885
        held = 5_138_885 / wallet_client.BALANCE_BASE_UNITS
        assert not held + config.RECONCILE_SHARE_TOLERANCE < expected


class TestDustBoundInvariant:
    """
    SUPERSEDED FOR THE EXCHANGE_ONLY DIRECTION, 2026-08-30. These pin the
    flooring arithmetic, which is still correct and still worth pinning --
    but flooring turned out not to be the only source of exit residue, so
    it was never the bound that mattered. The exchange can fill under the
    size submitted, and one extra grid step of shortfall was enough to halt
    the live book against a tolerance set to exactly one step.

    The bound that guards exchange_only now lives in
    tests/test_exit_dust_reconciliation.py and is denominated in dollars of
    worst-case value, with a 10x margin instead of none.
    """

    def test_tolerance_covers_one_full_share_grid_step(self):
        """
        Still true, and still asserted because the two constants live in
        different modules -- but no longer load-bearing on its own.
        """
        grid_step = 10 ** -wallet_client.SHARE_DECIMALS
        assert config.RECONCILE_SHARE_TOLERANCE >= grid_step

    @pytest.mark.parametrize("shares", [5.138885, 0.999999, 12.345678, 3.010001])
    def test_flooring_alone_costs_less_than_one_grid_step(self, shares):
        """Property check over the actual flooring build_exit_order performs."""
        import math

        factor = 10 ** wallet_client.SHARE_DECIMALS
        sold = math.floor(shares * factor + 1e-9) / factor
        residue = shares - sold
        assert 0 <= residue < config.RECONCILE_SHARE_TOLERANCE
        assert sold <= shares, "an exit must never try to sell more than is held"

    @pytest.mark.parametrize("shares", [5.138885, 0.999999, 12.345678, 3.010001])
    def test_but_a_short_fill_on_top_of_it_can_exceed_that(self, shares):
        """
        The 2026-08-30 case, generalised: flooring plus one grid step of
        fill shortfall breaks the old bound and must still read as dust.
        """
        import math

        factor = 10 ** wallet_client.SHARE_DECIMALS
        submitted = math.floor(shares * factor + 1e-9) / factor
        filled = submitted - 10 ** -wallet_client.SHARE_DECIMALS
        residue = shares - filled
        assert residue > config.RECONCILE_SHARE_TOLERANCE
        assert wallet_client._is_dust(residue)


class _FakeLib:
    class BalanceAllowanceParams:
        def __init__(self, asset_type=None, token_id=None):
            self.asset_type, self.token_id = asset_type, token_id

    class AssetType:
        CONDITIONAL = "CONDITIONAL"


class _FakeClient:
    def __init__(self, balance=None, raises=False):
        self._balance, self._raises = balance, raises

    def get_balance_allowance(self, params):
        if self._raises:
            raise RuntimeError("balance read failed")
        return {"balance": self._balance}
