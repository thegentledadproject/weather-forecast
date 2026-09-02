"""
tests/test_calibration_panel.py

The per-station indicator the dashboards render: measured bias, the EV the
engine is looking at right now, and how the model has actually scored
against the market.

WHY THE NUMBERS LIVE HERE AND NOT IN THE GENERATORS. promotion_dossier.py
already computes all of this, but only as a per-station CLI nobody runs
during a trading day. deploy/generate_dashboard.py cannot be imported by a
test at all (it renders a page at import time, which is why it has never
had one), so putting the arithmetic in the generator would put it out of
reach of exactly the checks it needs. The generators render; this module
computes and is tested.

WHAT THE TESTS ARE MOSTLY GUARDING. Not the arithmetic -- live_calibration
owns that and is tested with it. These pin the reporting honesty rules,
which are the ones a dashboard cell quietly breaks:

  * an unscored station renders as "no data", never as a Brier of 0.0,
    which is a PERFECT score
  * n AND n_days always travel with a gap, because entries taken on one
    station-day are one draw of the weather
  * one unreadable station costs its own row, not the panel
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import calibration_panel
import config

NOW = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)


def _calibration(n=20, n_days=9, gap=0.03, stderr=0.01, separable=True):
    """A live_calibration() result, in its real shape."""
    return {
        "n": n,
        "n_days": n_days,
        "brier_model": 0.121,
        "brier_market": 0.151,
        "mean_gap": gap,
        "gap_stderr": stderr,
        "separable": separable,
        "model_wins": 13,
        "win_rate": 0.45,
    }


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """
    Every seam the panel reads, replaced. Returns a dict the test mutates
    to say what each station's record looks like.
    """
    state = {
        "bias": {"WSSS": (-0.42, 31, 0.18)},
        "entries": {"WSSS": ([{"scored": True}], {})},
        "calibration": {"WSSS": _calibration()},
        "since_seen": [],
        "raises": set(),
    }

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    import entry_manager
    import promotion_dossier

    def _bias(icao):
        if icao in state["raises"]:
            raise RuntimeError("bias unreadable")
        return state["bias"].get(icao, (None, None, None))

    def _entries(icao, limit=None, since=None, until=None):
        state["since_seen"].append((icao, since))
        return state["entries"].get(icao, ([], {}))

    def _calib(entries):
        # Keyed on identity of the entry list the stub handed back, so the
        # windowed call and the all-time call can differ.
        for icao, (rows, _) in state["entries"].items():
            if rows is entries:
                return state["calibration"].get(icao)
        return None

    monkeypatch.setattr(entry_manager, "forecast_bias_stats", _bias)
    monkeypatch.setattr(promotion_dossier, "scorable_entries", _entries)
    monkeypatch.setattr(promotion_dossier, "live_calibration", _calib)
    return state


def _write_snapshot(tmp_path, icao, generated_at, results):
    (tmp_path / f"ev_latest_{icao}.json").write_text(
        json.dumps({
            "station_icao": icao,
            "generated_at": generated_at,
            "target_date": "2026-08-30",
            "results": results,
        }),
        encoding="utf-8",
    )


class TestAnUnscoredStationSaysSo:
    def test_it_reports_no_calibration_rather_than_a_perfect_score(self, stub):
        stub["entries"]["RKPK"] = ([], {})
        stub["calibration"]["RKPK"] = None

        rows, _ = calibration_panel.station_rows(["RKPK"], now=NOW)

        assert rows[0]["alltime"] is None, (
            "an empty book produced a calibration dict -- a Brier of 0.0 is a "
            "PERFECT score and must never be reachable by having no data"
        )

    def test_an_unmeasured_bias_is_none_not_zero(self, stub):
        rows, _ = calibration_panel.station_rows(["KLAX"], now=NOW)

        assert rows[0]["bias"]["c"] is None, (
            "an unmeasured bias came back as 0.0 -- 'no correction measured' "
            "and 'measured, and it is zero' are different facts"
        )


class TestTheGapNeverTravelsAlone:
    def test_the_row_carries_n_and_n_days(self, stub):
        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        alltime = rows[0]["alltime"]
        assert alltime["n"] == 20
        assert alltime["n_days"] == 9, (
            "n_days is the honest ceiling on independent draws -- entries on "
            "one station-day are one draw of the weather"
        )

    def test_the_rendered_cell_shows_n_days(self, stub):
        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        table = calibration_panel.render_table_html(rows)

        assert "9d" in table or "9 d" in table


class TestTheRecentWindow:
    def test_it_scores_a_second_time_over_the_recent_days_only(self, stub):
        calibration_panel.station_rows(["WSSS"], now=NOW, recent_days=14)

        sinces = [since for _, since in stub["since_seen"]]
        assert None in sinces, "the all-time score was never asked for"
        assert NOW.date() - timedelta(days=14) in sinces, (
            "the recent column did not window its score, so it would repeat "
            "the all-time number"
        )


class TestTheEVCell:
    def test_it_takes_the_best_net_ev_in_the_snapshot(self, stub, tmp_path):
        _write_snapshot(tmp_path, "WSSS", NOW.isoformat(), [
            {"bucket_c": 32, "side": "YES", "net_ev_per_dollar": 0.11},
            {"bucket_c": 33, "side": "YES", "net_ev_per_dollar": 0.42},
            {"bucket_c": 34, "side": "NO", "net_ev_per_dollar": -0.05},
        ])

        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        assert rows[0]["ev"]["net_ev"] == pytest.approx(0.42)
        assert rows[0]["ev"]["bucket_c"] == 33
        assert rows[0]["ev"]["side"] == "YES"

    def test_it_carries_the_snapshots_age(self, stub, tmp_path):
        _write_snapshot(tmp_path, "WSSS", (NOW - timedelta(minutes=25)).isoformat(),
                        [{"bucket_c": 33, "side": "YES", "net_ev_per_dollar": 0.42}])

        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        assert rows[0]["ev"]["age_s"] == pytest.approx(25 * 60, abs=1)

    def test_a_computed_but_empty_snapshot_is_not_a_missing_one(self, stub, tmp_path):
        # ev_engine writes a snapshot even when it found nothing, precisely
        # so "computed and found nothing" stays distinguishable from "never
        # computed". The panel must not collapse the two.
        _write_snapshot(tmp_path, "WSSS", NOW.isoformat(), [])

        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        assert rows[0]["ev"] is not None
        assert rows[0]["ev"]["net_ev"] is None

    def test_a_sub_screen_price_is_not_offered_as_the_best_ev(self, stub, tmp_path):
        """
        net EV divides raw edge by price, so a near-zero price turns any
        stale-model disagreement into a "+21,517% EV" phantom. The trading
        path screens those out (config.EV_MIN_PRICE_SCREEN) and both EV
        cards already mirror the screen; a third view that skips it puts the
        phantoms back on the page, which is what shipped on 2026-09-02.
        """
        _write_snapshot(tmp_path, "WSSS", NOW.isoformat(), [
            {"bucket_c": 31, "side": "YES", "market_price": 0.001,
             "raw_edge": 0.20, "net_ev_per_dollar": 215.17},
            {"bucket_c": 33, "side": "YES", "market_price": 0.44,
             "raw_edge": 0.09, "net_ev_per_dollar": 0.18},
        ])

        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        assert rows[0]["ev"]["bucket_c"] == 33, (
            "the panel offered a sub-screen price as this station's best EV"
        )
        assert rows[0]["ev"]["net_ev"] == pytest.approx(0.18)

    def test_an_implausible_edge_is_not_offered_either(self, stub, tmp_path):
        """The other half of the same screen: an edge bigger than the price
        leaves room for is a stale model, not an opportunity."""
        _write_snapshot(tmp_path, "WSSS", NOW.isoformat(), [
            {"bucket_c": 31, "side": "YES", "market_price": 0.92,
             "raw_edge": 0.60, "net_ev_per_dollar": 4.10},
            {"bucket_c": 33, "side": "YES", "market_price": 0.44,
             "raw_edge": 0.09, "net_ev_per_dollar": 0.18},
        ])

        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        assert rows[0]["ev"]["bucket_c"] == 33

    def test_a_snapshot_of_only_phantoms_prices_nothing(self, stub, tmp_path):
        _write_snapshot(tmp_path, "WSSS", NOW.isoformat(), [
            {"bucket_c": 31, "side": "YES", "market_price": 0.001,
             "raw_edge": 0.20, "net_ev_per_dollar": 215.17},
        ])

        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        assert rows[0]["ev"] is not None
        assert rows[0]["ev"]["net_ev"] is None, (
            "a snapshot containing nothing but screened-out rows must read as "
            "'nothing priced', not as a phantom"
        )

    def test_no_snapshot_at_all_is_none(self, stub):
        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        assert rows[0]["ev"] is None


class TestOneBadStationCostsOnlyItsOwnRow:
    def test_the_other_stations_still_render(self, stub):
        stub["raises"].add("WMKK")

        rows, warnings = calibration_panel.station_rows(["WMKK", "WSSS"], now=NOW)

        assert [r["icao"] for r in rows] == ["WMKK", "WSSS"]
        assert rows[0]["error"] is not None
        assert rows[1]["alltime"]["n"] == 20
        assert any("WMKK" in w for w in warnings)


class TestRendering:
    def test_an_unscored_station_renders_a_dash_not_a_number(self, stub):
        stub["entries"]["RKPK"] = ([], {})
        rows, _ = calibration_panel.station_rows(["RKPK"], now=NOW)

        table = calibration_panel.render_table_html(rows)

        assert "RKPK" in table
        assert "0.000" not in table, (
            "an unscored station rendered a numeric Brier -- the empty case "
            "must read as absent, not as perfect"
        )

    def test_a_separable_gap_is_marked_and_a_noisy_one_is_not(self, stub):
        stub["calibration"]["WSSS"] = _calibration(separable=True)
        separable_rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)
        stub["calibration"]["WSSS"] = _calibration(separable=False)
        noisy_rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        separable = calibration_panel.render_table_html(separable_rows)
        noisy = calibration_panel.render_table_html(noisy_rows)

        assert separable != noisy, (
            "a gap inside its own error bar rendered identically to one "
            "outside it -- the whole point of showing the stderr"
        )

    def test_it_prints_no_verdict(self, stub):
        rows, _ = calibration_panel.station_rows(["WSSS"], now=NOW)

        table = calibration_panel.render_table_html(rows).lower()

        # The dossier's stance, inherited: print what is measured, not a
        # judgement. "beats the market" on a 9-day sample is laundering.
        assert "beats the market" not in table
        assert "promote" not in table
