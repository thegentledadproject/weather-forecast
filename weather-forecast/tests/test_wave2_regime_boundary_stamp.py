"""
config.REGIME_BOUNDARIES is the list of days a wave first ran on the box.
Every entry must be an ISO date that has already happened (a boundary in
the future would split every report at a day with no rows on one side),
and the list must be strictly increasing. Vacuous while the tuple is
empty on the branch; load-bearing from the deploy-day stamp on.
"""
from datetime import date

import config
import regimes


def test_every_boundary_is_an_iso_date_not_in_the_future():
    today = config._now_utc().date()
    for raw in config.REGIME_BOUNDARIES:
        parsed = date.fromisoformat(raw)      # raises on a malformed stamp
        assert parsed <= today, f"boundary {raw} is in the future"
        assert raw == parsed.isoformat(), f"boundary {raw} is not canonical ISO"


def test_boundaries_are_strictly_increasing():
    parsed = [date.fromisoformat(b) for b in config.REGIME_BOUNDARIES]
    assert parsed == sorted(set(parsed))
    assert regimes.boundaries() == tuple(parsed)


def test_the_tuple_is_a_tuple_of_strings():
    assert isinstance(config.REGIME_BOUNDARIES, tuple)
    assert all(isinstance(b, str) for b in config.REGIME_BOUNDARIES)
