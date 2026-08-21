"""
Tests for promotion_dossier's scoring half.

Every test here runs against positions held IN MEMORY -- no database, no
storage patching -- which is the whole reason score_entries() and
live_calibration() are pure. The I/O wrappers around them are one-line
storage reads and are not retested here.

The properties under test are the ones that would make
`brier_model < brier_market` mean something other than what it says, and
each of them is a live trap rather than a hypothetical:

  * model_prob must NOT be flipped for a NO side (it arrives already
    side-adjusted from ev_engine, via executor).
  * a term must land in BOTH Brier lists or NEITHER, or the two means
    describe different sets of entries.
  * an unmeasurable entry must be skipped and COUNTED, never scored as if
    the model had said 0.
"""

from datetime import date

import pytest

import promotion_dossier
from models import Position

STATION = "ZGGG"
BOUNDS = (30, 40)


def _position(target_date, bucket_c, side, entry_price, model_prob, mode="paper"):
    return Position(
        position_id=f"{STATION}:{target_date}:{bucket_c}:{side}",
        station_icao=STATION,
        target_date=target_date,
        bucket_c=bucket_c,
        side=side,
        entry_price=entry_price,
        size_usd=3.0,
        entry_time=f"{target_date}T06:00:00Z",
        status="closed_resolution",
        high_water_mark=entry_price,
        execution_mode=mode,
        model_prob=model_prob,
    )


def _settled(mapping):
    """{date: bucket} -> the {date: (bucket, min, max)} shape storage returns."""
    return {d: (b, BOUNDS[0], BOUNDS[1]) for d, b in mapping.items()}


# --------------------------------------------------------------------------
# Side adjustment
# --------------------------------------------------------------------------

def test_no_side_model_prob_is_not_flipped():
    """
    A NO position's model_prob is ALREADY P(this side wins).

    ev_engine.build_ev_table stores `side_model_prob = model_prob if side ==
    "YES" else (1 - model_prob)` on the EVResult and executor.open_position
    copies THAT onto the row. Flipping it again here would score every NO
    entry against the complement of what the model actually said -- and NO
    entries are the majority of the book on any station whose modal bucket
    is expensive.

    Day settles on 35; a NO on 34 therefore WINS. A model that said 0.90 for
    this side was nearly right, so its Brier term must be small (0.01), not
    the 0.81 a spurious flip would produce.
    """
    day = date(2026, 8, 15)
    entries, _ = promotion_dossier.score_entries(
        [_position(day, 34, "NO", 0.80, 0.90)], _settled({day: 35})
    )
    assert len(entries) == 1
    assert entries[0]["outcome"] == 1.0
    stats = promotion_dossier.live_calibration(entries)
    assert stats["brier_model"] == pytest.approx(0.01)
    assert stats["brier_market"] == pytest.approx(0.04)


def test_no_side_losing_bucket_resolves_against_it():
    """A NO on the bucket that actually settled loses -- outcome 0.0."""
    day = date(2026, 8, 19)
    entries, _ = promotion_dossier.score_entries(
        [_position(day, 35, "NO", 0.70, 0.75)], _settled({day: 35})
    )
    assert entries[0]["outcome"] == 0.0


# --------------------------------------------------------------------------
# Pairing and refusal to guess
# --------------------------------------------------------------------------

def test_missing_model_prob_is_skipped_and_counted_not_scored_as_zero():
    """
    A NULL model_prob is "no model ran", not "the model said 0".

    storage.py stores NULL on manual_trigger rows and on every row predating
    the column, and says explicitly not to backfill them. Scoring one as 0.0
    would hand the model a Brier term of 1.0 on a day it never spoke -- and
    on a winning YES, that is the worst possible term, invented.
    """
    day = date(2026, 8, 14)
    entries, skipped = promotion_dossier.score_entries(
        [_position(day, 33, "YES", 0.30, None)], _settled({day: 33})
    )
    assert entries == []
    assert skipped["no stored model_prob"] == 1


