"""
tests/test_opposite_side_lock.py

The per-bucket cap must count BOTH sides of a bucket, not just the side
being bought.

THE FAILURE THIS FIXES, measured on the box 2026-08-25. RPLL's book on
target 2026-08-24 held both sides of bucket 32 at once:

    08-23 21:41Z   RPLL 32 YES @0.34  $4.54   model P(32) = 0.4234
    08-23 22:20Z   RPLL 32 NO  @0.64  $14.04  model P(32) = 0.2215

Both open together from 22:20Z until the YES took profit at 02:00Z. The
model's P(32) had halved in 39 minutes and the system acted on both
readings without noticing they contradicted each other.

`count_open_positions_for_bucket` filtered on `p.side.upper() == side`, so
veto 0b -- which exists to stop one bet being sized up by accident -- never
saw the YES when the NO came through. It is the same bet, on the same
bucket, with the sign flipped.

The overlapping 13.35 shares cost 0.34 + 0.64 = $0.98 for a guaranteed
$1.00 payout, which the taker fee then eats. RPLL settled 32: the YES was
right and was closed early for +$1.44, the NO was wrong and rode to
-$14.04.

WHY BLOCK THE NEW ENTRY rather than close the old one: the conservative
direction. Refusing an entry can only make trading less likely, and a model
that has changed its mind should be exiting the position it no longer
believes in through the exit path, not opening its own hedge through the
entry path.
"""
from datetime import date

import pytest

import config
import entry_manager
import storage
from clients import market_client
from models import EVResult, Position

STATION = "RPLL"
TARGET = date(2026, 8, 24)
BUCKET = 32


def _ev(side="NO", price=0.64, prob=0.7785, bucket_c=BUCKET, target_date=TARGET):
    """
    The real RPLL 32 NO candidate. Edge is kept at its measured 0.1385 so
    veto 0a (edge plausibility) passes and the candidate actually reaches
    the per-bucket cap this file is about.
    """
    return EVResult(
        station_icao=STATION, target_date=target_date, bucket_c=bucket_c, side=side,
        model_prob=prob, market_price=price, raw_edge=prob - price,
        estimated_slippage_pct=0.01, fee_rate_pct=0.0,
        net_ev_per_dollar=(prob - price) / price - 0.01, spread_source="ensemble",
    )


def _open(side="YES", bucket_c=BUCKET, target_date=TARGET, is_paper=True):
    """The RPLL 32 YES that was already on the book."""
    return Position(
        position_id=f"{STATION}:{target_date}:{bucket_c}:{side}:x",
        station_icao=STATION, target_date=target_date, bucket_c=bucket_c, side=side,
        entry_price=0.34, size_usd=4.54, entry_time="2026-08-23T21:41:00+00:00",
        status="open", token_id="tok", is_paper=is_paper, execution_mode="paper",
    )


def _book(monkeypatch, positions):
    """Present these as the open book, honouring the is_paper scoping."""
    def _load(station_icao=None, is_paper=None):
        return [
            p for p in positions
            if (station_icao is None or p.station_icao == station_icao)
            and (is_paper is None or p.is_paper == is_paper)
        ]
    monkeypatch.setattr(storage, "load_open_positions", _load)
    monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])


def _offline(monkeypatch):
    """Keep the post-veto sizing path off the network."""
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token_id: 1000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token_id, size_usd: 0.01)


def _decide(ev):
    return entry_manager.evaluate_entry(ev, "TOK", min_net_ev=0.15)


class TestTheOppositeSideIsTheSameBet:
    def test_an_open_YES_blocks_a_NO_on_the_same_bucket(self, monkeypatch):
        """The production case, stated directly."""
        _offline(monkeypatch)
        _book(monkeypatch, [_open(side="YES")])

        decision = _decide(_ev(side="NO"))

        assert decision.approved is False
        assert "opposite side" in decision.reason.lower()

    def test_an_open_NO_blocks_a_YES_on_the_same_bucket(self, monkeypatch):
        """Symmetric -- neither side is privileged."""
        _offline(monkeypatch)
        _book(monkeypatch, [_open(side="NO")])

        decision = _decide(_ev(side="YES", price=0.34, prob=0.4234))

        assert decision.approved is False
        assert "opposite side" in decision.reason.lower()


