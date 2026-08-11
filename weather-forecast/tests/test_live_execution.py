"""
The real-money execution path: the mode ladder, the fixed $1 trade size,
and every place the code is supposed to refuse rather than guess.

The bias of this file is deliberate and one-directional. Almost every
assertion here is that something did NOT happen -- no order submitted, no
position written, no close recorded. Those are the failures that cost real
money or strand a real position; a mode that is too cautious costs a
missed trade, which is recoverable.
"""

from datetime import date

import pytest

import config
import entry_manager
import executor
import storage
from clients import market_client, wallet_client
from models import EntryDecision, ExitDecision, EVResult, Position


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def stub_book(monkeypatch, tick="0.01", min_order_size=None):
    """Pin the public-book constraints wallet_client reads."""
    monkeypatch.setattr(
        wallet_client, "_book_constraints", lambda token_id: (tick, min_order_size)
    )


def make_decision(size_usd=1.0, price=0.30, station="WSSS", approved=True, token_id="TOK"):
    return EntryDecision(
        station_icao=station, target_date=date(2026, 8, 10), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=size_usd, available_depth_usd=1000.0,
        slippage_at_size_pct=0.01, net_ev_at_size=0.30,
        approved=approved, reason="test", station_maturity="mature",
        entry_price=price, token_id=token_id,
    )


def make_position(station="WSSS", size_shares=3.33, price=0.30, mode="live", token_id="TOK"):
    return Position(
        position_id="p1", station_icao=station, target_date=date(2026, 8, 10),
        bucket_c=32, side="YES", entry_price=price, size_usd=1.0,
        entry_time="2026-08-10T00:00:00+00:00", status="open", token_id=token_id,
        is_paper=(mode != "live"), size_shares=size_shares, execution_mode=mode,
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture what would be written to storage instead of writing it."""
    opened, closed = [], []
    monkeypatch.setattr(storage, "open_position", lambda p: opened.append(p))
    monkeypatch.setattr(
        storage, "close_position",
        lambda **kw: closed.append(kw),
    )
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])
    return {"opened": opened, "closed": closed}


@pytest.fixture
def reconciled(monkeypatch):
    """
    Exchange agrees with the database, and the size re-check stays offline.

    Both are now on the live entry path, and both reach the network if left
    alone -- reconciliation needs credentials (correctly failing closed
    without them) and the resolved-size re-check calls estimate_slippage().
    Tests about what gets RECORDED after a fill should not also be testing
    either of those; they have their own tests.
    """
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions: wallet_client.Reconciliation(ok=True, checked=True, reason="stubbed"),
    )
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)


@pytest.fixture
def mode(monkeypatch):
    """Set every station's execution mode for one test."""
    def _set(station_mode, station="WSSS"):
        monkeypatch.setattr(
            executor, "EXECUTION_MODE",
            {icao: ("manual_review" if icao != station else station_mode)
             for icao in config.STATIONS},
        )
    return _set


# --------------------------------------------------------------------------
# The allowlist / maturity conjunction
# --------------------------------------------------------------------------

def test_live_size_cap_requires_allowlist_and_maturity():
    assert config.live_size_cap_usd("WSSS", "live") == config.LIVE_TRADE_SIZE_USD
    assert config.live_size_cap_usd("WSSS", "simulation") == config.LIVE_TRADE_SIZE_USD
    # Not allowlisted -> no cap, and not permitted at all.
    assert config.live_size_cap_usd("WMKK", "live") is None
    assert config.live_mode_is_permitted("WMKK", "live") is False
    # Paper/manual_review are unaffected -- normal Kelly sizing.
    assert config.live_size_cap_usd("WSSS", "paper") is None


def test_allowlisting_an_immature_station_does_nothing(monkeypatch):
    """
    The conjunction has to hold from BOTH sides: adding a station to the
    allowlist without promoting its maturity must not enable it.
    """
    monkeypatch.setattr(config, "LIVE_TRADING_STATIONS", {"WSSS", "WMKK"})
    assert config.live_size_cap_usd("WMKK", "live") is None
    assert config.live_mode_is_permitted("WMKK", "live") is False


def test_every_allowlisted_station_is_mature():
    """Guards the shipped config itself, not just the helper."""
    for icao in config.LIVE_TRADING_STATIONS:
        assert config.STATION_MATURITY.get(icao) == "mature", (
            f"{icao} is allowlisted for real money but is not a mature station"
        )


# --------------------------------------------------------------------------
# Mode validation
# --------------------------------------------------------------------------

def test_legacy_auto_mode_is_refused_not_mapped_to_live(mode):
    mode("auto")
    with pytest.raises(ValueError, match="legacy mode 'auto'"):
        executor._validated_mode("WSSS")


def test_unknown_mode_raises(mode):
    mode("yolo")
    with pytest.raises(ValueError, match="Unknown execution mode"):
        executor._validated_mode("WSSS")


def test_ineligible_station_in_live_mode_raises_rather_than_downgrading(monkeypatch):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WMKK": "live"})
    with pytest.raises(ValueError, match="has not earned it"):
        executor._validated_mode("WMKK")


# --------------------------------------------------------------------------
# The $1 sizing clamp
# --------------------------------------------------------------------------

def _ev(price=0.35, prob=0.45, station="WSSS"):
    # Edge kept modest on purpose: config.max_plausible_edge_for() vetoes a
    # raw edge above ~25% at this price as a presumed data error, so a
    # headline-grabbing 0.55-vs-0.30 candidate never reaches the sizing code
    # these tests are about.
    return EVResult(
        station_icao=station, target_date=date(2026, 8, 10), bucket_c=32, side="YES",
        model_prob=prob, market_price=price, raw_edge=prob - price,
        estimated_slippage_pct=0.01, fee_rate_pct=0.0,
        net_ev_per_dollar=(prob - price) / price - 0.01, spread_source="ensemble",
    )


@pytest.mark.parametrize("station_mode,expect_capped", [
    ("simulation", True),
    ("live", True),
    ("paper", False),
    ("manual_review", False),
])
def test_live_modes_size_at_one_dollar(monkeypatch, mode, captured, station_mode, expect_capped):
    mode(station_mode)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)

    decision = entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)

    assert decision.approved
    if expect_capped:
        assert decision.recommended_size_usd == config.LIVE_TRADE_SIZE_USD
    else:
        assert decision.recommended_size_usd > config.LIVE_TRADE_SIZE_USD


