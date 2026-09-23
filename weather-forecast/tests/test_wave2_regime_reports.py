"""
cohort_monitor, calibration_panel and promotion_dossier each report each
side of config.REGIME_BOUNDARIES separately by default, and pool on
request (--no-regime-split / regime_split=False). With no boundaries the
output is the pre-Wave-2 output.
"""
from datetime import date

import pytest

import calibration_panel
import cohort_monitor
import config
import promotion_dossier
import storage
from models import Position

BOUNDARY = "2026-09-21"
BEFORE = date(2026, 9, 18)
AFTER = date(2026, 9, 23)


def _position(day, bucket_c, pid, station="WSSS", model_prob=0.55):
    return Position(
        position_id=pid, station_icao=station, target_date=day, bucket_c=bucket_c, side="YES",
        entry_price=0.30, size_usd=10.0, entry_time=f"{day}T22:00:00+00:00", status="open",
        high_water_mark=0.30, is_paper=True, execution_mode="paper", model_prob=model_prob,
    )


@pytest.fixture
def two_regimes(tmp_path, monkeypatch):
    """One settled winner before the boundary, one settled loser after it."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", (BOUNDARY,))
    storage.migrate()
    for day, bucket, pid in ((BEFORE, 32, "w"), (AFTER, 31, "l")):
        storage.open_position(_position(day, bucket, pid))
        storage.close_position(pid, 0.30, f"{day}T10:00:00+00:00", "closed_resolution", "test")
        storage.save_settled_bucket("WSSS", day, 32, 30, 34, "test")


# --- cohort_monitor ----------------------------------------------------------

def test_the_monitor_prints_one_block_per_regime_by_default(two_regimes, capsys):
    cohort_monitor.main(["--station", "WSSS", "--as-of", "2026-09-24"])
    out = capsys.readouterr().out
    assert "regime pre-2026-09-21" in out
    assert "regime from-2026-09-21" in out
    assert "\nall time\n" not in out
    assert "POOLED across regimes" in out


def test_the_monitor_pools_on_request(two_regimes, capsys):
    cohort_monitor.main(["--station", "WSSS", "--as-of", "2026-09-24", "--no-regime-split"])
    out = capsys.readouterr().out
    assert "\nall time\n" in out
    assert "regime pre-" not in out


def test_the_monitor_is_unchanged_with_no_boundaries(two_regimes, monkeypatch, capsys):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ())
    cohort_monitor.main(["--station", "WSSS", "--as-of", "2026-09-24"])
    out = capsys.readouterr().out
    assert "\nall time\n" in out
    assert "regime" not in out


# --- calibration_panel -------------------------------------------------------

def test_the_cohort_card_renders_a_table_per_regime(two_regimes):
    html = calibration_panel.cohort_card(as_of=date(2026, 9, 24), stations=["WSSS"])
    assert "pre-2026-09-21" in html and "from-2026-09-21" in html
    assert "held" in html


def test_the_cohort_card_pools_on_request(two_regimes):
    html = calibration_panel.cohort_card(
        as_of=date(2026, 9, 24), stations=["WSSS"], regime_split=False)
    assert "pre-2026-09-21" not in html


def test_the_split_renders_nothing_without_boundaries(two_regimes, monkeypatch):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ())
    rows, _ = cohort_monitor.load_cohort(stations=["WSSS"])
    assert calibration_panel.render_regime_split_html(rows) == ""


def test_an_empty_regime_prints_no_number(two_regimes, monkeypatch):
    """Reporting rule 1: no empty book may print a number."""
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ("2026-09-21", "2026-10-05"))
    rows, _ = cohort_monitor.load_cohort(stations=["WSSS"])
    html = calibration_panel.render_regime_split_html(rows)
    assert "from-2026-10-05" in html
    assert "no rows" in html


# --- promotion_dossier -------------------------------------------------------

def test_the_dossier_scores_each_regime_separately(two_regimes, capsys):
    promotion_dossier._print_calibration("WSSS", None, None)
    out = capsys.readouterr().out
    assert "BEATS_MARKET -- regime pre-2026-09-21" in out
    assert "BEATS_MARKET -- regime from-2026-09-21" in out
    assert out.count("scored entries:") == 2


def test_the_dossier_pools_on_request(two_regimes, capsys):
    promotion_dossier._print_calibration("WSSS", None, None, regime_split=False)
    out = capsys.readouterr().out
    assert "BEATS_MARKET -- measured on the live book" in out
    assert out.count("scored entries:") == 1


def test_the_dossier_is_unchanged_with_no_boundaries(two_regimes, monkeypatch, capsys):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ())
    promotion_dossier._print_calibration("WSSS", None, None)
    out = capsys.readouterr().out
    assert "BEATS_MARKET -- measured on the live book" in out
    assert "regime" not in out
