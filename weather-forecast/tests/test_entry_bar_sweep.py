"""
tests/test_entry_bar_sweep.py

Guards for backtest/entry_bar_sweep.py, and they are shaped by what went
wrong with the other two sweeps on 2026-09-02.

stop_sweep and take_sweep were inert for five days because their replay
cohort was EXEMPT from the rule they varied, and they failed by printing a
table whose every row was identical -- which reads as "this threshold does
not matter" rather than "nothing was measured". The lesson is not "arm the
cohort": it is that a sweep must PROVE its cohort is in the state its answer
assumes, before it scores a single row.

This sweep's premise is the OPPOSITE of stop_sweep's. It varies which
entries are ADMITTED, so it wants every admitted position carried to
settlement -- otherwise the rows differ by exit rules the repo has already
measured as negative (cohort_monitor: entry selection is +18.4% held, the
stop and take turn it into -7.3%) and the entry question is buried under
them. config.HOLD_TO_SETTLEMENT_MODES = ("paper",) already does exactly that
for the replay cohort, so this sweep must leave it ALONE -- and prove it did.
"""

import pytest

import config
import risk_manager
from models import Position
from backtest import entry_bar_sweep


def _replay_position(entry_price: float = 0.40) -> Position:
    """Shaped the way backtest/engine.py builds one -- see test_exit_sweeps_armed.py."""
    return Position(
        position_id="test-1", station_icao="WSSS", target_date="2026-09-07",
        bucket_c=32, side="YES", entry_price=entry_price, size_usd=10.0,
        entry_time="2026-09-07T00:00:00Z", high_water_mark=entry_price,
        is_paper=True,
    )


# --- the contextmanager --------------------------------------------------

def test_bar_context_sets_the_basis_for_its_duration():
    assert config.ENTRY_BAR_BASIS == "ratio"
    with entry_bar_sweep._entry_bar("per_share"):
        assert config.ENTRY_BAR_BASIS == "per_share"
    assert config.ENTRY_BAR_BASIS == "ratio"


def test_bar_context_restores_on_the_raising_path():
    """
    config is process-global and the daemon imports the same module, so a
    leaked basis is a live trading change made by a crashed analysis script.
    """
    with pytest.raises(RuntimeError):
        with entry_bar_sweep._entry_bar("per_share"):
            raise RuntimeError("boom")
    assert config.ENTRY_BAR_BASIS == "ratio"


def test_bar_context_does_not_touch_hold_to_settlement():
    """
    The deliberate opposite of stop_sweep._stop_distance(), which empties
    this tuple. Emptying it here would arm the stop and the take on every
    replayed position and mix the exit rules back into an entry answer.
    """
    before = config.HOLD_TO_SETTLEMENT_MODES
    with entry_bar_sweep._entry_bar("per_share"):
        assert config.HOLD_TO_SETTLEMENT_MODES == before
    assert config.HOLD_TO_SETTLEMENT_MODES == before


# --- the sweep proves its own premise ------------------------------------

def test_hold_probe_passes_on_the_shipped_config():
    """
    The discriminating check, run before any row is scored: a replay-shaped
    position must be held, at a price that would otherwise stop it. Entry
    0.40 -> risk unit 0.40; 0.05 is an 87% loss and stops on any distance.
    """
    assert entry_bar_sweep.assert_cohort_holds() is True
    assert risk_manager.evaluate_exit(
        _replay_position(), current_price=0.05, local_hour=6).reason == "hold"


def test_hold_probe_raises_when_the_cohort_is_not_held(monkeypatch):
    """
    The guard has to be able to fail, or it is decoration. With the modes
    tuple emptied -- exactly what stop_sweep does -- the same position stops,
    and this sweep must refuse to report numbers rather than silently score
    a cohort whose exits are armed.
    """
    monkeypatch.setattr(config, "HOLD_TO_SETTLEMENT_MODES", ())
    with pytest.raises(AssertionError):
        entry_bar_sweep.assert_cohort_holds()


# --- the cells are what they say they are --------------------------------

def test_cells_carry_their_basis_and_are_not_interchangeable():
    """
    A 0.15 ratio and a 0.15 per-share bar are wildly different rules, so a
    cell is (basis, bar) and never a bare number. Guards against a printed
    table whose rows cannot be told apart.
    """
    cells = entry_bar_sweep.parse_cells("ratio:0.15,per_share:0.045")
    assert cells == [("ratio", 0.15), ("per_share", 0.045)]


def test_parse_cells_rejects_an_unknown_basis():
    with pytest.raises(ValueError):
        entry_bar_sweep.parse_cells("ratioo:0.15")


def test_bar_context_refuses_when_a_second_entry_window_is_enabled(monkeypatch):
    """
    config.MARKET_OPEN_WINDOW is prepended by scheduler.determine_window() when
    ENABLE_MARKET_OPEN_WINDOW is on, and it carries its OWN min_net_ev. The
    override below rewrites SCHEDULE_WINDOWS only, so with that flag on the
    replay would run two different bars and print one -- the exact shape of
    failure the other two sweeps had. Ships off today; refuse rather than
    handle a path nothing exercises.
    """
    monkeypatch.setattr(config, "ENABLE_MARKET_OPEN_WINDOW", True)
    with pytest.raises(RuntimeError, match="MARKET_OPEN_WINDOW"):
        with entry_bar_sweep._entry_bar("ratio", 0.15):
            pass
    assert config.ENTRY_BAR_BASIS == "ratio"
