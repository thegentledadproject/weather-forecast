"""
Which side of the book each caller reads.

The bug these pin: every entry-side price came from the BID while a buy
fills at the ASK, so raw_edge was overstated by the spread -- 0.02 on a
live 0.29/0.31 book, against a MIN_ABS_RAW_EDGE of 0.03. Sizing and
slippage had always walked the asks, so the module was contradicting
itself about which side it was on.

The assertions are deliberately about DIRECTION, not values: an entry must
never be priced better than an exit on the same book. A regression that
swapped the two would still return plausible-looking numbers, and only a
directional check catches it.
"""

import pytest

from clients import market_client


BID = 0.29
ASK = 0.31


@pytest.fixture
def stub_quotes(monkeypatch):
    """
    Stub only the HTTP layer, keyed by the API's own `side` parameter, so
    the wrappers' choice of parameter is what is actually under test.

    Note the inversion this encodes: the API's `side=buy` returns the BID.
    If a future refactor "corrects" that to the intuitive reading, these
    tests fail -- which is the point.
    """
    calls = []

    def _fetch(token_id, book_side, timeout=10):
        calls.append(book_side)
        return {"buy": BID, "sell": ASK}[book_side]

    monkeypatch.setattr(market_client, "_fetch_quote", _fetch)
    return calls


def test_bid_and_ask_read_the_sides_they_name(stub_quotes):
    assert market_client.get_token_bid("TOK") == BID
    assert market_client.get_token_ask("TOK") == ASK
    # The API parameter is the opposite of the word it uses.
    assert stub_quotes == ["buy", "sell"]


def test_entries_pay_the_ask(stub_quotes):
    assert market_client.get_entry_price_for_side("TOK", "YES") == ASK


def test_open_positions_are_marked_at_the_bid(stub_quotes):
    assert market_client.get_current_price_for_side("TOK", "YES") == BID


def test_an_entry_is_never_priced_better_than_an_exit(stub_quotes):
    """
    The invariant the whole fix exists to preserve. Buying costs at least
    what selling returns; any implementation where that inverts is
    manufacturing edge out of the spread.
    """
    entry = market_client.get_entry_price_for_side("TOK", "YES")
    exit_ = market_client.get_current_price_for_side("TOK", "YES")
    assert entry >= exit_


def test_the_spread_is_charged_to_the_entry(stub_quotes):
    """
    Pins the magnitude, not just the direction: the correction must move
    the entry price by the full spread. At MIN_ABS_RAW_EDGE = 0.03 a 0.02
    spread is two thirds of the minimum edge, which is why this mattered.
    """
    import config

    entry = market_client.get_entry_price_for_side("TOK", "YES")
    exit_ = market_client.get_current_price_for_side("TOK", "YES")
    spread = entry - exit_

    assert spread == pytest.approx(ASK - BID)
    assert spread > config.MIN_ABS_RAW_EDGE / 2, (
        "this fixture's spread should be large enough to matter against "
        "the minimum edge gate, or the test is not pinning anything"
    )


def test_edge_shrinks_by_the_spread_when_priced_correctly(stub_quotes):
    """
    The end-to-end consequence, stated in the units the gate uses: a model
    probability that cleared MIN_ABS_RAW_EDGE against the bid may not
    clear it against the ask. That is the approval-rate drop, and it is
    the correction working.
    """
    import config

    model_prob = 0.33
    edge_on_bid = model_prob - market_client.get_current_price_for_side("TOK", "YES")
    edge_on_ask = model_prob - market_client.get_entry_price_for_side("TOK", "YES")

    assert edge_on_bid > config.MIN_ABS_RAW_EDGE      # would have been approved
    assert edge_on_ask < config.MIN_ABS_RAW_EDGE      # correctly rejected now


def test_missing_quote_fails_soft_on_both_sides(monkeypatch):
    monkeypatch.setattr(market_client, "_fetch_quote", lambda t, s, timeout=10: None)

    assert market_client.get_entry_price_for_side("TOK", "YES") is None
    assert market_client.get_current_price_for_side("TOK", "YES") is None


def test_ev_engine_prices_entries_off_the_ask(monkeypatch, stub_quotes):
    """
    The call site that actually carried the bug: fetch_market_quotes()
    feeds compute_ev_table -> raw_edge -> EntryDecision.entry_price -> the
    limit price on the order.
    """
    import ev_engine

    token_map = {32: {"yes_token_id": "Y", "no_token_id": "N"}}
    quotes = ev_engine.fetch_market_quotes(token_map)

    assert quotes[32].yes_price == ASK
    assert quotes[32].no_price == ASK
    assert "buy" not in stub_quotes, "entry pricing read the bid side"
