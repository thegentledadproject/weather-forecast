"""
tests/test_entry_bar_basis.py

WHAT THIS IS FOR. The entry admission bar is keyed on net_ev_per_dollar --
(raw_edge / price) - costs -- and 9367907 measured that key as INVERTED on
the live book: the highest quintile returns -31.7% held to settlement while
the second-lowest returns +30.6%, and the top quintile's mean price is 0.114.
That commit fixed the SORT and deliberately left the BAR alone, because
changing which candidates surface is a trading change and this project's rule
is that entry-gate changes need replay evidence first.

config.ENTRY_BAR_BASIS is the switch that lets the replay produce that
evidence. It ships "ratio" -- today's behaviour, byte for byte -- and
backtest/entry_bar_sweep.py drives it to "per_share" to score the alternative.

WHY A SWITCH IN LIVE CODE RATHER THAN A PATCH IN THE SWEEP. The bar is
applied TWICE, not once: ev_engine.best_opportunities() screens the table,
and then entry_manager.evaluate_entry() re-checks net EV at the ACTUAL size
once real slippage is known (backtest/entry_sim.evaluate_entry_sim() is its
replica). A sweep that patched only the screen would measure a rule that
rejects at the second gate what it admitted at the first -- which is not a
rule anyone would ship. Both gates read one helper so they cannot drift, and
test_parity_entry.py already fails if the live gate and its replica disagree.

THE ASYMMETRY THIS IS TESTING FOR, stated so the two behavioural cases below
are not read as arbitrary. A ratio bar demands a cost proportional to price:
0.15 asks 0.6c per share at price 0.04 and 10.5c at price 0.70. So it is
nearly free at the cheap end -- where MIN_ABS_RAW_EDGE, not this bar, is
doing the work -- and binds hardest on expensive buckets, which is the
opposite of where the measured inversion says the bad entries are.
"""

from datetime import date

import pytest

import config
import entry_manager
import ev_engine
from backtest import entry_sim
from models import EVResult


RATIO_BAR = 0.15
PER_SHARE_BAR = 0.045   # ~what a 0.15 ratio demands at the median entry price


def _cand(price, model_prob, slippage=0.0, fee=0.0, bucket=30, side="YES"):
    """An EVResult with net_ev_per_dollar computed exactly as ev_engine does."""
    raw_edge = model_prob - price
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 9, 7), bucket_c=bucket,
        side=side, model_prob=model_prob, market_price=price,
        raw_edge=raw_edge, estimated_slippage_pct=slippage, fee_rate_pct=fee,
        net_ev_per_dollar=(raw_edge / price) - slippage - fee,
    )


# A 1c edge on a 4c ticket. Ratio 0.25 clears a 0.15 bar comfortably; the
# same candidate is worth one cent a share.
CHEAP = _cand(price=0.04, model_prob=0.05)

# An 8c edge on a 70c ticket. Ratio 0.114 MISSES a 0.15 bar; the same
# candidate is worth eight cents a share, eight times the cheap one.
EXPENSIVE = _cand(price=0.70, model_prob=0.78)


# --- the switch ships inert ---------------------------------------------

def test_shipped_default_is_the_ratio_basis():
    """
    The whole point of the switch is that arming it is a separate, evidenced
    decision. If this ever ships as "per_share" without that evidence, the
    trading change happened by import.
    """
    assert config.ENTRY_BAR_BASIS == "ratio"


def test_an_unrecognised_basis_raises_rather_than_admitting_everything():
    """
    A typo must not fail open. The failure mode being excluded is a bar that
    silently stops rejecting anything, which looks like a working daemon.
    """
    with pytest.raises(ValueError):
        config.clears_entry_bar(0.20, 0.30, RATIO_BAR, basis="ratioo")


# --- the two bases disagree, in both directions -------------------------

def test_per_share_basis_rejects_the_cheap_ticket_the_ratio_admits():
    """1c of edge on a 4c ticket: ratio 0.25 clears 0.15, per-share 0.01 does not."""
    assert config.clears_entry_bar(
        CHEAP.net_ev_per_dollar, CHEAP.market_price, RATIO_BAR, basis="ratio")
    assert not config.clears_entry_bar(
        CHEAP.net_ev_per_dollar, CHEAP.market_price, PER_SHARE_BAR, basis="per_share")


def test_per_share_basis_admits_the_expensive_ticket_the_ratio_rejects():
    """
    8c of edge on a 70c ticket: ratio 0.114 misses 0.15, per-share 0.08
    clears 0.045. This is the band the ratio bar screens out today.
    """
    assert not config.clears_entry_bar(
        EXPENSIVE.net_ev_per_dollar, EXPENSIVE.market_price, RATIO_BAR, basis="ratio")
    assert config.clears_entry_bar(
        EXPENSIVE.net_ev_per_dollar, EXPENSIVE.market_price, PER_SHARE_BAR, basis="per_share")


