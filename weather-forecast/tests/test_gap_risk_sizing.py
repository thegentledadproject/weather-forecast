"""
tests/test_gap_risk_sizing.py

Pins the gap-risk haircut (2026-08-14).

WHAT IT IS FOR. Kelly sizes against the loss the stop defines. But a stop
does not fill at the stop -- measured over price_snapshots at the 10-20
minute horizon the exit windows decide on, the downward move past a
trigger is ~0.010 at the median and 0.040 at p90. So the loss actually
taken on a stop-out is the stop distance PLUS that gap, and a size chosen
against the stop distance alone is systematically too large.

WHY SIZING AND NOT A SCHEDULE OR ORDER-TYPE FIX. Cadence was already
tried: 120/60/180 -> 15/15/30 min (6ed099e) changed nothing, because the
fills are single jumps with no trades in between. And a resting sell limit
at the stop is not a stop -- below the market it is immediately marketable
and fills at once, and in a falling market nobody lifts it. Sizing is the
only lever that actually prices the gap.
"""
import pytest

import config
import entry_manager
import risk_manager
from backtest import entry_sim


class TestHaircutShape:
    @pytest.mark.parametrize("entry_price", [0.16, 0.20, 0.30, 0.42, 0.50, 0.70, 0.75])
    def test_haircut_is_nominal_over_nominal_plus_gap(self, entry_price):
        """The haircut is exactly the ratio of intended risk to real risk."""
        nominal = config.STOP_LOSS_PCT * risk_manager.risk_unit(entry_price)
        expected = nominal / (nominal + config.EXPECTED_STOP_GAP)
        assert entry_manager.gap_risk_haircut(entry_price) == pytest.approx(expected)

    @pytest.mark.parametrize("entry_price", [0.16, 0.30, 0.50, 0.75])
    def test_haircut_always_reduces_size(self, entry_price):
        assert 0.0 < entry_manager.gap_risk_haircut(entry_price) < 1.0

    def test_haircut_bites_hardest_on_cheap_entries(self):
        """
        Four cents of slippage is most of the risk against a 0.16 stop and a
        rounding error against a 0.50 one. The curve must reflect that, or
        it is not pricing the gap -- it is just a flat size cut.
        """
        assert entry_manager.gap_risk_haircut(0.16) < entry_manager.gap_risk_haircut(0.30)
        assert entry_manager.gap_risk_haircut(0.30) < entry_manager.gap_risk_haircut(0.50)

    def test_haircut_is_symmetric_about_the_risk_unit_peak(self):
        """
        The stop distance is a fraction of min(entry, 1-entry), so 0.30 and
        0.70 carry the same nominal risk and must take the same haircut.
        A haircut keyed off entry price alone would fail this.
        """
        assert entry_manager.gap_risk_haircut(0.30) == pytest.approx(
            entry_manager.gap_risk_haircut(0.70)
        )


class TestExemptions:
    @pytest.mark.parametrize("entry_price", [0.01, 0.04, 0.10, 0.149])
    def test_lottery_entries_take_no_haircut(self, entry_price):
        """
        A lottery ticket carries no stop at all, so there is no gap to
        allow for -- its maximum loss is the stake, accepted at entry.
        Haircutting it would shrink the position for a risk it cannot run.
        """
        assert entry_price < config.LOTTERY_PRICE_THRESHOLD
        assert entry_manager.gap_risk_haircut(entry_price) == 1.0

    @pytest.mark.parametrize("entry_price", [None, 0.0, -0.1])
    def test_degenerate_prices_take_no_haircut(self, entry_price):
        assert entry_manager.gap_risk_haircut(entry_price) == 1.0

    def test_the_lottery_boundary_is_a_cliff_and_that_is_known(self):
        """
        Documents rather than defends: 0.149 takes full size with no stop,
        0.151 takes a stop and roughly half size. The discontinuity lives in
        LOTTERY_PRICE_THRESHOLD's risk model, not in the haircut -- but it is
        real money either side of one cent, so it is pinned here to stay
        visible if the threshold is ever retuned.
        """
        below = entry_manager.gap_risk_haircut(config.LOTTERY_PRICE_THRESHOLD - 0.001)
        above = entry_manager.gap_risk_haircut(config.LOTTERY_PRICE_THRESHOLD + 0.001)
        assert below == 1.0
        assert above < 0.60


class TestPerStationOverride:
    def test_default_applies_when_no_override(self):
        assert config.stop_gap_allowance("WSSS") == config.EXPECTED_STOP_GAP
        assert config.stop_gap_allowance(None) == config.EXPECTED_STOP_GAP

    def test_override_is_honoured(self, monkeypatch):
        monkeypatch.setattr(config, "STOP_GAP_BY_STATION", {"ZSPD": 0.06})
        assert config.stop_gap_allowance("ZSPD") == 0.06
        assert config.stop_gap_allowance("WSSS") == config.EXPECTED_STOP_GAP
        # A gappier station must be sized smaller than a calmer one.
        assert entry_manager.gap_risk_haircut(0.30, "ZSPD") < entry_manager.gap_risk_haircut(0.30, "WSSS")

    def test_a_zero_gap_disables_the_haircut(self, monkeypatch):
        """The escape hatch: setting the allowance to 0 restores old sizing."""
        monkeypatch.setattr(config, "EXPECTED_STOP_GAP", 0.0)
        monkeypatch.setattr(config, "STOP_GAP_BY_STATION", {})
        assert entry_manager.gap_risk_haircut(0.30) == 1.0


class TestLiveReplayParity:
    def test_entry_sim_uses_the_live_function_object(self):
        """
        Imported, not reimplemented. If the replay ever grows its own copy
        this fails, which is the whole point -- a backtest that sized
        without the haircut would report a strategy nobody is running.
        """
        assert entry_sim.gap_risk_haircut is entry_manager.gap_risk_haircut

    @pytest.mark.parametrize("entry_price", [0.16, 0.30, 0.50, 0.70])
    def test_both_paths_agree_on_the_haircut(self, entry_price):
        assert entry_sim.gap_risk_haircut(entry_price, "WSSS") == entry_manager.gap_risk_haircut(
            entry_price, "WSSS"
        )
