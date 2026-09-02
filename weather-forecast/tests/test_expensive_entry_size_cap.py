"""
tests/test_expensive_entry_size_cap.py

config.max_position_usd(): a LOWER per-trade ceiling for expensive entries.

WHY IT EXISTS. With config.HOLD_TO_SETTLEMENT_MODES on, a paper position has
no stop, so its maximum loss is the whole stake. The dollars are concentrated
exactly where that hurts most: mean position size runs $2.89 in the 0.00-0.15
band and $17.55 at 0.55+, because Kelly sizes up as the edge gets cheaper to
express. Six of the seven losers at size >= $20 and entry >= 0.55 lost their
full stake.

WHY A CEILING AND NOT A PRICE RULE. A price-triggered loss cap was measured
first and REJECTED: swept over 80 (cap, size, price) cells against the 513
closed positions with a recorded settlement, its sign depends entirely on the
fill assumption -- +$146 if a cap always fills at its trigger, -$75 if it
fills at the lowest quote the record can prove existed (76 of 80 cells beat
holding on the first assumption, 45 of 80 on the second). The regime that
triggers a loss cap IS the gapping regime, which is when the optimistic
assumption fails: WMKK 2026-08-07 b35 NO @0.750 had its stop at 0.675 and
filled at 0.060. The price history cannot settle it either -- the snapshot
series covers a median 25% of each position's hold window, and 365 of 514
positions have under half.

A ceiling needs no price path and no fill: it acts at entry, and gapping
cannot defeat it.

WHAT IT COSTS. The cohort it caps is PROFITABLE held (+$49 over the 27
positions at size >= $20 and entry >= 0.55; +$147 at entry >= 0.50), so this
buys a smaller worst case at a cost in expectation. That is the trade it was
chosen for, not a measured improvement, and no test here should claim
otherwise.

The shipped value is NOT free on the record: three positions exceed it, all
WSSS in the first week of August, and capping them costs -$33.14. config.py
lists them. It bounds the future -- MAX_POSITION_USD permits $150 -- and the
cost is the price of that bound, not an oversight.
"""

import config
import entry_manager
from backtest import entry_sim


# The values the cap shipped with, used explicitly rather than read from
# config, so a retune cannot silently turn these into tautologies.
SHIPPED_PRICE = 0.55
SHIPPED_CEILING = 30.0


# --- the helper ----------------------------------------------------------

def test_an_expensive_entry_gets_the_lower_ceiling():
    assert config.max_position_usd(0.60) == config.MAX_POSITION_USD_EXPENSIVE


def test_a_cheap_entry_keeps_the_full_ceiling():
    assert config.max_position_usd(0.20) == config.MAX_POSITION_USD


def test_the_boundary_is_inclusive_and_one_cent_below_is_not():
    assert config.max_position_usd(SHIPPED_PRICE) == config.MAX_POSITION_USD_EXPENSIVE
    assert config.max_position_usd(SHIPPED_PRICE - 0.01) == config.MAX_POSITION_USD


def test_no_price_falls_back_to_the_full_ceiling():
    """
    manual_trigger and any future caller without a quote. Falling back to the
    LOWER ceiling would silently shrink every such trade; falling back to the
    full one leaves behaviour exactly as it was before this existed.
    """
    assert config.max_position_usd(None) == config.MAX_POSITION_USD


def test_the_expensive_ceiling_is_never_the_looser_of_the_two():
    """A retune that inverted these would raise the cap where risk is worst."""
    assert config.MAX_POSITION_USD_EXPENSIVE <= config.MAX_POSITION_USD


# --- the shipped defaults ------------------------------------------------

def test_shipped_defaults():
    assert config.EXPENSIVE_ENTRY_PRICE == SHIPPED_PRICE
    assert config.MAX_POSITION_USD_EXPENSIVE == SHIPPED_CEILING


def test_the_cap_is_not_keyed_on_execution_mode():
    """
    Deliberately global, unlike HOLD_TO_SETTLEMENT_MODES. Gapping defeats the
    stop as well as the absence of one -- WMKK's 30% stop filled at -92% --
    so the live book is exposed to the same tail and the ceiling only ever
    reduces size. The helper takes a price and nothing else; this test pins
    that it cannot grow a mode argument without someone noticing.
    """
    import inspect
    params = list(inspect.signature(config.max_position_usd).parameters)
    assert params == ["entry_price"]


# --- both sizing paths use it -------------------------------------------

def test_live_sizing_reads_the_helper(monkeypatch):
    seen = []

    def fake(entry_price=None):
        seen.append(entry_price)
        return 7.0

    monkeypatch.setattr(config, "max_position_usd", fake)
    src = entry_manager.evaluate_entry.__code__
    assert "max_position_usd" in src.co_names, (
        "entry_manager.evaluate_entry must size through config.max_position_usd"
    )


def test_replay_sizing_reads_the_helper():
    """
    Parity. backtest/entry_sim.py applies the SAME ceiling, or a replay
    silently sizes expensive entries larger than live would and every sweep
    reports P&L the live book could not have made.
    """
    assert "max_position_usd" in entry_sim.evaluate_entry_sim.__code__.co_names
