"""
tests/test_exit_fee_in_ev.py

P1-8(a) · the EV side of the fee asymmetry. Live, no migration.

THE DEFECT. compute_ev_table() subtracts ONE taker fee -- the entry leg's. On
any book that SELLS, a second taker fee is due on the way out. Polymarket
charges 0.05 x (1 - p) of notional per leg, so a round trip on a 0.30 entry
taken to its profit target costs roughly 4 points of the entry notional in exit
fee alone, against a net-EV bar that is often 15. The entry gate has been
approving trades on a number that omits a cost the trade will certainly pay.

WHICH BOOKS PAY IT. Exactly those not in config.HOLD_TO_SETTLEMENT_MODES. A
position held to par pays NOTHING on the way out -- redeeming a resolved token
is not a trade, as risk_manager.taker_fee_per_share records -- so the paper
book, which now holds to settlement, is correctly charged one fee and not two.
Live and simulation sell, and pay two.

p_exit IS AN ESTIMATOR, NOT A PREDICTION, and the docstring has to say so. The
take-profit target is used because it is the exit the position is aiming at; a
stop-out exits lower and pays less fee, and a settlement close pays none. So
this is the fee on the intended outcome, which is the right one to charge
against an expected value that also assumes the intended outcome.

WHAT ELSE THIS FILE GUARDS. entry_manager re-derives net EV at the resolved
size from raw_edge, slippage and fee_rate_pct. A new cost term that is not
added there too would be silently dropped at exactly the layer that approves
the order -- the same defect shape as P1-2's unpriced limit pad. There is a
test for that below, and it is the one most likely to catch a future
regression.
"""
from datetime import date

import pytest

import config
import ev_engine
from models import CalibratedEstimate, EVResult, MarketQuote

STATION = "WSSS"
TARGET = date(2026, 9, 3)


def _estimate(central=32.0, sd=1.0):
    return CalibratedEstimate(
        station_icao=STATION,
        target_date=TARGET,
        central_estimate_c=central,
        std_dev_c=sd,
        monsoon_phase="southwest",
        spread_source="measured_error",
    )


def _token_map(buckets=(32,)):
    return {
        b: {"yes_token_id": f"tok-{b}-yes", "no_token_id": f"tok-{b}-no"}
        for b in buckets
    }


def _quotes(price=0.30, buckets=(32,)):
    return {
        b: MarketQuote(
            bucket_c=b, yes_price=price, no_price=round(1.0 - price, 4),
        )
        for b in buckets
    }


@pytest.fixture(autouse=True)
def _no_book_walk(monkeypatch):
    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.0)
    # entry_manager re-reads depth and slippage from its own import of the
    # client, and reaches storage for the per-bucket cap. Pinned so the at-size
    # test below differs from the EV table in the exit fee and nothing else.
    import entry_manager
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)


def _yes_row(rows):
    return next(r for r in rows if r.side == "YES" and r.bucket_c == 32)


def _table(execution_mode="__unset__", price=0.30):
    kwargs = {} if execution_mode == "__unset__" else {"execution_mode": execution_mode}
    return ev_engine.compute_ev_table(
        _estimate(), _token_map(), quotes=_quotes(price=price), **kwargs
    )


# ---------------------------------------------------------------------------
# The estimator itself
# ---------------------------------------------------------------------------

def test_the_expected_exit_fee_uses_the_take_profit_target_as_p_exit():
    """
    Entry 0.30, risk unit min(0.30, 0.70) = 0.30, PROFIT_TAKE_PCT 0.50, so the
    target is 0.45. Fee there is 0.05 x 0.55 x 0.45 = $0.012375 per share, and
    a dollar of entry notional buys 1/0.30 shares, so 4.125% of notional.
    """
    assert ev_engine.expected_exit_fee_pct_of_notional(0.30) == pytest.approx(0.04125, abs=1e-6)


def test_the_exit_fee_shrinks_as_the_target_approaches_par():
    """
    0.05 x (1 - p) falls toward zero at par, which is why an expensive entry
    pays little exit fee: there is almost nothing left to sell into.
    """
    cheap = ev_engine.expected_exit_fee_pct_of_notional(0.20)
    dear = ev_engine.expected_exit_fee_pct_of_notional(0.80)
    assert dear < cheap


def test_the_exit_fee_is_zero_for_a_missing_or_nonsense_price():
    """
    Mirrors taker_fee_pct_of_notional's stance: those candidates carry
    net_ev_per_dollar=None and are never sized, so the fee is informational.
    """
    assert ev_engine.expected_exit_fee_pct_of_notional(None) == 0.0
    assert ev_engine.expected_exit_fee_pct_of_notional(0.0) == 0.0


