"""
config.REGIME_BOUNDARIES is the list of days a wave first ran on the box.
Every entry must be an ISO date no later than tomorrow (UTC): the boundary
is the first TARGET DATE every station decides on the new code, which is
legitimately tomorrow when the deploy lands in the evening gap after the
last region's window closes; anything further out is a typo and would
split every report at a day with no rows on one side. The list must be
strictly increasing. Vacuous while the tuple is
empty on the branch; load-bearing from the deploy-day stamp on.
"""
from datetime import date, timedelta

import config
import regimes


def test_every_boundary_is_an_iso_date_no_later_than_tomorrow():
    latest_allowed = config._now_utc().date() + timedelta(days=1)
    for raw in config.REGIME_BOUNDARIES:
        parsed = date.fromisoformat(raw)      # raises on a malformed stamp
        assert parsed <= latest_allowed, f"boundary {raw} is more than a day ahead"
        assert raw == parsed.isoformat(), f"boundary {raw} is not canonical ISO"


def test_boundaries_are_strictly_increasing():
    parsed = [date.fromisoformat(b) for b in config.REGIME_BOUNDARIES]
    assert parsed == sorted(set(parsed))
    assert regimes.boundaries() == tuple(parsed)


def test_the_tuple_is_a_tuple_of_strings():
    assert isinstance(config.REGIME_BOUNDARIES, tuple)
    assert all(isinstance(b, str) for b in config.REGIME_BOUNDARIES)