def test_net_ev_at_size_describes_the_clamped_size(monkeypatch, mode, captured):
    """
    The clamp must run BEFORE the slippage/net-EV re-checks, so the recorded
    net_ev_at_size describes the order that actually gets built. If it ran
    after, slippage would be quoted for a ~$77 trade on a $1 order.
    """
    mode("simulation")
    seen_sizes = []
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)

    def _slippage(token_id, size_usd):
        seen_sizes.append(size_usd)
        return 0.01

    monkeypatch.setattr(market_client, "estimate_slippage", _slippage)
    entry_manager.evaluate_entry(_ev(), "TOK", min_net_ev=0.15)

    assert seen_sizes == [config.LIVE_TRADE_SIZE_USD]


def test_candidate_track_matches_what_executor_stamps(mode):
    """
    entry_manager's per-bucket cap must count against the same track
    executor files the position under, for every mode -- not just paper.
    """
    for station_mode in ("manual_review", "paper", "simulation"):
        mode(station_mode)
        assert entry_manager._candidate_is_paper("WSSS") is True
    mode("live")
    assert entry_manager._candidate_is_paper("WSSS") is False


# --------------------------------------------------------------------------
# Order construction: the $1 rounding problem
# --------------------------------------------------------------------------

@pytest.mark.parametrize("price", [0.05, 0.10, 0.19, 0.27, 0.33, 0.50, 0.66, 0.75])
def test_one_dollar_order_never_lands_below_one_dollar(monkeypatch, price):
    """
    The order builder rounds share counts DOWN to 2 decimals, so a naive
    $1.00 order is submitted for slightly less than $1.00 and is rejected
    wherever the exchange minimum binds at $1. Rounding up onto the same
    grid is the fix; this pins it across the price range.
    """
    stub_book(monkeypatch, tick="0.01")
    spec = wallet_client.build_entry_order("TOK", price, 1.00)

    assert spec.ok, spec.reason
    assert spec.notional_usd >= 1.00
    # Already on the builder's 2-decimal grid, so round_down() is a no-op.
    assert spec.size_shares == round(spec.size_shares, 2)
    assert spec.notional_usd <= config.LIVE_SIZE_OVERSHOOT_CEILING_USD


def test_share_count_survives_the_builders_round_down(monkeypatch):
    """The exact failure mode, stated directly: 1.00/0.33 = 3.0303..."""
    stub_book(monkeypatch, tick="0.01")
    spec = wallet_client.build_entry_order("TOK", 0.33, 1.00)

    import math
    naive = math.floor((1.00 / 0.33) * 100) / 100      # what the old path submitted
    assert naive * 0.33 < 1.00                          # ...which is under the minimum
    assert spec.size_shares > naive
    assert spec.notional_usd >= 1.00


def test_buy_limit_rounds_up_and_sell_limit_rounds_down(monkeypatch):
    """
    Both round in the direction that keeps the order marketable. Rounding a
    buy limit down would sit it below the ask and guarantee a fill-or-kill
    order gets killed -- indistinguishable from "no liquidity".
    """
    assert wallet_client._align_price_to_tick(0.301, "0.01", "BUY") == 0.31
    assert wallet_client._align_price_to_tick(0.309, "0.01", "SELL") == 0.30
    # Already aligned: unchanged in both directions.
    assert wallet_client._align_price_to_tick(0.30, "0.01", "BUY") == 0.30
    assert wallet_client._align_price_to_tick(0.30, "0.01", "SELL") == 0.30


def test_declines_when_upsizing_is_disabled(monkeypatch):
    """
    The live minimum is 5 SHARES, so at price 0.50 the smallest legal order
    is $2.50 -- 2.5x the requested $1. With upsizing OFF, declining is the
    only correct answer. Pinned explicitly rather than read off the shipped
    default, so this keeps testing the branch it names if the default flips.
    """
    monkeypatch.setattr(config, "LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE", False)
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    spec = wallet_client.build_entry_order("TOK", 0.50, 1.00)

    assert not spec.ok
    assert "LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE is off" in spec.reason


def test_upsizing_raises_the_order_to_exactly_the_market_minimum(monkeypatch):
    """With the flag on, the order is raised to the minimum and no further."""
    monkeypatch.setattr(config, "LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE", True)
    monkeypatch.setattr(config, "LIVE_SIZE_OVERSHOOT_CEILING_USD", 5.0)
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)

    spec = wallet_client.build_entry_order("TOK", 0.50, 1.00)

    assert spec.ok, spec.reason
    assert spec.size_shares == 5.0
    assert spec.notional_usd == pytest.approx(2.50)


def test_shipped_ceiling_admits_the_worst_case_minimum_order(monkeypatch):
    """
    Guards the shipped config, not the helper: with upsizing on, the ceiling
    must clear MAX_ENTRY_PRICE x a 5-share minimum, or the most expensive
    tradeable buckets would be silently undtradeable while the config claims
    otherwise.
    """
    if not config.LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE:
        pytest.skip("upsizing disabled -- ceiling is not the binding constraint")

    worst_case = config.MAX_ENTRY_PRICE * 5
    assert config.LIVE_SIZE_OVERSHOOT_CEILING_USD >= worst_case, (
        f"ceiling ${config.LIVE_SIZE_OVERSHOOT_CEILING_USD:.2f} is below the "
        f"${worst_case:.2f} worst-case minimum order"
    )

    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    spec = wallet_client.build_entry_order("TOK", config.MAX_ENTRY_PRICE, 1.00)
    assert spec.ok, spec.reason


def test_upsizing_still_refuses_past_the_overshoot_ceiling(monkeypatch):
    monkeypatch.setattr(config, "LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE", True)
    monkeypatch.setattr(config, "LIVE_SIZE_OVERSHOOT_CEILING_USD", 2.00)
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)

    spec = wallet_client.build_entry_order("TOK", 0.50, 1.00)

    assert not spec.ok
    assert "overshoot ceiling" in spec.reason


def test_a_one_dollar_order_is_legal_only_at_low_prices(monkeypatch):
    """
    Pins the actual constraint: with a 5-share minimum, $1 buys enough
    shares only at price <= 0.20. This is the fact that makes a literal
    "$1 trade size" untradeable on most of the live WSSS book -- and
    therefore the reason upsizing is enabled at all.
    """
    monkeypatch.setattr(config, "LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE", False)
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)

    assert wallet_client.build_entry_order("TOK", 0.20, 1.00).ok
    assert not wallet_client.build_entry_order("TOK", 0.21, 1.00).ok


@pytest.mark.parametrize("price", [0.21, 0.31, 0.50, 0.62, 0.75])
def test_upsizing_makes_the_real_book_tradeable(monkeypatch, price):
    """
    The complement: the prices that a literal $1 order cannot reach are
    exactly the ones the live WSSS buckets actually trade at, and upsizing
    turns each of them into a legal order at the exchange minimum.
    """
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    spec = wallet_client.build_entry_order("TOK", price, 1.00)

    assert spec.ok, spec.reason
    assert spec.size_shares == 5.0
    assert spec.notional_usd == pytest.approx(5.0 * price)
    assert spec.notional_usd <= config.LIVE_SIZE_OVERSHOOT_CEILING_USD


