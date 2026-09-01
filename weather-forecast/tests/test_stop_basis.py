"""
The stop-loss compares like with like.

THE BUG THESE PIN
-----------------
`evaluate_exit()` fired on `position.entry_price - current_price`, but
entry_price is the ASK (market_client.get_entry_price_for_side -- an entry
pays it) and current_price is the BID (get_current_price_for_side -- an open
position marks at it). Each is individually correct; subtracting one from the
other charges the whole bid-ask spread against the stop's budget before the
market has moved at all.

MEASURED on 511 closed positions with a recorded book snapshot within 15
minutes of entry (2026-08-01..09-01): `entry_ask - bid_at_entry` is positive
on 86% of them, median 0.020, max 0.080. The stop distance is
0.30 x min(entry, 1 - entry), i.e. 1.2c-13.8c, so the spread eats a MEDIAN
24% of the budget and 100% or more of it on 7% of positions. 117 of the 207
stop fires that can be scored against settlement (57%) would not fire on a
like-for-like basis; 46 of those 117 were positions that went on to WIN, and
together they carry -$401.74. Two positions stopped 0 seconds after entry
with no market movement at all -- WMKK 2026-08-31 b34 YES @0.15 (spread
0.050 against a stop distance of 0.045) and RKSI 2026-08-25 b29 YES @0.24
(spread 0.080 against 0.072).

WHY THE STOP AND NOT THE TAKE-PROFIT
------------------------------------
The stop is a MOVEMENT filter -- this module's own docstring calls it a
price-noise filter, "worth having only where the noise it filters is smaller
than the move it reacts to". Movement has to be measured on one basis.

The take-profit is not the mirror of that. It cashes a REALIZABLE gain, and
what a sale actually realizes is current_bid - entry_ask: the spread is a
real cost that was really paid. Moving it to a bid-to-bid basis would make it
fire EARLIER by the spread, on a rule already measured as the expensive one
(-$265 book-wide). So it stays on the entry price, and
test_the_take_profit_still_measures_realizable_gain pins that as a decision
rather than an oversight.
"""

from contextlib import contextmanager
from datetime import date

import pytest

import config
import entry_manager
import executor
import risk_manager
import storage
from models import CalibratedEstimate, EntryDecision, EVResult, MarketQuote, Position


# A book wide enough that the spread alone exceeds the stop distance:
# entry 0.15 -> unit 0.15 -> distance 0.045, against a 0.05 spread.
# This is WMKK 2026-08-31 b34 YES, which stopped 0.28s after entry.
ASK = 0.15
BID = 0.10
DISTANCE = 0.30 * 0.15   # config.STOP_LOSS_PCT x risk_unit(0.15)

# Evaluated before config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL so the loose
# thresholds apply, matching tests/test_stop_exempt_high.py.
LOOSE_HOUR = 6


def _position(entry_price: float = ASK, entry_bid=BID, side: str = "YES") -> Position:
    return Position(
        position_id=f"WMKK:2026-08-31:34:{side}:{entry_price}",
        station_icao="WMKK", target_date=date(2026, 8, 31), bucket_c=34,
        side=side, entry_price=entry_price, entry_bid=entry_bid,
        size_usd=10.0, entry_time="2026-08-30T21:01:00+00:00",
        status="open", is_paper=True,
    )


@contextmanager
def _with_lottery_threshold(price: float):
    """
    0.15 is exactly LOTTERY_PRICE_THRESHOLD, and `is_lottery` is a strict
    `<`, so the shipped default leaves this entry armed by one hundredth of
    a cent. Pin the threshold out of the way so these tests measure the
    basis and not that boundary.
    """
    old = config.LOTTERY_PRICE_THRESHOLD
    config.LOTTERY_PRICE_THRESHOLD = price
    try:
        yield
    finally:
        config.LOTTERY_PRICE_THRESHOLD = old


# --------------------------------------------------------------------------
# The stop's basis
# --------------------------------------------------------------------------

def test_the_stop_does_not_fire_on_a_bid_that_has_not_moved():
    """
    The regression case. Bought at the 0.15 ask on a 0.10/0.15 book; the bid
    is still 0.10 one cycle later. Nothing has happened, and an exit here
    books -33% on a position the market has not moved against.
    """
    with _with_lottery_threshold(0.05):
        decision = risk_manager.evaluate_exit(
            _position(), current_price=BID, local_hour=LOOSE_HOUR,
        )

    assert decision.should_exit is False
    assert decision.reason == "hold"


def test_the_stop_fires_when_the_bid_falls_the_full_distance():
    """
    The rule still works -- it is the starting point that moves, not the
    distance. 0.10 - 0.045 = 0.055.
    """
    with _with_lottery_threshold(0.05):
        decision = risk_manager.evaluate_exit(
            _position(), current_price=BID - DISTANCE, local_hour=LOOSE_HOUR,
        )

    assert decision.should_exit is True
    assert decision.reason == "stop_loss"


def test_a_position_with_no_recorded_entry_bid_keeps_the_old_basis():
    """
    Every row written before this column existed reads None, and that is the
    honest value -- the entry-side book was never recorded. Those positions
    keep exactly the behaviour they were opened under rather than silently
    acquiring a spread's worth of extra room.
    """
    with _with_lottery_threshold(0.05):
        decision = risk_manager.evaluate_exit(
            _position(entry_bid=None), current_price=ASK - DISTANCE,
            local_hour=LOOSE_HOUR,
        )

    assert decision.should_exit is True
    assert decision.reason == "stop_loss"