def test_per_share_is_the_ratio_multiplied_back_out():
    """Not a new cost model -- the same quantity undivided, as net_ev_per_share()."""
    c = _cand(price=0.40, model_prob=0.50, slippage=0.01, fee=0.02)
    on_the_nose = ev_engine.net_ev_per_share(c)
    assert config.clears_entry_bar(
        c.net_ev_per_dollar, c.market_price, on_the_nose - 1e-9, basis="per_share")
    assert not config.clears_entry_bar(
        c.net_ev_per_dollar, c.market_price, on_the_nose + 1e-9, basis="per_share")


# --- gate 1: the screen --------------------------------------------------

def test_screen_admits_the_expensive_candidate_under_the_per_share_basis(monkeypatch):
    """best_opportunities() is the first of the two gates."""
    rows = [CHEAP, EXPENSIVE]

    monkeypatch.setattr(config, "ENTRY_BAR_BASIS", "ratio")
    assert [r.market_price for r in
            ev_engine.best_opportunities(rows, min_net_ev=RATIO_BAR)] == [0.04]

    monkeypatch.setattr(config, "ENTRY_BAR_BASIS", "per_share")
    assert [r.market_price for r in
            ev_engine.best_opportunities(rows, min_net_ev=PER_SHARE_BAR)] == [0.70]


# --- gate 2: the re-check at actual size ---------------------------------

def _sized(ev, min_net_ev):
    """evaluate_entry_sim with every earlier gate passing, so gate 11 decides."""
    return entry_sim.evaluate_entry_sim(
        ev=ev, token_id="TOKEN-1", open_count_for_bucket=0,
        opposite_count_for_bucket=0, stop_outs_for_bucket=0,
        depth_usd=500.0, slippage_fn=lambda size_usd: 0.0,
        min_net_ev=min_net_ev, sizing_bankroll=config.BANKROLL_USD,
        station_maturity="mature",
    )


def test_size_time_recheck_honours_the_basis(monkeypatch):
    """
    The gate that would otherwise undo the screen. Under "per_share" the
    expensive candidate must survive BOTH gates, or the sweep is scoring a
    rule that admits at the screen and rejects at sizing.
    """
    monkeypatch.setattr(config, "ENTRY_BAR_BASIS", "per_share")
    decision = _sized(EXPENSIVE, PER_SHARE_BAR)
    assert decision.approved, decision.reason


def test_size_time_recheck_still_rejects_under_the_shipped_basis(monkeypatch):
    """The same candidate, the shipped basis: rejected at gate 11 as today."""
    monkeypatch.setattr(config, "ENTRY_BAR_BASIS", "ratio")
    decision = _sized(EXPENSIVE, RATIO_BAR)
    assert not decision.approved
    assert "no longer clears" in decision.reason


# --- the two implementations cannot drift apart --------------------------

def test_live_gate_and_its_replica_both_read_the_helper():
    """
    Parity by construction. test_parity_entry.py compares outputs; this
    fails faster and says why -- one of the two gates was left on the old
    arithmetic, so a replay would score a rule live does not run.
    """
    assert "clears_entry_bar" in entry_manager.evaluate_entry.__code__.co_names
    assert "clears_entry_bar" in entry_sim.evaluate_entry_sim.__code__.co_names
    assert "clears_entry_bar" in ev_engine.best_opportunities.__code__.co_names


# --- gate 3: the executor's re-check at the RESOLVED size ----------------
#
# Found while wiring the other two, and it is the one that matters most: this
# gate re-tests net EV after the exchange minimum has upsized the order (P1-1's
# 55%-of-entries path), and it compares a RATIO against decision.min_net_ev.
# Handed a per-share bar it does not merely disagree with the other gates -- it
# FAILS OPEN, because ratio values (0.1-1.0) sit above per-share bars
# (0.02-0.06) almost by construction. A gate that stops refusing anything looks
# exactly like a working daemon.

def test_executor_resize_gate_honours_the_basis(monkeypatch):
    """
    Ratio 0.10 at price 0.40 is 4c per share, under a 4.5c bar -- must refuse.
    Under the ratio basis the same numbers are 0.10 against 0.045 and pass, so
    this is the basis deciding, not the arithmetic.
    """
    import executor
    from clients import market_client, wallet_client
    from models import EntryDecision

    decision = EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 7), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=1.0, available_depth_usd=1000.0,
        slippage_at_size_pct=0.01, net_ev_at_size=0.10,
        approved=True, reason="test", station_maturity="mature",
        entry_price=0.40, token_id="TOK", min_net_ev=PER_SHARE_BAR,
    )
    spec = wallet_client.OrderSpec(ok=True, token_id="TOK", side="BUY", limit_price=0.40,
                     size_shares=10.0, notional_usd=3.75, expected_price=0.40)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)

    monkeypatch.setattr(config, "ENTRY_BAR_BASIS", "per_share")
    ok, note = executor._resolved_size_ok(spec, decision)
    assert not ok, f"per-share basis must refuse 4c/share against a 4.5c bar: {note}"
    assert "net EV falls" in note

    monkeypatch.setattr(config, "ENTRY_BAR_BASIS", "ratio")
    ok, _ = executor._resolved_size_ok(spec, decision)
    assert ok, "ratio basis: 0.10 clears a 0.045 bar, as today"


def test_executor_resize_gate_reads_the_helper():
    import executor
    assert "clears_entry_bar" in executor._resolved_size_ok.__code__.co_names