def test_exit_refuses_below_the_market_minimum(monkeypatch):
    """
    A position too small to sell cannot be exited at all -- every stop-loss
    and profit-take is dead for its whole life. Fail loudly.
    """
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    spec = wallet_client.build_exit_order("TOK", 0.40, 3.2)

    assert not spec.ok
    assert "cannot be sold" in spec.reason


def test_entry_sizing_never_opens_a_position_it_cannot_exit(monkeypatch):
    """
    The structural guarantee behind the test above: whatever entry size is
    accepted must clear the same minimum the exit leg will face.
    """
    monkeypatch.setattr(config, "LIVE_ALLOW_EXCHANGE_MINIMUM_UPSIZE", True)
    monkeypatch.setattr(config, "LIVE_SIZE_OVERSHOOT_CEILING_USD", 3.75)
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)

    for price in (0.05, 0.20, 0.31, 0.50, 0.62, 0.75):
        entry = wallet_client.build_entry_order("TOK", price, 1.00)
        if not entry.ok:
            continue
        exit_spec = wallet_client.build_exit_order("TOK", price, entry.size_shares)
        assert exit_spec.ok, (
            f"price {price}: entry of {entry.size_shares} shares is not exitable "
            f"-- {exit_spec.reason}"
        )


def test_refuses_to_guess_tick_size_when_book_is_unreachable(monkeypatch):
    monkeypatch.setattr(wallet_client, "_book_constraints", lambda token_id: (None, None))
    spec = wallet_client.build_entry_order("TOK", 0.30, 1.00)

    assert not spec.ok
    assert "order book unavailable" in spec.reason


def test_exit_refuses_without_a_recorded_share_count(monkeypatch):
    """
    size_usd/entry_price is NOT an acceptable substitute: it uses the wrong
    price and asks the exchange to sell shares that do not exist.
    """
    stub_book(monkeypatch, tick="0.01")
    spec = wallet_client.build_exit_order("TOK", 0.40, None)

    assert not spec.ok
    assert "no recorded share count" in spec.reason


def test_exit_rounds_shares_down(monkeypatch):
    """Selling more than is held fails outright; dust is cheaper."""
    stub_book(monkeypatch, tick="0.01")
    spec = wallet_client.build_exit_order("TOK", 0.40, 3.4567)

    assert spec.ok
    assert spec.size_shares == 3.45


# --------------------------------------------------------------------------
# Submission gates
# --------------------------------------------------------------------------

def test_simulation_never_submits(monkeypatch):
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(wallet_client, "get_client", _explode)
    spec = wallet_client.build_entry_order("TOK", 0.30, 1.00)

    result = wallet_client.submit_order(spec, live=False)

    assert result.simulated and not result.submitted and not result.filled


def test_live_is_blocked_without_the_environment_gate(monkeypatch):
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.delenv("POLYMARKET_LIVE_TRADING", raising=False)
    monkeypatch.setattr(wallet_client, "get_client", _explode)
    spec = wallet_client.build_entry_order("TOK", 0.30, 1.00)

    result = wallet_client.submit_order(spec, live=True)

    assert not result.submitted and not result.filled
    assert "second gate closed" in result.error


def test_unresolved_spec_is_never_submitted(monkeypatch):
    monkeypatch.setattr(wallet_client, "_book_constraints", lambda token_id: (None, None))
    monkeypatch.setattr(wallet_client, "get_client", _explode)
    spec = wallet_client.build_entry_order("TOK", 0.30, 1.00)

    result = wallet_client.submit_order(spec, live=True)

    assert not result.submitted and not result.filled


def _explode(*a, **kw):
    raise AssertionError("get_client() must not be reached on this path")


@pytest.mark.parametrize("response,expected_fill", [
    # "matched" with NO amount fields at all is NOT accepted. A fill has a
    # size; a status string on its own is not evidence of one. The two
    # errors are not symmetric -- treating a real fill as unfilled leaves an
    # untracked position a human will see on the exchange, while treating a
    # kill as a fill writes a phantom position the exit path will try to
    # sell. Erring this way is deliberate, and matches the verified
    # implementation in ~/Downloads/hermes/core/execution.py.
    ({"success": True, "status": "matched"}, False),
    ({"success": True, "status": "matched", "takingAmount": "12.8"}, True),
    ({"success": True, "status": "live"}, False),      # resting, not filled
    ({"success": True, "status": "delayed"}, False),
    ({"success": False, "status": "matched"}, False),
    ({"status": "unmatched"}, False),
    ({}, False),                                        # unrecognised -> not a fill
    ("some string", False),                             # not even a dict
])
def test_fill_interpretation_defaults_to_not_filled(response, expected_fill):
    spec = wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=0.3,
        size_shares=3.34, notional_usd=1.002,
    )
    filled, _, _ = wallet_client._interpret_fill(response, spec)
    assert filled is expected_fill


# --------------------------------------------------------------------------
# executor: entries
# --------------------------------------------------------------------------

def test_unfilled_live_order_records_no_position(monkeypatch, mode, captured):
    """
    THE failure this design exists to prevent. A stored "open" position with
    no shares behind it is one the exit path will later try to sell.
    """
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=False, simulated=False, spec=spec, error="killed unfilled",
        ),
    )

    executor.open_position(make_decision())

    assert captured["opened"] == []


def test_filled_live_order_records_shares_and_the_real_fill_price(monkeypatch, mode, captured, reconciled):
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=True, simulated=False, spec=spec,
            order_id="0xabc", fill_price=0.31, fill_shares=3.23,
        ),
    )

    executor.open_position(make_decision())

    assert len(captured["opened"]) == 1
    pos = captured["opened"][0]
    assert pos.execution_mode == "live"
    assert pos.is_paper is False
    assert pos.size_shares == 3.23
    assert pos.entry_price == 0.31              # the real fill, not the decision price
    assert pos.order_id == "0xabc"
    assert pos.size_usd == pytest.approx(0.31 * 3.23, rel=1e-6)


def test_simulation_records_the_resolved_size_not_the_requested_one(monkeypatch, mode, captured):
    mode("simulation")
    stub_book(monkeypatch, tick="0.01")

    executor.open_position(make_decision(size_usd=1.00, price=0.33))

    assert len(captured["opened"]) == 1
    pos = captured["opened"][0]
    assert pos.execution_mode == "simulation"
    assert pos.is_paper is True                  # zero real risk
    assert pos.size_shares is not None
    assert pos.size_usd >= 1.00                  # what the real order would have cost


