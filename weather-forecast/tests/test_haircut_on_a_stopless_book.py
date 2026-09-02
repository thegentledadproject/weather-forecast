"""
tests/test_haircut_on_a_stopless_book.py

entry_manager.gap_risk_haircut() on a book that has no stop.

THE INCOHERENCE THIS FIXES. The haircut scales a position so that "a stop-out
costs what the Kelly size was actually chosen against": nominal / (nominal +
gap + spread), where nominal is STOP_LOSS_PCT x risk_unit. Since
config.HOLD_TO_SETTLEMENT_MODES shipped (2026-09-02) the paper book HAS NO
STOP, so every term in that ratio describes a rule that cannot fire there.
The function already returns 1.0 for lottery entries on exactly this
reasoning -- "no stop at all, max loss is the stake, and no amount of gapping
changes it" -- and that reasoning now covers the whole paper book.

WHY THE DEFAULT DOES NOT CHANGE SIZING. Returning 1.0 is arithmetically
right and would multiply paper positions by 1.4x at entry 0.50, 2.0x at 0.20
and 2.25x at 0.16. That is not a cleanup, it is a trading decision, and the
conservatism it would remove is doing real work for a reason that is not the
one in the docstring: the model is measurably overconfident (mean model_prob
0.432 against a 0.344 realised win rate; Brier 0.1930 against the market's
0.1842), and Kelly's f* = edge/(1-p) takes the model's probability at face
value. So the number is retained by default and the switch that removes it is
explicit, one line, and named for what it actually decides.

What is FIXED is that the code now knows which regime it is in and says so,
instead of computing a stop's economics for a book with no stop.
"""

from contextlib import contextmanager

import pytest

import config
import entry_manager


BID = 0.38          # a 0.02 spread under a 0.40 ask -- the measured median
ASK = 0.40


@contextmanager
def _pure_kelly(on: bool):
    old = config.SIZE_STOPLESS_BOOKS_ON_PURE_KELLY
    config.SIZE_STOPLESS_BOOKS_ON_PURE_KELLY = on
    try:
        yield
    finally:
        config.SIZE_STOPLESS_BOOKS_ON_PURE_KELLY = old


def test_a_stopped_book_is_completely_unchanged():
    """The live book still has both exits; nothing here may touch its sizing."""
    with_stop = entry_manager.gap_risk_haircut(ASK, "WSSS", BID, has_stop=True)
    assert with_stop == entry_manager.gap_risk_haircut(ASK, "WSSS", BID)
    assert 0.0 < with_stop < 1.0


def test_the_default_leaves_a_stopless_book_sized_exactly_as_before():
    """
    The whole point of the default: this commit changes no position size.
    A silent 1.4x-2.25x increase on the live-running paper book is not a
    thing a coherence fix gets to do.
    """
    with _pure_kelly(False):
        assert entry_manager.gap_risk_haircut(ASK, "WSSS", BID, has_stop=False) == \
               entry_manager.gap_risk_haircut(ASK, "WSSS", BID, has_stop=True)


def test_the_switch_sizes_a_stopless_book_on_pure_kelly():
    with _pure_kelly(True):
        assert entry_manager.gap_risk_haircut(ASK, "WSSS", BID, has_stop=False) == 1.0


def test_the_switch_does_not_touch_a_book_that_still_has_a_stop():
    """It is keyed on the ABSENCE of a stop, not on being a global override."""
    with _pure_kelly(True):
        assert entry_manager.gap_risk_haircut(ASK, "WSSS", BID, has_stop=True) < 1.0


def test_lottery_entries_stay_exempt_either_way():
    """The pre-existing carve-out, for the same reason, must not regress."""
    cheap = config.LOTTERY_PRICE_THRESHOLD - 0.01
    for has_stop in (True, False):
        for on in (True, False):
            with _pure_kelly(on):
                assert entry_manager.gap_risk_haircut(cheap, "WSSS", cheap - 0.01,
                                                      has_stop=has_stop) == 1.0


def test_shipped_default_is_off():
    assert config.SIZE_STOPLESS_BOOKS_ON_PURE_KELLY is False


def test_sizing_asks_whether_this_candidates_book_has_a_stop(monkeypatch):
    """
    The wiring. A station whose execution mode is in HOLD_TO_SETTLEMENT_MODES
    must be sized as stopless; anything else keeps the stop's economics.
    """
    seen = []
    real = entry_manager.gap_risk_haircut
    monkeypatch.setattr(entry_manager, "gap_risk_haircut",
                        lambda *a, **k: seen.append(k.get("has_stop")) or real(*a))
    monkeypatch.setattr(config, "HOLD_TO_SETTLEMENT_MODES", ("paper",))

    monkeypatch.setattr(entry_manager, "_execution_mode", lambda icao: "paper")
    assert entry_manager._book_has_stop("WSSS") is False
    monkeypatch.setattr(entry_manager, "_execution_mode", lambda icao: "live")
    assert entry_manager._book_has_stop("WSSS") is True
    monkeypatch.setattr(entry_manager, "_execution_mode", lambda icao: "simulation")
    assert entry_manager._book_has_stop("WSSS") is True, \
        "simulation rehearses live and keeps both exits"
