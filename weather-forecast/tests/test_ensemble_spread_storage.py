"""
The ensemble spread the live path already fetches is currently used once and
thrown away, so no historical record exists to score the "ensemble" tier of
calibration.estimate_std_dev against the measured-error tier it pre-empts.

These tests pin the collection side of that: the value is persisted with the
member count that produced it, and re-fetching the same station/date within a
cycle does not multiply rows.
"""
from datetime import date

import pytest

import config
import storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    return tmp_path


def test_saved_ensemble_spread_round_trips(db):
    storage.save_ensemble_spread(
        "WSSS", date(2026, 8, 29), std_dev_c=0.503, member_count=51,
        fetched_at="2026-08-29T05:10:00+00:00",
    )

    rows = storage.load_ensemble_spreads("WSSS")

    assert rows == {date(2026, 8, 29): (0.503, 51)}


def test_second_save_for_same_day_replaces_rather_than_duplicates(db):
    storage.save_ensemble_spread(
        "WSSS", date(2026, 8, 29), std_dev_c=0.503, member_count=51,
        fetched_at="2026-08-29T05:10:00+00:00",
    )
    storage.save_ensemble_spread(
        "WSSS", date(2026, 8, 29), std_dev_c=0.612, member_count=51,
        fetched_at="2026-08-29T06:10:00+00:00",
    )

    rows = storage.load_ensemble_spreads("WSSS")

    assert rows == {date(2026, 8, 29): (0.612, 51)}


def test_load_returns_empty_for_station_with_no_record(db):
    storage.save_ensemble_spread(
        "WSSS", date(2026, 8, 29), std_dev_c=0.503, member_count=51,
        fetched_at="2026-08-29T05:10:00+00:00",
    )

    assert storage.load_ensemble_spreads("RJTT") == {}