def test_entry_abandoned_when_tick_alignment_blows_the_slippage_budget(monkeypatch, mode, captured):
    """
    On a 0.1-tick market, aligning a 0.31 limit up to 0.40 is +29% -- far
    past MAX_ACCEPTABLE_SLIPPAGE_PCT. The entry was sized under a slippage
    budget that no longer describes this order.
    """
    mode("simulation")
    stub_book(monkeypatch, tick="0.1")

    executor.open_position(make_decision(price=0.31))

    assert captured["opened"] == []


def test_live_entry_blocked_by_exposure_backstop(monkeypatch, mode, captured):
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    existing = [make_position(mode="live") for _ in range(config.LIVE_MAX_CONCURRENT_POSITIONS)]
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: list(existing))
    monkeypatch.setattr(wallet_client, "submit_order", _explode)

    executor.open_position(make_decision())

    assert captured["opened"] == []


def test_unapproved_decision_never_reaches_the_order_path(monkeypatch, mode, captured):
    mode("live")
    monkeypatch.setattr(wallet_client, "build_entry_order", _explode)

    executor.open_position(make_decision(approved=False))

    assert captured["opened"] == []


# --------------------------------------------------------------------------
# executor: exits
# --------------------------------------------------------------------------

def _exit_decision(reason="stop_loss", price=0.20):
    return ExitDecision(
        position_id="p1", should_exit=True, reason=reason,
        current_price=price, pnl_pct=-0.33,
    )


def test_unfilled_live_exit_leaves_the_position_open(monkeypatch, mode, captured):
    """
    Recording a close we could not place would hide a position whose shares
    are still on the exchange from every later exit check.
    """
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=False, simulated=False, spec=spec, error="killed unfilled",
        ),
    )

    executor.close_position(make_position(), _exit_decision())

    assert captured["closed"] == []


def test_unbuildable_exit_leaves_the_position_open(monkeypatch, mode, captured):
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(wallet_client, "submit_order", _explode)

    # No recorded share count -> the sell cannot be built.
    executor.close_position(make_position(size_shares=None), _exit_decision())

    assert captured["closed"] == []


def test_resolution_close_places_no_order_in_live_mode(monkeypatch, mode, captured):
    """
    A resolved market has no book to sell into. An order there is a
    guaranteed non-fill, which would then block the close and strand the
    position.
    """
    mode("live")
    monkeypatch.setattr(wallet_client, "build_exit_order", _explode)
    monkeypatch.setattr(wallet_client, "submit_order", _explode)

    executor.close_position(
        make_position(), _exit_decision(reason="resolution", price=1.0),
        status="closed_resolution", exit_reason="market_resolved",
    )

    assert len(captured["closed"]) == 1
    assert captured["closed"][0]["status"] == "closed_resolution"


def test_filled_live_exit_is_recorded_net_of_the_exit_fee(monkeypatch, mode, captured):
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=True, simulated=False, spec=spec,
            order_id="0xdef", fill_price=spec.limit_price, fill_shares=spec.size_shares,
        ),
    )

    import risk_manager
    executor.close_position(make_position(), _exit_decision(price=0.20))

    assert len(captured["closed"]) == 1
    recorded = captured["closed"][0]["exit_price"]
    assert recorded == pytest.approx(0.20 - risk_manager.taker_fee_per_share(0.20))


def test_live_position_is_not_closed_by_a_non_live_process(monkeypatch, mode, captured):
    """
    THE stranding bug, stated directly. A real position is open; the daemon
    restarts without the live flag, so WSSS is back to manual_review. The old
    code dispatched on the STATION'S CURRENT MODE, printed an ACTION NEEDED
    and wrote the close immediately -- database says closed, shares still
    held, no stop-loss, invisible to every report from then on.

    Dispatch is on the POSITION'S origin mode, and a live position may only
    be closed by a process authorized for live.
    """
    mode("manual_review")                     # daemon restarted without --mode live
    monkeypatch.setattr(wallet_client, "build_exit_order", _explode)
    monkeypatch.setattr(wallet_client, "submit_order", _explode)

    executor.close_position(make_position(mode="live"), _exit_decision())

    assert captured["closed"] == [], "a live position was closed by a manual_review process"


def test_paper_position_never_reaches_the_order_path_in_a_live_process(monkeypatch, mode, captured):
    """
    The mirror-image failure: starting WITH the live flag must not route a
    pre-existing paper position into a real sell of shares that don't exist.
    """
    mode("live")
    monkeypatch.setattr(wallet_client, "build_exit_order", _explode)
    monkeypatch.setattr(wallet_client, "submit_order", _explode)

    executor.close_position(make_position(mode="paper"), _exit_decision())

    assert len(captured["closed"]) == 1       # closed as paper, no order attempted


def test_simulation_exit_records_without_submitting(monkeypatch, mode, captured):
    mode("simulation")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(wallet_client, "get_client", _explode)

    executor.close_position(make_position(mode="simulation"), _exit_decision())

    assert len(captured["closed"]) == 1


@pytest.mark.parametrize("response", [
    {"status": "", "takingAmount": "0", "makingAmount": "0"},
    {"takingAmount": "0"},
    {"takingAmount": 0.0},
    {"status": "matched", "takingAmount": "0"},
    {"status": "matched", "success": True, "size_matched": "0"},
])
def test_a_zero_matched_amount_is_never_a_fill(response):
    """
    The exchange returns matched amounts as STRINGS, and bool('0') is True
    in Python. Testing them for truthiness read a killed FOK as a fill and
    wrote a position with no shares behind it -- the failure this whole
    module is arranged to prevent. Parse as a number, require > 0.
    """
    spec = wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=0.39,
        size_shares=5.0, notional_usd=1.95,
    )
    filled, _, _ = wallet_client._interpret_fill(response, spec)
    assert filled is False


def test_the_real_matched_response_shape_is_recognised():
    """
    Verbatim from a real on-chain fill: no size_matched key, amounts as
    strings, shares in takingAmount and USDC in makingAmount for a BUY.
    """
    spec = wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=0.08,
        size_shares=14.0, notional_usd=1.0,
    )
    filled, price, shares = wallet_client._interpret_fill({
        "status": "matched", "success": True,
        "takingAmount": "14.285713", "makingAmount": "0.999999",
        "transactionsHashes": ["0xabc"], "orderID": "0xdef", "errorMsg": "",
    }, spec)

    assert filled is True
    assert shares == pytest.approx(14.285713)
    assert price == pytest.approx(0.07, abs=0.001)


# --------------------------------------------------------------------------
# Ghost books (py-clob-client issue #180)
# --------------------------------------------------------------------------