# ---------------------------------------------------------------------------
# The acceptance case
# ---------------------------------------------------------------------------

def test_a_selling_book_gets_a_lower_net_ev_than_a_holding_book():
    """THE ACCEPTANCE CASE, on otherwise identical candidates."""
    selling = _yes_row(_table(execution_mode="live"))
    holding = _yes_row(_table(execution_mode="paper"))

    assert selling.net_ev_per_dollar < holding.net_ev_per_dollar


def test_the_difference_is_exactly_the_expected_exit_fee():
    selling = _yes_row(_table(execution_mode="live"))
    holding = _yes_row(_table(execution_mode="paper"))

    assert holding.net_ev_per_dollar - selling.net_ev_per_dollar == pytest.approx(
        ev_engine.expected_exit_fee_pct_of_notional(selling.market_price), abs=1e-9
    )


def test_a_hold_to_settlement_mode_is_charged_no_exit_fee():
    """
    The paper book holds to settlement, and a position held to par pays nothing
    on the way out -- redeeming a resolved token is not a trade.
    """
    row = _yes_row(_table(execution_mode="paper"))
    assert row.expected_exit_fee_pct == 0.0


def test_simulation_is_charged_the_exit_fee():
    """
    Simulation exists to rehearse live decisions faithfully, so it must see the
    same costs live sees. config.HOLD_TO_SETTLEMENT_MODES deliberately leaves it
    out for the same reason.
    """
    assert _yes_row(_table(execution_mode="simulation")).expected_exit_fee_pct > 0


def test_the_gate_reads_the_config_set_rather_than_naming_modes(monkeypatch):
    """
    One definition of "this book holds". A second list of mode strings here
    would drift from config.HOLD_TO_SETTLEMENT_MODES the first time that set
    changed -- and it changed on 2026-09-02.
    """
    monkeypatch.setattr(config, "HOLD_TO_SETTLEMENT_MODES", ("live",))

    assert _yes_row(_table(execution_mode="live")).expected_exit_fee_pct == 0.0
    assert _yes_row(_table(execution_mode="paper")).expected_exit_fee_pct > 0


# ---------------------------------------------------------------------------
# An unknown mode changes nothing
# ---------------------------------------------------------------------------

def test_no_declared_mode_charges_no_exit_fee():
    """
    The dashboard EV card and the backtest call this without a mode. Charging
    them a fee for a sale that may never happen would change published numbers
    for callers that cannot know the answer, so the historical figure stands
    and the field reads zero.
    """
    row = _yes_row(_table())
    assert row.expected_exit_fee_pct == 0.0


def test_a_flat_fee_override_still_suppresses_the_schedule():
    """
    fee_rate_pct is a flat override for the backtest's fee-parity runs. It
    overrides the ENTRY schedule; an exit fee silently added on top would
    break the parity the override exists to establish.
    """
    rows = ev_engine.compute_ev_table(
        _estimate(), _token_map(), quotes=_quotes(), fee_rate_pct=0.0,
        execution_mode="live",
    )
    assert _yes_row(rows).expected_exit_fee_pct == 0.0


# ---------------------------------------------------------------------------
# The at-size re-derivation must not drop it
# ---------------------------------------------------------------------------

def test_the_at_size_net_ev_derivation_subtracts_the_exit_fee():
    """
    THE REGRESSION MOST WORTH CATCHING. entry_manager re-derives net EV at the
    resolved size from raw_edge, slippage and the fee terms. A cost added to
    compute_ev_table but not there is dropped at exactly the layer that
    approves the order -- which is the defect P1-2 had just finished fixing for
    the limit pad.
    """
    import entry_manager

    selling = _yes_row(_table(execution_mode="live"))
    holding = _yes_row(_table(execution_mode="paper"))
    assert selling.expected_exit_fee_pct > 0, "fixture: the selling row must carry a fee"

    # Same book, same size, same slippage -- the ONLY difference between the two
    # EVResults is the exit fee, so any difference in the at-size number is it.
    def _at_size(row):
        decision = entry_manager.evaluate_entry(row, token_id="tok", min_net_ev=-9.0)
        return decision.net_ev_at_size

    selling_at_size = _at_size(selling)
    holding_at_size = _at_size(holding)

    assert selling_at_size is not None and holding_at_size is not None
    assert holding_at_size - selling_at_size == pytest.approx(
        selling.expected_exit_fee_pct, abs=1e-9
    ), (
        "the at-size re-derivation must subtract the exit fee too, or the "
        "approval runs on a number the EV table already rejected"
    )
