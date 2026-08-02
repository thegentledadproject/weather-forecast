"""
tests/test_determinism.py

Two runs of backtest.engine.run() over the same databases with the same
parameters must produce the same result -- not "close enough", byte-
identical. This is the property everything else in the backtest rests on:
a replay that varies run to run cannot be used to compare two strategies,
because any difference between them could be noise from the engine itself.

The shared synthetic scenario (tests/conftest.py) is used unchanged, and
both runs read the SAME seeded sqlite files, so even the manifest's db
paths match. Only the manifest git SHA is excluded, per
engine.py's own determinism note.
"""

import dataclasses
import json

from conftest import canonical_run


def test_two_identical_runs_are_byte_identical(synthetic_scenario, quiet_run):
    run_a = quiet_run(synthetic_scenario)
    run_b = quiet_run(synthetic_scenario)

    # Sanity: a scenario that produced nothing would pass this test
    # vacuously. Assert it actually traded before asserting it repeated.
    assert run_a.closed_positions, "scenario produced no closed positions -- test is vacuous"

    assert canonical_run(run_a, include_manifest=True) == canonical_run(
        run_b, include_manifest=True
    )


def test_closed_positions_match(synthetic_scenario, quiet_run):
    run_a = quiet_run(synthetic_scenario)
    run_b = quiet_run(synthetic_scenario)

    def dump(run):
        return json.dumps(
            [dataclasses.asdict(p) for p in run.closed_positions],
            sort_keys=True,
            default=str,
        )

    assert dump(run_a) == dump(run_b)


def test_equity_curve_matches(synthetic_scenario, quiet_run):
    run_a = quiet_run(synthetic_scenario)
    run_b = quiet_run(synthetic_scenario)

    assert run_a.portfolio.equity_curve == run_b.portfolio.equity_curve
    assert run_a.portfolio.cash == run_b.portfolio.cash
    assert run_a.portfolio.max_drawdown_pct == run_b.portfolio.max_drawdown_pct


def test_counters_match(synthetic_scenario, quiet_run):
    run_a = quiet_run(synthetic_scenario)
    run_b = quiet_run(synthetic_scenario)

    assert json.dumps(run_a.counters, sort_keys=True, default=str) == json.dumps(
        run_b.counters, sort_keys=True, default=str
    )


def test_run_id_is_a_function_of_params_only(synthetic_scenario, quiet_run):
    """
    run_id must collide across re-runs of the same experiment (so a re-run
    OVERWRITES its artifact directory rather than accumulating a near
    duplicate), and must change when a parameter changes.
    """
    run_a = quiet_run(synthetic_scenario)
    run_b = quiet_run(synthetic_scenario)
    assert run_a.run_id == run_b.run_id

    run_c = quiet_run(synthetic_scenario, fee_rate_pct=0.02)
    assert run_c.run_id != run_a.run_id