GHOST = {"bids": [{"price": "0.01", "size": "10"}], "asks": [{"price": "0.99", "size": "10"}]}


@pytest.mark.parametrize("book,expected", [
    (GHOST, True),
    # Real extremes trip ONE bound, never both -- these must stay tradeable.
    ({"bids": [{"price": "0.000", "size": "99"}], "asks": [{"price": "0.001", "size": "99"}]}, False),
    ({"bids": [{"price": "0.998", "size": "5"}], "asks": [{"price": "1.000", "size": "5"}]}, False),
    ({"bids": [{"price": "0.38", "size": "40"}], "asks": [{"price": "0.39", "size": "40"}]}, False),
    # A missing side is an ordinary thin market, already handled as no depth.
    ({"bids": [], "asks": [{"price": "0.99", "size": "1"}]}, False),
    ({"bids": [], "asks": []}, False),
    (None, False),
])
def test_ghost_book_detection(book, expected):
    assert market_client.is_ghost_book(book) is expected


def test_a_ghost_book_reads_as_no_book_at_all(monkeypatch):
    """
    The guard lives in get_order_book(), so depth, slippage and the
    tick/minimum lookup are all covered by one check rather than three.
    """
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return GHOST

    monkeypatch.setattr(market_client.requests, "get", lambda *a, **kw: _Resp())
    monkeypatch.setattr(market_client, "_ghost_book_seen", set())

    assert market_client.get_order_book("TOK") is None
    assert market_client.get_available_depth_usd("TOK") is None


def test_an_order_cannot_be_built_against_a_ghost_book(monkeypatch):
    """
    Fails closed. A 0.01/0.99 snapshot would otherwise price a bucket at
    the extreme opposite of reality.
    """
    monkeypatch.setattr(market_client, "get_order_book", lambda *a, **kw: None)
    spec = wallet_client.build_entry_order("TOK", 0.39, 1.00)

    assert not spec.ok
    assert "order book unavailable" in spec.reason


def test_a_drift_check_could_not_have_caught_this():
    """
    Documents WHY a dedicated check is needed: the stale snapshot persists
    unchanged across repeated fetches, so any "did the book move?" guard
    sees zero drift and passes it through.
    """
    first, second = dict(GHOST), dict(GHOST)
    assert first == second                       # zero drift between reads
    assert market_client.is_ghost_book(first)    # ...and both are unusable


# --------------------------------------------------------------------------
# Balance propagation
# --------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, balances):
        self.balances = list(balances)
        self.syncs = 0
        self.reads = 0

    def update_balance_allowance(self, params):
        self.syncs += 1

    def get_balance_allowance(self, params):
        self.reads += 1
        return {"balance": self.balances[min(self.reads - 1, len(self.balances) - 1)]}


def test_balance_poll_waits_for_the_refresh_to_propagate(monkeypatch):
    """
    update_balance_allowance() returning 200 does not mean post_order() will
    see the new balance -- it only triggers a re-check. Poll until nonzero.
    """
    monkeypatch.setattr(wallet_client.time, "sleep", lambda s: None)
    client = _FakeClient(["0", "0", "12.5"])

    assert wallet_client._wait_for_balance(client) is True
    assert client.syncs == 1
    assert client.reads == 3


def test_balance_poll_gives_up_and_lets_the_exchange_decide(monkeypatch):
    """
    A stuck-at-zero read must not block an order the operator asked for --
    post_order() is the authority, and its rejection is the real signal.
    """
    import config as cfg

    monkeypatch.setattr(wallet_client.time, "sleep", lambda s: None)
    client = _FakeClient(["0"])

    assert wallet_client._wait_for_balance(client) is False
    assert client.reads == cfg.BALANCE_POLL_ATTEMPTS


def test_balance_check_never_raises(monkeypatch):
    class _Broken:
        def update_balance_allowance(self, params):
            raise RuntimeError("network down")
        def get_balance_allowance(self, params):
            raise RuntimeError("network down")

    monkeypatch.setattr(wallet_client.time, "sleep", lambda s: None)
    assert wallet_client._wait_for_balance(_Broken()) is False


def test_simulation_never_touches_the_balance_api(monkeypatch):
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(wallet_client, "_wait_for_balance", _explode)
    monkeypatch.setattr(wallet_client, "get_client", _explode)

    spec = wallet_client.build_entry_order("TOK", 0.39, 1.00)
    result = wallet_client.submit_order(spec, live=False)

    assert result.simulated and not result.submitted


# --------------------------------------------------------------------------
# FOK limit padding
# --------------------------------------------------------------------------

def test_buy_limit_is_padded_above_the_expected_fill(monkeypatch):
    """
    A FOK at exactly the observed ask dies on one adverse tick. The pad
    widens what we ACCEPT, not what we expect -- expected_price stays the
    real quote and size is computed from it.
    """
    stub_book(monkeypatch, tick="0.01")
    spec = wallet_client.build_entry_order("TOK", 0.39, 1.00)

    assert spec.expected_price == 0.39
    assert spec.limit_price > spec.expected_price
    assert spec.notional_usd == pytest.approx(spec.size_shares * spec.expected_price)


def test_sell_limit_is_padded_below_the_expected_fill(monkeypatch):
    stub_book(monkeypatch, tick="0.01")
    spec = wallet_client.build_exit_order("TOK", 0.40, 10.0)

    assert spec.expected_price == 0.40
    assert spec.limit_price < spec.expected_price


@pytest.mark.parametrize("price,tick", [
    (0.39, "0.01"), (0.57, "0.01"), (0.95, "0.01"),
    (0.18, "0.001"), (0.03, "0.001"), (0.05, "0.01"),
])
def test_the_pad_never_exceeds_its_percentage_cap(monkeypatch, price, tick):
    """
    Snapping to the tick grid must not push the pad back over the cap.
    Aligning OUTWARD turned a capped 3% into 5.1% at ask 0.39 on a 0.01
    tick -- which is how a cap stops being one.
    """
    stub_book(monkeypatch, tick=tick)
    spec = wallet_client.build_entry_order("TOK", price, 1.00)

    pad = (spec.limit_price - spec.expected_price) / spec.expected_price
    assert 0 <= pad <= config.LIVE_LIMIT_PAD_MAX_PCT + 1e-9


def test_a_cap_below_one_tick_leaves_the_price_unpadded(monkeypatch):
    """3% of 0.05 is under a 0.01 tick -- pad nothing rather than overshoot."""
    stub_book(monkeypatch, tick="0.01")
    spec = wallet_client.build_entry_order("TOK", 0.05, 1.00)

    assert spec.limit_price == spec.expected_price


