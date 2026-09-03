"""
tests/test_max_attainable_bucket_prob.py

The calibration panel's new column: the MOST probability the model could
ever put in one bucket, given the station's resolved spread (sigma) and
its real bucket width.

    p_max = 2 * Phi(half_width_c / sigma) - 1

This is the spread floor's cap on model confidence made visible -- a
SPREAD_FLOOR_C-clamped sigma silently limits how peaked any bucket
probability can ever be, and until now that limit was only derivable by
hand. Two things must be right for the number to mean anything:

  * sigma is whatever calibration.estimate_std_dev actually returns for
    the station, INCLUDING the floor/clamp -- that clamp is the whole
    point of the column;
  * half_width_c is HALF the station's real bucket width, in Celsius --
    most stations are 1C, but eleven Americas cities are 2F (= 1.111C),
    and hardcoding 0.5 would silently mis-report every one of them.
"""

from datetime import datetime, timezone

import pytest

import calibration
import calibration_panel

NOW = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def fixed_sigma(monkeypatch):
    """
    Pins calibration.estimate_std_dev to a known (sigma, source) pair, so
    the arithmetic can be checked against independently-computed reference
    values instead of whatever the real spread history happens to hold.
    """
    state = {"sigma": 0.70, "source": "measured_error"}

    def _fake(*args, **kwargs):
        return state["sigma"], state["source"]

    monkeypatch.setattr(calibration, "estimate_std_dev", _fake)
    return state


class TestPMaxOnA1CBucket:
    def test_it_reports_p_max_for_the_stations_resolved_sigma(self, fixed_sigma):
        """WSSS is a plain 1C-bucket station -- half_width_c = 0.5."""
        row = calibration_panel.station_row("WSSS", now=NOW)

        mabp = row["max_attainable_prob"]
        assert mabp is not None
        assert mabp["p_max"] == pytest.approx(0.5249, abs=1e-4)
        assert mabp["sigma"] == pytest.approx(0.70)
        assert mabp["source"] == "measured_error"

    @pytest.mark.parametrize("sigma, expected_p_max", [
        (0.70, 0.5249),
        (1.00, 0.3829),
        (1.21, 0.3206),
    ])
    def test_p_max_tracks_sigma(self, fixed_sigma, sigma, expected_p_max):
        fixed_sigma["sigma"] = sigma

        row = calibration_panel.station_row("WSSS", now=NOW)

        assert row["max_attainable_prob"]["p_max"] == pytest.approx(expected_p_max, abs=1e-4)


class TestPMaxOnA2FBucket:
    def test_half_width_is_converted_from_the_stations_real_bucket_not_hardcoded(self, fixed_sigma):
        """
        KLGA lists 2F buckets (1.111C wide), so half_width_c = 5/9 = 0.5556,
        NOT the 0.5 every 1C station uses. Hardcoding 0.5 here would
        silently mis-report every Americas station.

        p_max expected value is 0.5726012, computed at full precision with
        statistics.NormalDist -- the three 1C reference values (0.5249,
        0.3829, 0.3206) reproduce EXACTLY at full precision, which is the
        cross-check that the formula itself is right; the discrepancy is a
        rounding artifact of computing half_width_c to 3dp (0.556) by hand.
        """
        row = calibration_panel.station_row("KLGA", now=NOW)

        mabp = row["max_attainable_prob"]
        assert mabp is not None
        assert mabp["half_width_c"] == pytest.approx(5 / 9, abs=1e-6)
        assert mabp["p_max"] == pytest.approx(0.5726012, abs=1e-6)


class TestAnUnresolvableSpreadIsNoneNotAFabricatedNumber:
    def test_an_unregistered_station_reports_none(self, fixed_sigma):
        """
        No StationConfig means no bucket axis to convert against -- printing
        a number here would be a fabrication, not a measurement.
        """
        row = calibration_panel.station_row("ZZZZ", now=NOW)

        assert row["max_attainable_prob"] is None

    def test_a_spread_that_cannot_be_resolved_reports_none(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise RuntimeError("no spread history")

        monkeypatch.setattr(calibration, "estimate_std_dev", _raise)

        row = calibration_panel.station_row("WSSS", now=NOW)

        assert row["max_attainable_prob"] is None

def _row(icao="WSSS", max_attainable_prob=None):
    """A hand-built row, bypassing station_row, so the rendering tests can
    isolate this one column instead of riding along on em-dashes the other
    (deliberately empty) columns already print."""
    return {
        "icao": icao, "bias": {"c": None, "n": None, "stderr": None},
        "ev": None, "alltime": None, "recent": None,
        "max_attainable_prob": max_attainable_prob, "error": None,
    }


class TestRenderingTheColumn:
    def test_a_resolved_p_max_is_shown_as_a_percentage(self):
        row = _row(max_attainable_prob={
            "p_max": 0.5249, "sigma": 0.70, "source": "measured_error",
            "half_width_c": 0.5,
        })

        table = calibration_panel.render_table_html([row])

        assert "52%" in table or "52.5%" in table, (
            "the resolved p_max did not render as a percentage anywhere in "
            "the table"
        )

    def test_an_unresolved_p_max_shows_the_dash_in_its_own_cell_not_a_number(self):
        resolved = calibration_panel.render_table_html(
            [_row(max_attainable_prob={
                "p_max": 0.5249, "sigma": 0.70, "source": "measured_error",
                "half_width_c": 0.5,
            })]
        )
        unresolved = calibration_panel.render_table_html(
            [_row(max_attainable_prob=None)]
        )

        assert resolved != unresolved
        # The dash count in the column's own cell went up by exactly one --
        # everything else about the two rows is identical.
        assert unresolved.count(calibration_panel._EM_DASH) == (
            resolved.count(calibration_panel._EM_DASH) + 1
        )