def test_the_risk_unit_still_comes_from_the_price_actually_paid():
    """
    Only the STARTING POINT of the comparison moves onto the bid. The risk
    unit is what the position can lose, which is what it paid -- so the
    distance below a 0.60-ask entry stays 0.30 x (1 - 0.60), not
    0.30 x (1 - 0.58).
    """
    entry_price, entry_bid = 0.60, 0.58
    unit_from_ask = min(entry_price, 1 - entry_price)
    just_inside = entry_bid - 0.30 * unit_from_ask + 0.001

    decision = risk_manager.evaluate_exit(
        _position(entry_price=entry_price, entry_bid=entry_bid, side="NO"),
        current_price=just_inside, local_hour=LOOSE_HOUR,
    )

    assert decision.should_exit is False


def test_the_take_profit_still_measures_realizable_gain():
    """
    Deliberately NOT moved onto the bid basis -- see the module docstring.
    A sale realizes current_bid - entry_ask, so the target is measured from
    the ask. On the bid basis this position would already be done.
    """
    take_distance = config.PROFIT_TAKE_PCT * 0.15
    with _with_lottery_threshold(0.05):
        decision = risk_manager.evaluate_exit(
            _position(), current_price=BID + take_distance, local_hour=LOOSE_HOUR,
        )

    assert decision.should_exit is False


# --------------------------------------------------------------------------
# Getting the entry-side bid onto the position
# --------------------------------------------------------------------------

def test_the_entry_bid_survives_a_storage_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    storage._connect().close()

    storage.open_position(_position())
    (loaded,) = storage.load_open_positions("WMKK")

    assert loaded.entry_bid == pytest.approx(BID)


def test_a_position_opened_without_a_bid_reads_back_none(tmp_path, monkeypatch):
    """
    None means "the book was not recorded", which is not the same claim as
    0.0 -- a real bid of zero.
    """
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    storage._connect().close()

    storage.open_position(_position(entry_bid=None))
    (loaded,) = storage.load_open_positions("WMKK")

    assert loaded.entry_bid is None


def test_the_executor_records_the_entry_bid_on_the_position(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    storage._connect().close()
    monkeypatch.setitem(executor.EXECUTION_MODE, "WMKK", "paper")

    executor.open_position(EntryDecision(
        station_icao="WMKK", target_date=date(2026, 8, 31), bucket_c=34, side="YES",
        kelly_fraction_raw=0.2, kelly_fraction_applied=0.05,
        recommended_size_usd=10.0, available_depth_usd=200.0,
        slippage_at_size_pct=0.01, net_ev_at_size=0.08,
        approved=True, reason="test", station_maturity="exploratory",
        entry_price=ASK, entry_bid=BID, token_id="tok-1",
        model_prob=0.34, raw_edge=0.19,
    ))

    (stored,) = storage.load_open_positions("WMKK")
    assert stored.entry_bid == pytest.approx(BID)


def test_the_entry_decision_carries_the_bid_from_the_ev_result():
    result = EVResult(
        station_icao="WMKK", target_date=date(2026, 8, 31), bucket_c=34, side="YES",
        model_prob=0.34, market_price=ASK, market_bid=BID, raw_edge=0.19,
        estimated_slippage_pct=0.01, fee_rate_pct=0.01, net_ev_per_dollar=0.5,
    )

    decision = entry_manager.evaluate_entry(result, token_id="tok-1")

    assert decision.entry_bid == pytest.approx(BID)


def test_the_ev_table_carries_each_sides_own_bid(monkeypatch):
    """
    Both bids are already fetched every cycle for save_market_snapshot().
    Carrying them onto the EVResult costs no extra call -- and each side must
    keep its OWN bid: NO is independently quoted, never 1 - the YES bid.
    """
    import ev_engine

    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.01)

    estimate = CalibratedEstimate(
        station_icao="WMKK", target_date=date(2026, 8, 31),
        central_estimate_c=34.0, std_dev_c=1.0,
        monsoon_phase="inter_monsoon", spread_source="test",
    )
    results = ev_engine.compute_ev_table(
        estimate,
        {34: {"yes_token_id": "y", "no_token_id": "n"}},
        quotes={34: MarketQuote(
            bucket_c=34, yes_price=0.15, no_price=0.88,
            fetched_at="2026-08-30T21:01:00+00:00", yes_bid=0.10, no_bid=0.83,
        )},
        model_probs={34: 0.34},
    )

    by_side = {r.side: r for r in results}
    assert by_side["YES"].market_bid == pytest.approx(0.10)
    assert by_side["NO"].market_bid == pytest.approx(0.83)


# --------------------------------------------------------------------------
# Backtest parity
# --------------------------------------------------------------------------

def test_a_replayed_position_carries_an_entry_bid_too(synthetic_scenario, quiet_run):
    """
    backtest/engine.py drives the REAL risk_manager.evaluate_exit(), so a
    replayed Position with entry_bid=None would silently keep the old basis
    while the live path used the new one -- the backtest and the paper book
    disagreeing by a spread, which is the exact class of divergence
    Position.model_prob was added to expose.

    NOTE what this fixture can and cannot show: its snapshots are bid-only
    (4-tuples, ask_price NULL), so _PriceReader.entry_price() falls back to
    the bid and entry_bid equals entry_price here. What is pinned is that the
    field is POPULATED from the recorded book rather than left None. The
    ask/bid distinction itself is pinned on the live path above.
    """
    run = quiet_run(synthetic_scenario)

    positions = list(run.closed_positions)
    assert positions, "scenario opened no positions -- nothing to check"
    for position in positions:
        assert position.entry_bid is not None, (
            f"{position.position_id} replayed without an entry-side bid"
        )