def test_the_pad_is_spent_from_the_slippage_budget():
    """
    The padded limit is money the exchange may take, so it must fit inside
    the same budget a real adverse move is measured against -- and leave
    room for one.
    """
    assert config.LIVE_LIMIT_PAD_MAX_PCT < config.MAX_ACCEPTABLE_SLIPPAGE_PCT


def test_the_overshoot_ceiling_binds_on_the_worst_case(monkeypatch):
    """
    Otherwise the pad is a hole in the only cap on trade size: the expected
    cost passes while the price actually payable does not.
    """
    monkeypatch.setattr(config, "LIVE_SIZE_OVERSHOOT_CEILING_USD", 4.80)
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)

    spec = wallet_client.build_entry_order("TOK", 0.95, 1.00)

    assert spec.notional_usd <= 4.80          # expected cost is under the ceiling
    assert not spec.ok                         # ...but the worst case is not
    assert "worst-case" in spec.reason


def test_padding_does_not_change_the_recorded_fill_price(monkeypatch, mode, captured, reconciled):
    """
    The pad is what we were willing to pay; entry_price must record what we
    actually paid, parsed from the response.
    """
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=True, simulated=False, spec=spec,
            order_id="0x1", fill_price=spec.expected_price, fill_shares=spec.size_shares,
        ),
    )

    executor.open_position(make_decision(price=0.39))

    assert captured["opened"][0].entry_price == 0.39


# --------------------------------------------------------------------------
# Re-checking size after the exchange-minimum upsize
# --------------------------------------------------------------------------

def _spec(notional, price=0.40, shares=5.0):
    return wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=price,
        size_shares=shares, notional_usd=notional, expected_price=price,
    )


def test_an_unchanged_size_is_not_rechecked(monkeypatch):
    monkeypatch.setattr(market_client, "estimate_slippage", _explode)
    ok, note = executor._resolved_size_ok(_spec(1.00), make_decision(size_usd=1.00))
    assert ok and note == ""


def test_upsized_order_is_rejected_when_it_blows_the_depth_cap(monkeypatch):
    """
    The entry cleared depth at $1. The exchange minimum makes it $3.75, and
    nothing re-ran the check -- so a trade approved against the book at one
    size was submitted at up to 3.75x it.
    """
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    decision = make_decision(size_usd=1.00)
    decision.available_depth_usd = 8.0          # 25% of 8 = $2.00 ceiling

    ok, note = executor._resolved_size_ok(_spec(3.75), decision)

    assert not ok
    assert "visible depth" in note


def test_upsized_order_is_rejected_when_slippage_now_fails(monkeypatch):
    monkeypatch.setattr(market_client, "estimate_slippage",
                        lambda t, s: config.MAX_ACCEPTABLE_SLIPPAGE_PCT + 0.01)
    ok, note = executor._resolved_size_ok(_spec(3.75), make_decision(size_usd=1.00))

    assert not ok
    assert "hard gate" in note


def test_upsized_order_is_rejected_when_net_ev_goes_negative(monkeypatch):
    """
    Only slippage moves with size, so the net-EV change is exactly the
    slippage increase -- no field the EntryDecision lacks.
    """
    decision = make_decision(size_usd=1.00)
    decision.net_ev_at_size = 0.02
    decision.slippage_at_size_pct = 0.01
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.05)

    ok, note = executor._resolved_size_ok(_spec(3.75), decision)

    assert not ok
    assert "net EV falls" in note


def test_an_upsize_that_still_clears_everything_is_allowed(monkeypatch):
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.011)
    decision = make_decision(size_usd=1.00)
    decision.available_depth_usd = 1000.0
    decision.net_ev_at_size = 0.30
    decision.slippage_at_size_pct = 0.01

    ok, note = executor._resolved_size_ok(_spec(3.75), decision)

    assert ok
    assert "exchange minimum" in note


def test_a_failed_slippage_recheck_rejects_rather_than_passes(monkeypatch):
    """A re-check that cannot run must not count as a re-check that passed."""
    def boom(t, s):
        raise RuntimeError("book endpoint down")

    monkeypatch.setattr(market_client, "estimate_slippage", boom)
    ok, note = executor._resolved_size_ok(_spec(3.75), make_decision(size_usd=1.00))

    assert not ok
    assert "refusing to guess" in note


def test_the_upsize_recheck_runs_before_any_order_is_submitted(monkeypatch, mode, captured):
    mode("live")
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    monkeypatch.setattr(market_client, "estimate_slippage",
                        lambda t, s: config.MAX_ACCEPTABLE_SLIPPAGE_PCT + 0.01)
    monkeypatch.setattr(wallet_client, "submit_order", _explode)

    executor.open_position(make_decision(size_usd=1.00, price=0.50))

    assert captured["opened"] == []


# --------------------------------------------------------------------------
# Exchange reconciliation
# --------------------------------------------------------------------------

class _Exchange:
    """A fake CLOB: token -> shares held, plus a list of traded asset ids."""

    def __init__(self, balances=None, traded=(), fail_balance=(), fail_trades=False):
        self.balances = balances or {}
        self.traded = list(traded)
        self.fail_balance = set(fail_balance)
        self.fail_trades = fail_trades

    def get_balance_allowance(self, params):
        token = params.token_id
        if token in self.fail_balance:
            raise RuntimeError("balance endpoint down")
        return {"balance": str(self.balances.get(token, 0.0))}

    def get_trades(self, params=None, **kw):
        if self.fail_trades:
            raise RuntimeError("trade history down")
        return [{"asset_id": a} for a in self.traded]


@pytest.fixture
def exchange(monkeypatch):
    """Wire a fake exchange in, with credentials present."""
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xtest")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setattr(wallet_client, "_reconcile_cache", {"at": 0.0, "result": None})

    def _install(ex):
        monkeypatch.setattr(wallet_client, "get_client", lambda: ex)
        return ex

    return _install


def _live_pos(token="TOK", shares=5.0):
    p = make_position(mode="live", size_shares=shares)
    p.token_id = token
    return p


def test_reconciliation_passes_when_the_exchange_agrees(exchange):
    exchange(_Exchange(balances={"TOK": 5.0}, traded=["TOK"]))
    r = wallet_client.reconcile_live_positions([_live_pos()])

    assert r.ok and r.checked
    assert r.verified == [("TOK", 5.0)]


def test_unrecorded_holdings_are_caught(exchange):
    """
    THE case that breaks the caps: shares held that the database has no open
    row for. Unrecorded exposure is exposure the backstops cannot see.
    """
    exchange(_Exchange(balances={"TOK": 5.0, "GHOST": 12.0}, traded=["TOK", "GHOST"]))
    r = wallet_client.reconcile_live_positions([_live_pos()])

    assert not r.ok
    assert r.exchange_only == [("GHOST", 12.0)]
    assert "NOT RECORDED" in r.describe()


