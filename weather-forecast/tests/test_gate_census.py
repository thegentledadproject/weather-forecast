"""
AST-based structural guard: the number of decision-returning statements in
entry_manager.evaluate_entry() must equal backtest/entry_sim.py's GATE_COUNT.

This catches drift where a gate is added or removed from the live function
without entry_sim's replica (and its GATE_COUNT constant) being updated to
match. Adapted from the AST census in scratchpad/smoke_parity.py (section A).
"""

import ast
import inspect

import entry_manager
from backtest import entry_sim


def test_gate_count_matches_live_decision_sites():
    src = inspect.getsource(entry_manager.evaluate_entry)
    tree = ast.parse(inspect.cleandoc(src))
    fn = tree.body[0]

    decision_returns = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        v = node.value
        # `return EntryDecision(...)` or `return _rejected(...)`
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
            if v.func.id in ("EntryDecision", "_rejected"):
                decision_returns += 1

    # The nested _rejected() helper's own `return EntryDecision(...)` is a
    # shape factory, not a decision site -- subtract it.
    decision_sites = decision_returns - 1

    assert decision_sites == entry_sim.GATE_COUNT, (
        f"entry_manager.evaluate_entry has {decision_sites} decision sites but "
        f"entry_sim.GATE_COUNT = {entry_sim.GATE_COUNT} -- a gate was added or "
        f"removed live without entry_sim's replica being updated to match."
    )