def test_unsettled_target_date_is_skipped_and_counted():
    """No settlement means unscorable -- the gap backtest brier_n exists for."""
    day = date(2026, 8, 21)
    entries, skipped = promotion_dossier.score_entries(
        [_position(day, 34, "YES", 0.33, 0.58)], settled={}
    )
    assert entries == []
    assert skipped["target date not settled yet"] == 1


def test_both_brier_terms_describe_the_same_entries():
    """
    The two means are paired: n is one number and it describes both.

    Scoring the model over one subset and the market over another would make
    `brier_model < brier_market` a comparison between two different
    questions -- and that inequality is the entire bar for real money.
    """
    days = [date(2026, 8, 14), date(2026, 8, 15), date(2026, 8, 16)]
    positions = [
        _position(days[0], 33, "YES", 0.30, 0.55),
        _position(days[1], 34, "YES", 0.40, None),   # unscorable
        _position(days[2], 35, "YES", 0.26, 0.60),
    ]
    entries, _ = promotion_dossier.score_entries(
        positions, _settled({days[0]: 33, days[1]: 34, days[2]: 32})
    )
    stats = promotion_dossier.live_calibration(entries)
    assert stats["n"] == 2
    # Recomputed by hand from the two scorable rows only.
    assert stats["brier_model"] == pytest.approx(((0.55 - 1) ** 2 + 0.60 ** 2) / 2)
    assert stats["brier_market"] == pytest.approx(((0.30 - 1) ** 2 + 0.26 ** 2) / 2)


def test_empty_book_returns_none_not_a_perfect_score():
    """A Brier of 0.0 is PERFECT. An empty book must not be able to print one."""
    assert promotion_dossier.live_calibration([]) is None


# --------------------------------------------------------------------------
# The gap, and its uncertainty
# --------------------------------------------------------------------------

def test_gap_is_positive_when_the_model_is_closer_than_the_price():
    """
    gap := market_term - model_term, so positive means the MODEL won.

    Sign convention is load-bearing: mean(gap) > 0 is exactly
    `brier_model < brier_market`, the direction config.calibration_vs_market()
    requires. Inverting it would report a losing station as a winning one.
    """
    day = date(2026, 8, 18)
    entries, _ = promotion_dossier.score_entries(
        [_position(day, 34, "YES", 0.40, 0.62)], _settled({day: 34})
    )
    stats = promotion_dossier.live_calibration(entries)
    assert stats["brier_model"] < stats["brier_market"]
    assert stats["mean_gap"] > 0


def test_a_large_gap_measured_noisily_is_not_separable():
    """
    The point of reporting a standard error at all.

    Two entries that disagree violently average to a big gap with a bigger
    stderr. Reporting only the means would show the model beating the market
    by a wide margin on a sample that says nothing -- the reading of "0.062
    vs 0.145 over 9 entries" this module exists to prevent.
    """
    days = [date(2026, 8, 14), date(2026, 8, 15)]
    positions = [
        # Settles on 33: the model called it at 0.90, the book priced it 0.10.
        # Model wins this entry by 0.80.
        _position(days[0], 33, "YES", 0.10, 0.90),
        # Settles on 35, so this YES loses: the model said 0.60, the book
        # said 0.20. Model loses this entry by 0.32.
        _position(days[1], 34, "YES", 0.20, 0.60),
    ]
    entries, _ = promotion_dossier.score_entries(
        positions, _settled({days[0]: 33, days[1]: 35})
    )
    stats = promotion_dossier.live_calibration(entries)

    # The means alone flatter the model: it "beats the market" by 0.24 a trade.
    assert stats["mean_gap"] == pytest.approx(0.24)
    assert stats["brier_model"] < stats["brier_market"]
    # The spread between the two entries is wider than the average of them.
    assert stats["gap_stderr"] > abs(stats["mean_gap"])
    assert stats["separable"] is False


def test_single_entry_has_no_standard_error():
    """n=1 cannot produce a stdev, and must not claim separability from one point."""
    day = date(2026, 8, 20)
    entries, _ = promotion_dossier.score_entries(
        [_position(day, 40, "YES", 0.15, 0.22)], _settled({day: 36})
    )
    stats = promotion_dossier.live_calibration(entries)
    assert stats["n"] == 1
    assert stats["gap_stderr"] is None
    assert stats["separable"] is False