class TestItStaysNarrow:
    """
    A veto that fires too widely costs real trades. Each of these is a
    position that looks adjacent but is a genuinely different bet.
    """

    def test_a_different_bucket_does_not_block(self, monkeypatch):
        _offline(monkeypatch)
        _book(monkeypatch, [_open(side="YES", bucket_c=31)])

        decision = _decide(_ev(side="NO"))

        assert "opposite side" not in decision.reason.lower()

    def test_a_different_target_date_does_not_block(self, monkeypatch):
        _offline(monkeypatch)
        _book(monkeypatch, [_open(side="YES", target_date=date(2026, 8, 23))])

        decision = _decide(_ev(side="NO"))

        assert "opposite side" not in decision.reason.lower()

    def test_an_empty_book_does_not_block(self, monkeypatch):
        _offline(monkeypatch)
        _book(monkeypatch, [])

        decision = _decide(_ev(side="NO"))

        assert "opposite side" not in decision.reason.lower()

    def test_a_PAPER_position_does_not_block_a_REAL_candidate(self, monkeypatch):
        """
        Paper and real are separate books. The existing cap scopes by track
        for exactly this reason -- letting a paper leg block a real one
        would silently halve real exposure the moment paper mode is used.
        """
        _offline(monkeypatch)
        _book(monkeypatch, [_open(side="YES", is_paper=True)])
        monkeypatch.setattr(entry_manager, "_candidate_is_paper", lambda icao: False)

        decision = _decide(_ev(side="NO"))

        assert "opposite side" not in decision.reason.lower()


class TestItFailsClosed:
    def test_an_unreadable_book_refuses_rather_than_assuming_zero(self, monkeypatch):
        """
        Same rule as the same-side cap: "cannot tell" is not "nothing is
        open". Refusing costs a trade; assuming costs a contradicted book.

        The storage read FAILS ONLY ON THE SECOND CALL, so veto 0b sees a
        clean book and this lock is the one that has to refuse. Failing the
        first call instead would be answered by 0b and would test nothing
        about the code this file exists for.
        """
        calls = []

        def _boom_on_second(**kw):
            calls.append(kw)
            if len(calls) >= 2:
                raise RuntimeError("database is locked")
            return []

        _offline(monkeypatch)
        monkeypatch.setattr(storage, "load_open_positions", _boom_on_second)
        monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])

        decision = _decide(_ev(side="NO"))

        assert len(calls) >= 2, "veto 0b answered this -- the lock was never reached"
        assert decision.approved is False
        assert "opposite-side lock" in decision.reason.lower()


class TestTheSameSideCapStillWorks:
    def test_an_open_position_on_the_SAME_side_still_vetoes(self, monkeypatch):
        """Regression: veto 0b's original job is untouched."""
        _offline(monkeypatch)
        _book(monkeypatch, [_open(side="NO")])

        decision = _decide(_ev(side="NO"))

        assert decision.approved is False
        assert "opposite side" not in decision.reason.lower()

    def test_the_counter_itself_still_counts_only_its_own_side(self, monkeypatch):
        """
        count_open_positions_for_bucket keeps its side filter -- the fix is
        a second lookup, not a loosened one, so the cap's own arithmetic
        and its log line stay about the side they name.
        """
        _book(monkeypatch, [_open(side="YES")])

        assert entry_manager.count_open_positions_for_bucket(
            STATION, TARGET, BUCKET, "NO", is_paper=True) == 0
        assert entry_manager.count_open_positions_for_bucket(
            STATION, TARGET, BUCKET, "YES", is_paper=True) == 1
