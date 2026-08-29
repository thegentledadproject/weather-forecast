"""
tests/test_scan_cadence.py

Regression tests for scheduler.seconds_until_next_boundary().

WHY THIS EXISTS. The function used to answer "one interval from NOW",
and run_forever() calls it AFTER a cycle has finished -- so the real
period was interval + however long the cycle took, and every group's
cadence drifted by its own cycle length. Measured on the live box
2026-08-28: the 9-station UTC+8 group took ~3.5 min per cycle and got
14 of its designed 18 entry-window ticks, while one-station groups got
17-18. backtest/simclock.generate_ticks() had always walked the grid
from the window start, so replays assumed 18 too.

The window's own grid (start + k*interval) is therefore the answer, and
a cycle that overruns a grid point waits for the NEXT one rather than
firing a catch-up burst.
"""

import scheduler


def _window(start_min: int, end_min: int, interval_min):
    """A schedule window as determine_window() returns one."""
    return {
        "start_minute": start_min,
        "end_minute": end_min,
        "interval_min": interval_min,
        "mode": "primary",
        "min_net_ev": 0.15,
        "description": "test window",
    }


PRIMARY = _window(5 * 60, 8 * 60, 10)


def test_next_tick_lands_on_the_window_grid_not_one_interval_from_now():
    # 05:00 tick, cycle took 3 minutes. The next tick belongs at 05:10,
    # which is 7 minutes away -- not 10.
    assert scheduler.seconds_until_next_boundary(PRIMARY, 5, 3) == 7 * 60


def test_a_cycle_that_overruns_a_grid_point_waits_for_the_next_one():
    # Cycle ran past 05:10 and returned at 05:12. Firing immediately would
    # be a catch-up burst; the answer is 05:20, 8 minutes away.
    assert scheduler.seconds_until_next_boundary(PRIMARY, 5, 12) == 8 * 60


def test_landing_exactly_on_a_grid_point_waits_a_full_interval():
    # Not zero: a cycle that returns instantly at 05:10 must still sleep
    # to 05:20 rather than spinning the loop.
    assert scheduler.seconds_until_next_boundary(PRIMARY, 5, 10) == 10 * 60


def test_the_next_tick_never_crosses_the_window_end():
    # 07:55 with a 10-minute grid: the grid says 08:00, and so does the
    # window end. Either way the group re-evaluates at the boundary.
    assert scheduler.seconds_until_next_boundary(PRIMARY, 7, 55) == 5 * 60


def test_a_closed_window_sleeps_until_it_ends():
    closed = _window(0, 4 * 60, None)
    assert scheduler.seconds_until_next_boundary(closed, 2, 30) == 90 * 60


def test_the_grid_is_anchored_to_the_window_start_not_to_the_hour():
    # 04:00-05:00 at a 30-minute interval: ticks belong at 04:00 and 04:30.
    # Reached at 04:05, the next one is 25 minutes away.
    collection = _window(4 * 60, 5 * 60, 30)
    assert scheduler.seconds_until_next_boundary(collection, 4, 5) == 25 * 60