# --------------------------------------------------------------------------
# Windowing and bounds drift
# --------------------------------------------------------------------------

def test_since_and_until_exclude_by_target_date():
    days = [date(2026, 8, 14), date(2026, 8, 18), date(2026, 8, 20)]
    positions = [
        _position(days[0], 33, "YES", 0.30, 0.55),
        _position(days[1], 34, "YES", 0.40, 0.62),
        _position(days[2], 40, "YES", 0.15, 0.22),
    ]
    settled = _settled({days[0]: 33, days[1]: 34, days[2]: 36})
    entries, skipped = promotion_dossier.score_entries(
        positions, settled, since=days[1], until=days[1]
    )
    assert len(entries) == 1
    assert entries[0]["position"].target_date == days[1]
    assert skipped["outside --since/--until window"] == 2


def test_bounds_drift_counts_days_traded_under_a_different_window():
    """
    ZGGG's window moved 27-37 -> 30-40 on 2026-08-18, so a book spanning that
    date is two samples wearing one number. The count is what says so.
    """
    old_day, new_day = date(2026, 8, 16), date(2026, 8, 19)
    positions = [
        _position(old_day, 33, "YES", 0.30, 0.55),
        _position(new_day, 34, "YES", 0.40, 0.62),
    ]
    settled = {old_day: (33, 27, 37), new_day: (34, 30, 40)}
    entries, _ = promotion_dossier.score_entries(positions, settled)
    drift = promotion_dossier.bounds_drift(STATION, entries)

    assert drift["config_bounds"] == BOUNDS          # config carries 30-40 today
    assert drift["n_mismatched"] == 1                # the 27-37 day
    assert set(drift["windows"]) == {(27, 37), (30, 40)}


def test_edge_buckets_are_counted_because_they_mean_at_or_beyond():
    """
    The bottom and top buckets are censored catch-alls, not single degrees.
    A window shift turns an interior bucket into one of them, which changes
    what winning MEANS for a position sitting there.
    """
    days = [date(2026, 8, 16), date(2026, 8, 17), date(2026, 8, 18)]
    positions = [
        _position(days[0], 30, "YES", 0.20, 0.10),   # bottom edge
        _position(days[1], 40, "YES", 0.15, 0.22),   # top edge
        _position(days[2], 34, "YES", 0.40, 0.62),   # interior
    ]
    entries, _ = promotion_dossier.score_entries(
        positions, _settled({days[0]: 30, days[1]: 36, days[2]: 34})
    )
    assert promotion_dossier.bounds_drift(STATION, entries)["n_on_edge"] == 2


# --------------------------------------------------------------------------
# Independence and robustness
# --------------------------------------------------------------------------

def test_same_day_entries_are_counted_as_one_settlement():
    """
    Two entries on one morning are ONE draw of the weather, not two.

    n_days is what says so. Without it the standard error divides by the
    entry count and reports an interval the evidence does not support --
    and a multi-leg basket makes n exceed n_days routinely.
    """
    day = date(2026, 8, 19)
    positions = [
        _position(day, 35, "NO", 0.70, 0.75),
        _position(day, 32, "NO", 0.85, 0.88),
    ]
    entries, _ = promotion_dossier.score_entries(positions, _settled({day: 35}))
    stats = promotion_dossier.live_calibration(entries)
    assert stats["n"] == 2
    assert stats["n_days"] == 1


def test_unrecognised_side_is_skipped_rather_than_raising():
    """
    One corrupt row must not take down the dossier.

    resolution_exit_price() refuses a side it does not know, which is right
    for the resolution path -- but here an unreadable entry is an UNSCORED
    entry, the way config.maturity_report() treats an unreadable criterion
    as a failed one instead of raising.
    """
    day = date(2026, 8, 18)
    positions = [
        _position(day, 34, "MAYBE", 0.40, 0.62),
        _position(day, 33, "YES", 0.30, 0.55),
    ]
    entries, skipped = promotion_dossier.score_entries(positions, _settled({day: 33}))
    assert len(entries) == 1
    assert skipped["unrecognised side"] == 1
