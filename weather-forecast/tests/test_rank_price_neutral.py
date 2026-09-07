"""
tests/test_rank_price_neutral.py

WHAT IS BEING FIXED. best_opportunities() ranked on net_ev_per_dollar =
(raw_edge / price) - costs. Dividing by price makes the number explode as
price -> 0, so the same absolute disagreement scores ~10x higher on a 4c
ticket than on a 40c one and cheap tickets sort to the top mechanically.
Measured on the live book: the HIGHEST net_ev quintile returns -31.7% held to
settlement while the second-LOWEST returns +30.6%, i.e. the key is not merely
uninformative but inverted, and its top quintile has a mean price of 0.114.

WHAT THIS DOES NOT CLAIM TO FIX. It cannot rescue the pathological case in
[[lottery-band-entry-defect]] -- a 0.04 ticket claiming 0.18 of edge against a
0.35 ticket claiming 0.10 -- because there the cheap one genuinely claims MORE
absolute edge, and no function rising in edge and falling in price can invert
that pair. That is a model-calibration problem at low prices, not a ranking
one. What this removes is the MECHANICAL cheapness tilt: equal edges now score
equally regardless of price.

SCOPE, and it is deliberately narrow: the ORDER only. The admission bar stays
net_ev_per_dollar >= min_net_ev, byte for byte, because changing which
candidates surface is a trading change and this project's own rule is that
entry-gate changes need replay evidence first (config.ENTRY_PRICE_BLOCK_BAND
ships off for exactly that reason). Today the order is cosmetic --
apply_portfolio_budget scales every approved leg proportionally and nothing
truncates the ranked list -- so this is a correctness fix that also stops a
future truncation from silently inheriting the bias.
"""

from datetime import date

import ev_engine
from models import EVResult


def _cand(price, model_prob, slippage=0.0, fee=0.0, bucket=30):
    """An EVResult with net_ev_per_dollar computed exactly as ev_engine does."""
    raw_edge = model_prob - price
    net_ev = (raw_edge / price) - slippage - fee
    return EVResult(
        station_icao="WSSS", target_date=date(2026, 9, 7), bucket_c=bucket,
        side="YES", model_prob=model_prob, market_price=price,
        raw_edge=raw_edge, estimated_slippage_pct=slippage, fee_rate_pct=fee,
        net_ev_per_dollar=net_ev,
    )


def test_net_ev_per_share_is_the_same_quantity_undivided():
    """net_ev_per_dollar x price = raw_edge - price*costs = dollars per share."""
    c = _cand(price=0.40, model_prob=0.50, slippage=0.01, fee=0.02)
    got = ev_engine.net_ev_per_share(c)
    assert abs(got - (c.raw_edge - 0.40 * 0.03)) < 1e-12
    assert abs(got - c.net_ev_per_dollar * 0.40) < 1e-12


def test_bigger_absolute_edge_outranks_a_cheaper_smaller_one():
    """
    The behaviour that was backwards. A carries 10c of edge at 0.50; B carries
    5c at 0.05. Dividing by price scores B at 1.00 against A's 0.20 and puts
    the smaller disagreement first.
    """
    a = _cand(price=0.50, model_prob=0.60, bucket=30)   # raw_edge 0.10
    b = _cand(price=0.05, model_prob=0.10, bucket=31)   # raw_edge 0.05
    assert b.net_ev_per_dollar > a.net_ev_per_dollar    # the old key preferred B
    ranked = ev_engine.best_opportunities([b, a], min_net_ev=0.0, min_price=0.0)
    assert [r.bucket_c for r in ranked] == [30, 31]


def test_equal_absolute_edge_ranks_equally_regardless_of_price():
    """The mechanical cheapness tilt, stated directly."""
    cheap = _cand(price=0.05, model_prob=0.08, bucket=31)    # raw_edge 0.03
    dear = _cand(price=0.50, model_prob=0.53, bucket=30)     # raw_edge 0.03
    assert cheap.net_ev_per_dollar > 9 * dear.net_ev_per_dollar   # old: 0.60 vs 0.06
    assert abs(ev_engine.net_ev_per_share(cheap)
               - ev_engine.net_ev_per_share(dear)) < 1e-12


def test_the_admission_bar_is_unchanged():
    """
    Sort-only. A candidate whose RATIO clears the bar must still be admitted
    even though its dollars-per-share is tiny, and one whose ratio misses must
    still be refused. This is what keeps the change non-trading.
    """
    tiny_but_clears = _cand(price=0.04, model_prob=0.05)   # ratio 0.25, $/share 0.01
    assert tiny_but_clears.net_ev_per_dollar >= 0.15
    assert ev_engine.best_opportunities([tiny_but_clears], min_net_ev=0.15,
                                        min_price=0.0) == [tiny_but_clears]
    misses = _cand(price=0.50, model_prob=0.55)            # ratio 0.10
    assert misses.net_ev_per_dollar < 0.15
    assert ev_engine.best_opportunities([misses], min_net_ev=0.15, min_price=0.0) == []


def test_unpriced_candidates_are_still_dropped_not_crashed_on():
    c = _cand(price=0.30, model_prob=0.40)
    c.net_ev_per_dollar = None
    c.market_price = None
    assert ev_engine.best_opportunities([c], min_net_ev=0.0, min_price=0.0) == []
