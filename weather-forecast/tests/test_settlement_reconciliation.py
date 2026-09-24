"""
Gap 2 of the 2026-09-24 plan gap audit: settlement reconciliation.

(b) a second close must not rewrite the first one's exit price.
(c) the winner WE book is cross-checked against what the exchange says; a
    disagreement is flagged (never acted on), agreement and missing exchange
    data are silent.
"""
from datetime import date, timedelta

import pytest

import alerts
import config
import executor
import position_manager
import storage
from models import ExitDecision, ObservedReading, Position

STATION = "RKSI"
SETTLED_DAY = date.today() - timedelta(days=2)


def _pos(position_id="RKSI:recon:1", bucket_c=33, side="YES", mode="paper") -> Position:
    return Position(
        position_id=position_id, station_icao=STATION, target_date=SETTLED_DAY,
        bucket_c=bucket_c, side=side, entry_price=0.40, size_usd=2.0,
        entry_time="2026-09-20T00:00:00+00:00", status="open", token_id="tok",
        is_paper=(mode != "live"), execution_mode=mode,
    )


# --------------------------------------------------------------------------
# (b) close only what is still open
# --------------------------------------------------------------------------

def test_a_second_close_changes_nothing_and_says_so():
    pos = _pos(position_id="RKSI:recon:double-close")
    storage.open_position(pos)

    assert storage.close_position(pos.position_id, 1.0, "2026-09-22T00:00:00+00:00",
                                  "closed_resolution", "market_resolved") is True
    assert storage.close_position(pos.position_id, 0.0, "2026-09-23T00:00:00+00:00",
                                  "closed_stop_loss", "late duplicate") is False

    row = [p for p in storage.load_position_history(STATION, limit=1000) if p.position_id == pos.position_id][0]
    assert row.exit_price == 1.0
    assert row.status == "closed_resolution"


def test_executor_warns_on_a_no_op_close(capsys):
    pos = _pos(position_id="RKSI:recon:executor-double")
    storage.open_position(pos)
    decision = ExitDecision(position_id=pos.position_id, should_exit=True,
                            reason="resolution", current_price=1.0, pnl_pct=1.5)

    executor.close_position(pos, decision, status="closed_resolution", exit_reason="market_resolved")
    capsys.readouterr()
    executor.close_position(pos, decision, status="closed_resolution", exit_reason="market_resolved")

    assert "already closed" in capsys.readouterr().out


# --------------------------------------------------------------------------
# (c) cross-check our winner against the exchange
# --------------------------------------------------------------------------

@pytest.fixture
def resolved(monkeypatch):
    """Gamma closed, bounds = config, no recorded settlement, closes captured."""
    st = config.get_station(STATION)
    monkeypatch.setattr(position_manager, "_event_bounds",
                        lambda position, station: (st.bucket_min_c, st.bucket_max_c))
    monkeypatch.setattr(storage, "load_settled_buckets", lambda icao: {})
    closed, sent = [], []
    monkeypatch.setattr(
        position_manager.executor, "close_position",
        lambda position, decision, status=None, exit_reason=None:
            closed.append((decision.current_price, exit_reason)),
    )
    monkeypatch.setattr(alerts, "send", lambda *a, **kw: sent.append(a) or True)

    def reading(temp_c):
        monkeypatch.setattr(storage, "load_observations_since", lambda icao, since: [
            ObservedReading(station_icao=STATION, target_date=SETTLED_DAY, max_temp_c=temp_c,
                            source=st.resolution_grade_source)])
    return closed, sent, reading


def test_a_disagreement_is_flagged_but_our_answer_stands(resolved, capsys):
    closed, sent, reading = resolved
    reading(31.0)  # our record: bucket 31 won, so 33 YES lost

    position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)  # exchange: 33 YES won

    price, reason = closed[0]
    assert price == 0.0, "the flag must not change which answer is used"
    assert "SETTLEMENT_MISMATCH" in reason
    assert "SETTLEMENT_MISMATCH" in capsys.readouterr().out
    assert len(sent) == 1


def test_agreement_is_silent(resolved, capsys):
    closed, sent, reading = resolved
    reading(33.0)

    position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)

    assert closed[0] == (1.0, "market_resolved")
    assert "SETTLEMENT_MISMATCH" not in capsys.readouterr().out
    assert sent == []


@pytest.mark.parametrize("book_quote", [None, 0.5])
def test_no_exchange_answer_is_not_a_mismatch(resolved, book_quote):
    """No book and no recorded settlement, or an indecisive quote: nothing to compare."""
    closed, sent, reading = resolved
    reading(31.0)

    position_manager._close_from_settlement_source(
        _pos(bucket_c=33), gamma_closed=True, book_quote=book_quote)

    assert closed[0] == (0.0, "market_resolved")
    assert sent == []


def test_the_recorded_exchange_settlement_is_checked_when_the_book_is_gone(resolved, monkeypatch):
    """After settlement the book is unseeded; the recorded winner is the exchange's answer."""
    closed, sent, reading = resolved
    reading(31.0)
    monkeypatch.setattr(storage, "load_settled_buckets",
                        lambda icao: {SETTLED_DAY: (33, 23, 33, "test", 11)})

    position_manager._close_from_settlement_source(_pos(bucket_c=33), gamma_closed=True)

    price, reason = closed[0]
    assert price == 0.0
    assert "SETTLEMENT_MISMATCH" in reason
    assert len(sent) == 1