def test_a_position_the_exchange_no_longer_holds_is_caught(exchange):
    """The other direction: the exit path would try to sell what is gone."""
    exchange(_Exchange(balances={"TOK": 0.0}, traded=["TOK"]))
    r = wallet_client.reconcile_live_positions([_live_pos(shares=5.0)])

    assert not r.ok
    assert r.db_only == [("TOK", 5.0, 0.0)]


def test_a_traded_token_since_sold_is_not_flagged(exchange):
    """
    get_trades() returns fills, including closed ones. A token that traded
    but is no longer held is not an unrecorded position.
    """
    exchange(_Exchange(balances={"TOK": 5.0, "SOLD": 0.0}, traded=["TOK", "SOLD"]))
    r = wallet_client.reconcile_live_positions([_live_pos()])

    assert r.ok
    assert r.exchange_only == []


@pytest.mark.parametrize("broken,expected", [
    ({"fail_balance": ["TOK"]}, "could not read the exchange balance"),
    ({"fail_trades": True}, "could not read trade history"),
])
def test_an_unreadable_exchange_fails_closed(exchange, broken, expected):
    """
    "I could not look" and "I looked and it was wrong" are the same answer
    when the question is whether to spend more money.
    """
    exchange(_Exchange(balances={"TOK": 5.0}, traded=["TOK"], **broken))
    r = wallet_client.reconcile_live_positions([_live_pos()])

    assert not r.ok
    assert expected in r.reason


def test_missing_credentials_fail_closed_without_claiming_a_check(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)
    monkeypatch.setattr(wallet_client, "_reconcile_cache", {"at": 0.0, "result": None})

    r = wallet_client.reconcile_live_positions([_live_pos()])

    assert not r.ok
    assert not r.checked, "must not claim to have checked when it could not"
    assert "NOT CHECKED" in r.describe()


def test_a_position_with_no_token_id_cannot_be_verified(exchange):
    exchange(_Exchange())
    pos = _live_pos()
    pos.token_id = None

    r = wallet_client.reconcile_live_positions([pos])

    assert not r.ok
    assert "no token_id" in r.reason


def test_reconciliation_is_cached_within_the_ttl(exchange):
    ex = exchange(_Exchange(balances={"TOK": 5.0}, traded=["TOK"]))
    calls = []
    original = ex.get_trades
    ex.get_trades = lambda params=None, **kw: (calls.append(1), original(params))[1]

    wallet_client.reconcile_cached([_live_pos()])
    wallet_client.reconcile_cached([_live_pos()])

    assert len(calls) == 1, "the second call should have been served from cache"


# --- enforcement -----------------------------------------------------------

def test_a_divergent_exchange_blocks_new_live_entries(monkeypatch, mode, captured):
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions: wallet_client.Reconciliation(
            ok=False, checked=True, exchange_only=[("GHOST", 12.0)], reason="divergence"),
    )
    monkeypatch.setattr(wallet_client, "submit_order", _explode)

    executor.open_position(make_decision())

    assert captured["opened"] == []


def test_reconciliation_never_blocks_an_exit(monkeypatch, mode, captured):
    """
    Entries only. Refusing to CLOSE a real position over a bookkeeping doubt
    would strand actual money -- the same asymmetry the budget checks already
    observe.
    """
    mode("live")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions: wallet_client.Reconciliation(
            ok=False, checked=True, exchange_only=[("GHOST", 12.0)], reason="divergence"),
    )
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=True, simulated=False, spec=spec,
            order_id="0x1", fill_price=spec.expected_price, fill_shares=spec.size_shares,
        ),
    )

    executor.close_position(make_position(), _exit_decision())

    assert len(captured["closed"]) == 1, "an exit was blocked by a reconciliation failure"


def test_simulation_entries_never_consult_the_exchange(monkeypatch, mode, captured):
    """Simulation must stay credential-free; reconciliation needs credentials."""
    mode("simulation")
    stub_book(monkeypatch, tick="0.01")
    monkeypatch.setattr(wallet_client, "reconcile_cached", _explode)

    executor.open_position(make_decision())

    assert len(captured["opened"]) == 1


# --------------------------------------------------------------------------
# Escalating a refused live close
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_refusals():
    executor._consecutive_live_close_refusals.clear()
    yield
    executor._consecutive_live_close_refusals.clear()


def test_a_refused_live_close_counts_and_shouts(monkeypatch, mode, captured, capsys):
    """
    Refusing is correct; refusing SILENTLY is not. The refusal used to be one
    print and a return, repeated forever, with no counter and no instruction
    -- on the one path where real shares have an unhonoured stop-loss.
    """
    mode("manual_review")
    executor.close_position(make_position(mode="live"), _exit_decision())

    out = capsys.readouterr().out
    assert captured["closed"] == []
    assert "[ACTION NEEDED]" in out
    assert "REAL SHARES ARE HELD" in out
    assert "POLYWEATHER_MODE=live" in out
    assert executor._consecutive_live_close_refusals["p1"] == 1


def test_consecutive_refusals_accumulate(monkeypatch, mode, captured, capsys):
    mode("manual_review")
    pos = make_position(mode="live")
    for _ in range(3):
        executor.close_position(pos, _exit_decision())

    assert executor._consecutive_live_close_refusals["p1"] == 3
    assert "Refused 3 cycle(s)" in capsys.readouterr().out


def test_the_alert_carries_what_an_operator_needs(monkeypatch, mode, captured, capsys):
    mode("manual_review")
    pos = make_position(mode="live", size_shares=5.0, price=0.21)
    pos.order_id = "0xdeadbeef"

    executor.close_position(pos, _exit_decision(reason="trailing_stop", price=0.28))
    out = capsys.readouterr().out

    for needed in ("5.00 shares", "0.2100", "0xdeadbeef", "TRAILING_STOP", "0.2800", "WSSS"):
        assert needed in out, f"alert is missing {needed!r}"


def test_a_streak_can_be_cleared(mode, captured):
    mode("manual_review")
    executor.close_position(make_position(mode="live"), _exit_decision())
    assert executor._consecutive_live_close_refusals

    executor.forget_live_close_refusals("p1")
    assert "p1" not in executor._consecutive_live_close_refusals


# --- the startup check -----------------------------------------------------

def test_unmanageable_positions_are_found_at_startup(monkeypatch, mode):
    mode("simulation")
    monkeypatch.setattr(storage, "load_open_positions",
                        lambda **kw: [make_position(mode="live")])

    assert len(executor.unmanageable_live_positions()) == 1


