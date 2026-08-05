"""
Polymarket taker-fee schedule wiring in ev_engine.

The old DEFAULT_FEE_RATE_PCT = 0.0 was an explicit placeholder; the
real schedule (verified 2026-08-05 against docs.polymarket.com --
Weather category) charges takers fee = shares * 0.05 * p * (1 - p),
i.e. 0.05 * (1 - p) of the dollars spent buying at price p. These
tests pin the helper's arithmetic and, more importantly, that
compute_ev_table actually deducts the price-dependent fee per
candidate by default while an explicit flat override still wins (the
backtest engine depends on the override path).
"""

from datetime import date

import pytest

import ev_engine
from models import CalibratedEstimate, MarketQuote
from clients import market_client


def test_fee_pct_formula():
    # fee/notional = rate * (1 - p)
    assert ev_engine.taker_fee_pct_of_notional(0.35) == pytest.approx(0.05 * 0.65)
    assert ev_engine.taker_fee_pct_of_notional(0.10) == pytest.approx(0.05 * 0.90)
    assert ev_engine.taker_fee_pct_of_notional(1.0) == pytest.approx(0.0)
    # Missing/degenerate prices carry no fee (those candidates are
    # never sized -- net_ev is None for them anyway).
    assert ev_engine.taker_fee_pct_of_notional(None) == 0.0
    assert ev_engine.taker_fee_pct_of_notional(0.0) == 0.0


@pytest.fixture()
def one_bucket_setup(monkeypatch):
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size: 0.01)
    estimate = CalibratedEstimate(
        station_icao="WSSS", target_date=date(2026, 8, 10),
        central_estimate_c=32.0, std_dev_c=1.0,
        monsoon_phase="inter_monsoon", spread_source="ensemble",
    )
    token_map = {32: {"yes_token_id": "Y32", "no_token_id": "N32"}}
    quotes = {32: MarketQuote(bucket_c=32, yes_price=0.35, no_price=0.66,
                              fetched_at="2026-08-10T00:00:00+00:00")}
    return estimate, token_map, quotes


def test_default_applies_price_dependent_fee_per_side(one_bucket_setup):
    estimate, token_map, quotes = one_bucket_setup
    results = ev_engine.compute_ev_table(estimate, token_map, quotes=quotes)

    by_side = {r.side: r for r in results}
    yes, no = by_side["YES"], by_side["NO"]

    # Each side's fee is computed at ITS OWN price -- the two sides of
    # one bucket carry different fees, which a flat rate cannot express.
    assert yes.fee_rate_pct == pytest.approx(0.05 * (1 - 0.35))
    assert no.fee_rate_pct == pytest.approx(0.05 * (1 - 0.66))
    assert yes.fee_rate_pct != no.fee_rate_pct

    # And the fee is actually deducted from net EV.
    assert yes.net_ev_per_dollar == pytest.approx(
        yes.raw_edge / 0.35 - 0.01 - yes.fee_rate_pct
    )


def test_flat_override_still_wins(one_bucket_setup):
    # The backtest engine passes an explicit float to keep fee
    # sensitivity a cache-keyed run parameter; 0.0 must mean 0.0.
    estimate, token_map, quotes = one_bucket_setup
    results = ev_engine.compute_ev_table(estimate, token_map, fee_rate_pct=0.0, quotes=quotes)
    assert all(r.fee_rate_pct == 0.0 for r in results)

    results = ev_engine.compute_ev_table(estimate, token_map, fee_rate_pct=0.02, quotes=quotes)
    assert all(r.fee_rate_pct == 0.02 for r in results)
