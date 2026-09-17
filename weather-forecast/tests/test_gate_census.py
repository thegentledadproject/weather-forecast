"""
AST-based structural guard: the number of decision-returning statements in
entry_manager.evaluate_entry() must equal backtest/entry_sim.py's GATE_COUNT.

This catches drift where a gate is added or removed from the live function
without entry_sim's replica (and its GATE_COUNT constant) being updated to
match. Adapted from the AST census in scratchpad/smoke_parity.py (section A).
"""

import ast
import inspect
import textwrap

import entry_manager
from backtest import entry_sim


def test_gate_count_matches_live_decision_sites():
    src = inspect.getsource(entry_manager.evaluate_entry)
    tree = ast.parse(textwrap.dedent(src))
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


# --------------------------------------------------------------------------
# Wave 1: every decision site stamps a rule_id from ENTRY_RULE_IDS
# --------------------------------------------------------------------------

def _rule_id_literals(fn):
    """
    The literal rule_id at every decision-returning statement in fn --
    `return EntryDecision(...)` and `return _rejected(...)`. The nested
    _rejected factory passes `rule_id=rule_id` (a Name); that is the shape,
    not a site, and is skipped. Any other site without a literal fails.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    literals = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Name) and call.func.id in ("EntryDecision", "_rejected")):
            continue
        by_name = {k.arg: k.value for k in call.keywords if k.arg is not None}
        assert "rule_id" in by_name, f"{fn.__qualname__} line {node.lineno}: decision site has no rule_id"
        value = by_name["rule_id"]
        if isinstance(value, ast.Name) and value.id == "rule_id":
            continue  # the _rejected factory forwarding its argument
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{fn.__qualname__} line {node.lineno}: rule_id must be a string literal"
        )
        literals.append(value.value)
    return literals


def _dict_rule_ids(fn):
    """rule_id literals written through `EntryDecision(**{**d.__dict__, "rule_id": ...})`."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "rule_id":
                assert isinstance(value, ast.Constant), f"{fn.__qualname__}: rule_id must be a literal"
                found.append(value.value)
    return found


def test_every_live_gate_site_has_a_known_rule_id():
    ids = _rule_id_literals(entry_manager.evaluate_entry)
    assert len(ids) == entry_sim.GATE_COUNT
    unknown = set(ids) - entry_manager.ENTRY_RULE_IDS
    assert not unknown, f"rule ids not in ENTRY_RULE_IDS: {unknown}"
    assert "approved" in ids


def test_entry_sim_uses_exactly_the_same_rule_ids_in_the_same_order():
    live = _rule_id_literals(entry_manager.evaluate_entry)
    sim = _rule_id_literals(entry_sim.evaluate_entry_sim)
    assert live == sim, f"live/sim rule_id sequences differ:\n live={live}\n sim ={sim}"


def test_budget_veto_and_collection_sites_have_rule_ids():
    assert set(_dict_rule_ids(entry_manager.apply_portfolio_budget)) == {"budget_exhausted", "budget_scaled"}
    assert set(_dict_rule_ids(entry_manager.veto_same_bucket_conflicts)) == {"same_bucket_conflict"}
    assert _rule_id_literals(entry_manager.collection_only_decision) == ["collection_gate"]


def test_the_id_set_is_closed():
    """Every id any site uses is declared, and every declared id is used somewhere."""
    used = set(_rule_id_literals(entry_manager.evaluate_entry))
    used |= set(_dict_rule_ids(entry_manager.apply_portfolio_budget))
    used |= set(_dict_rule_ids(entry_manager.veto_same_bucket_conflicts))
    used |= set(_rule_id_literals(entry_manager.collection_only_decision))
    assert used == entry_manager.ENTRY_RULE_IDS
