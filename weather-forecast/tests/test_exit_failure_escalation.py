"""
An unfilled FOK sell prints one line and returns; the next cycle retries. No
counter, no escalation -- unlike _note_price_failure and
_note_live_close_refused, which both escalate on exactly this shape of
repeated failure.

Exit failure correlates with a thin book, so failures cluster rather than
arriving alone: a 2026-09-02 probe found 9 of 11 open positions sitting on a
book with ZERO bids. A cluster currently produces nothing but repetition.
"""
from datetime import date

import pytest

import executor
from models import Position


@pytest.fixture(autouse=True)
def _clear():
    executor._consecutive_exit_failures.clear()
    yield
    executor._consecutive_exit_failures.clear()


def _pos(position_id="p1", shares=5.0, price=0.30):
    return Position(
        position_id=position_id, station_icao="WSSS", target_date=date(2026, 9, 3),
        bucket_c=32, side="YES", entry_price=price, size_usd=1.50,
        entry_time="2026-09-03T00:00:00+00:00", status="open", token_id="TOK",
        is_paper=False, size_shares=shares, execution_mode="live",
    )


def test_three_consecutive_failures_escalate_exactly_once(capsys):
    """
    A single killed FOK on a momentarily thin book is routine; three in a row
    is not. The escalation is the point at which it stops being routine, so it
    must fire once -- not on every subsequent cycle forever.
    """
    pos = _pos()
    for price in (0.28, 0.27, 0.25):
        executor._note_exit_failure(pos, limit_price=price, error="killed unfilled")

    out = capsys.readouterr().out

    assert out.count("[ACTION NEEDED]") == 1
    assert executor._consecutive_exit_failures["p1"] == 3


def test_a_fill_resets_the_streak():
    pos = _pos()
    executor._note_exit_failure(pos, limit_price=0.28, error="killed unfilled")
    assert executor._consecutive_exit_failures["p1"] == 1

    executor.forget_exit_failures("p1")

    assert "p1" not in executor._consecutive_exit_failures


def test_the_escalation_carries_the_last_three_limit_prices(capsys):
    """
    An operator needs to see whether the book is moving away or is simply
    empty, and the attempted prices are what distinguishes those.
    """
    pos = _pos()
    for price in (0.28, 0.27, 0.25):
        executor._note_exit_failure(pos, limit_price=price, error="killed unfilled")

    out = capsys.readouterr().out

    for price_text in ("0.2800", "0.2700", "0.2500"):
        assert price_text in out, f"escalation is missing the {price_text} attempt"


def test_recording_a_close_clears_the_streak(monkeypatch):
    """
    The wiring, not the helper. A closed position cannot fail an exit again,
    so the streak must not outlive it -- and _record() is the one place every
    mode's close is written, which makes it the only spot that covers a fill,
    a resolution and a manual close alike.
    """
    import storage
    from models import ExitDecision

    monkeypatch.setattr(storage, "close_position", lambda **kw: None)
    monkeypatch.setitem(executor.EXECUTION_MODE, "WSSS", "paper")

    pos = _pos()
    pos.is_paper = True
    pos.execution_mode = "paper"
    executor._note_exit_failure(pos, limit_price=0.28, error="killed unfilled")
    assert executor._consecutive_exit_failures["p1"] == 1

    executor.close_position(pos, ExitDecision(
        position_id="p1", should_exit=True, reason="stop_loss",
        current_price=0.25, pnl_pct=-0.17,
    ))

    assert "p1" not in executor._consecutive_exit_failures


def test_a_single_failure_stays_a_routine_line(capsys):
    pos = _pos()
    executor._note_exit_failure(pos, limit_price=0.28, error="killed unfilled")

    out = capsys.readouterr().out

    assert "[ACTION NEEDED]" not in out
    assert "did not fill" in out.lower()