def test_a_live_process_has_nothing_unmanageable(monkeypatch, mode):
    mode("live")
    monkeypatch.setattr(storage, "load_open_positions",
                        lambda **kw: [make_position(mode="live")])

    assert executor.unmanageable_live_positions() == []


def test_paper_positions_are_never_unmanageable(monkeypatch, mode):
    mode("simulation")
    monkeypatch.setattr(storage, "load_open_positions",
                        lambda **kw: [make_position(mode="paper"), make_position(mode="simulation")])

    assert executor.unmanageable_live_positions() == []


def test_the_startup_banner_names_the_exposure(monkeypatch, mode, capsys):
    mode("simulation")
    monkeypatch.setattr(storage, "load_open_positions",
                        lambda **kw: [make_position(mode="live")])

    n = executor.warn_about_unmanageable_live_positions()
    out = capsys.readouterr().out

    assert n == 1
    assert "[ACTION NEEDED]" in out
    assert "real exposure" in out
    assert "stop-losses and profit-takes will NOT fire" in out


def test_the_startup_check_never_stops_the_daemon(monkeypatch, mode, capsys):
    """A boot-time diagnostic must not be the thing that prevents booting."""
    mode("simulation")

    def _broken(**kw):
        raise RuntimeError("db unreadable")

    monkeypatch.setattr(storage, "load_open_positions", _broken)

    assert executor.warn_about_unmanageable_live_positions() == 0
    assert "could not check" in capsys.readouterr().out


def test_scheduler_warns_at_startup():
    """Structural: the banner only helps if the daemon actually calls it."""
    import ast
    from pathlib import Path

    import scheduler

    tree = ast.parse(Path(scheduler.__file__).read_text(encoding="utf-8"))
    called = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "warn_about_unmanageable_live_positions" in called, (
        "scheduler.py must run the unmanageable-live-position check at startup"
    )


# --------------------------------------------------------------------------
# The daily cap counts SUBMISSIONS, not fills
# --------------------------------------------------------------------------

@pytest.fixture
def live_db(tmp_path, monkeypatch):
    """A real throwaway database, so the audit table is genuinely exercised."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    storage._connect().close()
    monkeypatch.setattr(
        wallet_client, "reconcile_cached",
        lambda positions: wallet_client.Reconciliation(ok=True, checked=True, reason="stubbed"),
    )


def _attempt(kind="entry", outcome="killed"):
    storage.record_live_order_attempt(
        kind=kind, station_icao="WSSS", outcome=outcome,
        bucket_c=33, side="YES", notional_usd=1.95,
    )


def test_killed_orders_consume_the_daily_budget(live_db):
    """
    THE defect. An unfilled FOK writes no position -- correctly -- so a cap
    counting `positions` never decremented on a killed order. A day of two
    hundred rejections consumed none of the ten-order budget.
    """
    for _ in range(config.LIVE_MAX_ORDERS_PER_DAY):
        _attempt(outcome="killed")

    breach = executor._live_budget_breach(1.95)

    assert breach is not None
    assert "SUBMITTED today" in breach
    assert "filled or not" in breach


def test_a_fresh_day_starts_with_a_full_budget(live_db):
    assert executor._live_budget_breach(1.95) is None


def test_exits_do_not_consume_the_entry_budget(live_db):
    """An exit must never be rate-limited, though it is still audited."""
    for _ in range(config.LIVE_MAX_ORDERS_PER_DAY * 2):
        _attempt(kind="exit", outcome="filled")

    assert executor._live_budget_breach(1.95) is None
    assert storage.count_live_order_attempts("exit", "2000-01-01") == config.LIVE_MAX_ORDERS_PER_DAY * 2


def test_an_unreadable_count_fails_closed(live_db, monkeypatch):
    """A rate limit that fails open is not a rate limit."""
    monkeypatch.setattr(storage, "count_live_order_attempts", lambda kind, since: None)

    breach = executor._live_budget_breach(1.95)

    assert breach is not None
    assert "unenforceable rate limit" in breach


def test_yesterdays_orders_do_not_count_against_today(live_db):
    with storage._db() as conn:
        conn.execute(
            "INSERT INTO live_order_attempts (ts, kind, station_icao, outcome) VALUES (?,?,?,?)",
            ("2020-01-01T00:00:00+00:00", "entry", "WSSS", "filled"),
        )
    assert executor._live_budget_breach(1.95) is None


# --- the audit trail -------------------------------------------------------

def test_a_live_submission_is_recorded_whatever_the_outcome(live_db, monkeypatch, mode, capsys):
    mode("live")
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=False, simulated=False, spec=spec, error="killed unfilled",
        ),
    )

    executor.open_position(make_decision(size_usd=1.00, price=0.30))

    rows = storage.load_live_order_attempts()
    assert len(rows) == 1
    assert rows[0]["kind"] == "entry"
    assert rows[0]["outcome"] == "killed"
    assert rows[0]["detail"] == "killed unfilled"
    # ...and no position, because nothing filled.
    assert storage.load_open_positions() == []


def test_a_filled_submission_records_its_order_id(live_db, monkeypatch, mode):
    mode("live")
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=True, simulated=False, spec=spec,
            order_id="0xfeed", fill_price=spec.expected_price, fill_shares=spec.size_shares,
        ),
    )

    executor.open_position(make_decision(size_usd=1.00, price=0.30))

    rows = storage.load_live_order_attempts()
    assert rows[0]["outcome"] == "filled"
    assert rows[0]["order_id"] == "0xfeed"


def test_simulation_submissions_are_not_audited(live_db, monkeypatch, mode):
    """The table is the REAL-money audit trail; simulation would pollute it."""
    mode("simulation")
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)

    executor.open_position(make_decision(size_usd=1.00, price=0.30))

    assert storage.load_live_order_attempts() == []


def test_a_failed_audit_write_does_not_undo_the_order(live_db, monkeypatch, mode, capsys):
    """
    The order has already reached the exchange. Losing the bookkeeping must
    complain loudly, not raise.
    """
    mode("live")
    stub_book(monkeypatch, tick="0.01", min_order_size=5.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(
        wallet_client, "submit_order",
        lambda spec, live: wallet_client.OrderResult(
            submitted=True, filled=True, simulated=False, spec=spec,
            order_id="0xfeed", fill_price=spec.expected_price, fill_shares=spec.size_shares,
        ),
    )

    def _broken(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(storage, "record_live_order_attempt", _broken)

    executor.open_position(make_decision(size_usd=1.00, price=0.30))
    out = capsys.readouterr().out

    assert "could not record the live entry attempt" in out
    assert "under-counting" in out
    assert len(storage.load_open_positions()) == 1, "the fill must still be recorded"
