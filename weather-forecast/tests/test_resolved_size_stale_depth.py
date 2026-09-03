"""
_resolved_size_ok() re-validates the size-dependent gates at the notional that
will actually be submitted -- but only when the exchange minimum MOVED the
size. Two holes follow from that.

  * It returns immediately when the resolved notional equals the requested
    one, so an order whose size never moved is submitted having re-validated
    nothing at all.

  * Its depth ceiling tests `decision.available_depth_usd`, the fetch
    evaluate_entry made when the candidate was sized. Only slippage is re-read
    live. The book can thin between sizing and submission -- and on these
    markets it does; a 2026-09-02 probe found 9 of 11 open positions sitting
    on a book with zero bids -- so the one gate that reads a stale number is
    the one measuring the thing that moves.

These tests pin both: every gate is re-read at submission time, whether or not
the exchange changed the size.
"""
from datetime import date

import pytest

import config
import executor
from clients import market_client, wallet_client
from models import EntryDecision


def _decision(size_usd=1.00, depth_at_sizing=1000.0, token_id="TOK"):
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 3), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=size_usd, available_depth_usd=depth_at_sizing,
        slippage_at_size_pct=0.01, net_ev_at_size=0.30,
        approved=True, reason="test", station_maturity="mature",
        entry_price=0.30, token_id=token_id,
    )


def _spec(notional, price=0.40, shares=5.0):
    return wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=price,
        size_shares=shares, notional_usd=notional, expected_price=price,
    )


def test_unchanged_size_is_refused_when_the_book_thinned_since_sizing(monkeypatch):
    """
    The acceptance case. Sizing saw $1000 of depth; by submission the book
    holds $2.00. The order never changed size, so today nothing looks again.
    """
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 2.00)

    ok, note = executor._resolved_size_ok(_spec(1.00), _decision(size_usd=1.00))

    assert not ok
    assert "visible depth" in note


def test_unchanged_size_still_re_reads_slippage(monkeypatch):
    """Slippage was always re-read live -- but only on the path the early
    return skipped."""
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage",
                        lambda t, s: config.MAX_ACCEPTABLE_SLIPPAGE_PCT + 0.01)

    ok, note = executor._resolved_size_ok(_spec(1.00), _decision(size_usd=1.00))

    assert not ok
    assert "hard gate" in note


def test_unreadable_depth_refuses_rather_than_assuming(monkeypatch):
    """
    get_available_depth_usd documents None as "unknown depth", not "zero
    depth", and entry_manager already skips on it. A gate that cannot be
    evaluated has not been passed.
    """
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: None)

    ok, note = executor._resolved_size_ok(_spec(1.00), _decision(size_usd=1.00))

    assert not ok
    assert "unreadable" in note


def test_the_note_says_whether_the_exchange_moved_the_size(monkeypatch):
    """
    Both paths now run the same checks, so the note is the only thing left
    that distinguishes them in the journal.
    """
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 1000.0)

    _, unchanged = executor._resolved_size_ok(_spec(1.00), _decision(size_usd=1.00))
    _, upsized = executor._resolved_size_ok(_spec(3.75), _decision(size_usd=1.00))

    assert "size unchanged" in unchanged
    assert "by the exchange minimum" in upsized
