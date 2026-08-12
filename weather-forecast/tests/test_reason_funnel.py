"""
tests/test_reason_funnel.py

The rejection funnel is the only artifact that answers "why did this
station trade nothing?", and it silently stopped answering: four gates
added to entry_sim between 2026-08-05 and 2026-08-09 had no entry in
engine._REASON_PREFIXES, so the 2026-08-12 cohort run reported
`other=191` for nine stations and the cause had to be reverse-engineered
from source.

These tests make that failure mode structural rather than a thing
someone has to remember:
  - every distinct rejection/approval reason entry_sim can emit maps to a
    key that is NOT "other";
  - the prefix table stays in step with entry_sim.GATE_COUNT;
  - a budget-refused leg is counted as a budget refusal, not as approved.
"""

import ast
from pathlib import Path

from backtest import engine, entry_sim

PKG = Path(__file__).resolve().parent.parent


def _literal_reason_prefixes():
    """
    The literal head of every reason string the entry path can emit, taken
    from source so a new gate shows up here without anyone updating a
    fixture list. f-strings contribute their leading literal chunk, which
    is what _reason_key prefix-matches on.

    Scans entry_manager too, not just entry_sim: the collection-first gate
    builds its EntryDecision in entry_manager.collection_only_reason /
    collection_only_decision and entry_sim returns it verbatim, so an
    entry_sim-only scan missed "Collection-only" -- which is exactly the
    string nine stations were rejected with while the funnel said "other".
    """
    sources = [
        PKG / "backtest" / "entry_sim.py",
        PKG / "entry_manager.py",
    ]
    tree = ast.parse("\n".join(p.read_text(encoding="utf-8") for p in sources))
    heads = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "reason":
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            heads.append(value.value)
        elif isinstance(value, ast.JoinedStr):
            first = value.values[0] if value.values else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                heads.append(first.value)
    def head_of(node):
        """Leading string literal of a str constant or f-string, else None."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            first = node.values[0] if node.values else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
        return None

    # _rejected(...) call sites pass the reason positionally.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_rejected":
            if node.args:
                heads.append(head_of(node.args[0]))

    # Functions that RETURN a reason string rather than assigning one --
    # entry_manager.collection_only_reason is the load-bearing case: its
    # returns are the collection-first gate's wording, and an earlier
    # version of this scanner missed them entirely while still passing,
    # which is how "Collection-only" stayed unmapped.
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and "reason" in node.name):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Return) and inner.value is not None:
                heads.append(head_of(inner.value))

    return [h for h in heads if h and h.strip()]


def test_every_entry_sim_reason_is_classified():
    unclassified = [h for h in _literal_reason_prefixes() if engine._reason_key(h) == "other"]
    assert not unclassified, (
        "entry_sim can emit reason strings the funnel files under 'other' -- add them to "
        f"engine._REASON_PREFIXES so the funnel names the gate: {unclassified}"
    )


def test_prefix_table_covers_the_gate_count():
    # One "Approved" entry is the approval site; the rest are rejections.
    # entry_sim.GATE_COUNT counts every decision site including approval.
    assert len(engine._REASON_PREFIXES) >= entry_sim.GATE_COUNT - 1, (
        f"_REASON_PREFIXES has {len(engine._REASON_PREFIXES)} entries but entry_sim declares "
        f"GATE_COUNT={entry_sim.GATE_COUNT} decision sites -- a gate is probably unmapped."
    )


def test_budget_refusal_is_not_counted_as_approved():
    approved_then_refused = (
        "Approved: +22.0% net EV at $75.00 (mature station). "
        "[rejected: station/day budget exhausted ($250.00 of $250.00 already deployed)]"
    )
    assert engine._reason_key(approved_then_refused) == "station_day_budget_exhausted"
    assert engine._reason_key("Approved: +22.0% net EV at $75.00 (mature station).") == "approved"
