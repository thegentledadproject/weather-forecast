"""
Plan section 12 acceptance tests that had no test (gap audit 2026-09-24):
  #14 model independence, #9 position state-machine integrity,
  #11 idempotent external events. (#16, the kill-switch drill, lives in
  test_live_brakes.py beside the fixtures it reuses.)
"""
import ast
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import executor
import storage
from models import ExitDecision, Position

PKG = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# #14 MODEL INDEPENDENCE. The weather model -- calibration.py (mu/sd),
# probability.py (bucket probabilities: model_prob) and pipeline.py (which
# builds the estimate from forecast clients) -- must not import anything
# that reads or reacts to market prices, directly or transitively.
#
# ponytail: import-time closure. The model modules' OWN function-level
# imports are followed too (calibration's lazy `import storage`), but a
# transitively reached module contributes only its module-level imports --
# storage.open_position's lazy `import risk_manager` is a write path the
# model never calls. Tighten if the model ever starts calling writers.
# ---------------------------------------------------------------------------
MODEL_MODULES = ("calibration", "probability", "pipeline")
MARKET_MODULES = {
    "ev_engine", "market_discovery", "executor", "entry_manager", "risk_manager",
    "position_manager", "probability_calibration", "cohort_monitor", "promotion_dossier",
    "clients.market_client", "clients.wallet_client", "clients.onchain_client",
    "clients.redemption_client", "backtest.price_store",
}


def _path(mod: str):
    base = PKG.joinpath(*mod.split("."))
    for p in (base.with_suffix(".py"), base / "__init__.py"):
        if p.exists():
            return p
    return None  # stdlib / third-party


def _imports(mod: str, include_nested: bool) -> set:
    path = _path(mod)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
    nodes = ast.walk(tree) if include_nested else _module_level(tree.body)
    out = set()
    for n in nodes:
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".") if pkg else []
                parts = parts[: len(parts) - (n.level - 1)] if n.level > 1 else parts
                base = ".".join(p for p in parts + ([n.module] if n.module else []) if p)
            out.add(base)
            out.update(f"{base}.{a.name}" for a in n.names)  # may be submodules
    found = set()
    for name in out:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):  # importing a.b runs a/__init__ too
            if _path(".".join(parts[:i])):
                found.add(".".join(parts[:i]))
    return found


def _module_level(body):
    """Statements executed at import: the body, minus def/class bodies."""
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from ast.walk(stmt)


def _closure(root: str) -> dict:
    """{module: the module that pulled it in}, from root."""
    seen, stack = {root: None}, [root]
    while stack:
        mod = stack.pop()
        for dep in _imports(mod, include_nested=(mod == root)):
            if dep not in seen:
                seen[dep] = mod
                stack.append(dep)
    return seen


@pytest.mark.parametrize("root", MODEL_MODULES)
def test_weather_model_never_imports_market_code(root):
    closure = _closure(root)
    bad = {m: via for m, via in closure.items() if m in MARKET_MODULES}
    assert not bad, f"{root} reaches market code: {bad}"


def test_the_independence_check_can_fail():
    """The walker really follows imports: ev_engine is market code AND reaches probability."""
    assert "probability" in _closure("ev_engine")
    assert "clients.market_client" in _closure("ev_engine")


# ---------------------------------------------------------------------------
# #9 STATE MACHINE. open -> closed_* allowed once; closed -> anything refused
# with the first close's row intact.
# ---------------------------------------------------------------------------
def _pos(pid="P1", mode="paper", status="open"):
    return Position(
        position_id=pid, station_icao="WSSS", target_date=date(2026, 9, 20), bucket_c=32,
        side="YES", entry_price=0.40, size_usd=4.0, entry_time="2026-09-19T21:00:00+00:00",
        status=status, token_id="TOK", is_paper=(mode != "live"), size_shares=10.0,
        execution_mode=mode,
    )


def _row(pid="P1"):
    with sqlite3.connect(storage.config.DB_PATH) as c:
        return c.execute("SELECT status, exit_price, exit_time, exit_reason FROM positions "
                         "WHERE position_id = ?", (pid,)).fetchone()


@pytest.mark.parametrize("status", ["closed_resolution", "closed_stop_loss",
                                    "closed_take_profit", "closed_trailing_stop"])
def test_open_to_any_closed_status_is_allowed(tmp_db, status):
    storage.open_position(_pos())
    assert storage.close_position("P1", 1.0, "t1", status, "first") is True
    assert _row() == (status, 1.0, "t1", "first")


@pytest.mark.parametrize("second", ["closed_resolution", "closed_stop_loss", "open"])
def test_closed_to_anything_is_refused_without_touching_the_row(tmp_db, second):
    storage.open_position(_pos())
    storage.close_position("P1", 1.0, "t1", "closed_resolution", "first")
    assert storage.close_position("P1", 0.0, "t2", second, "second") is False
    assert _row() == ("closed_resolution", 1.0, "t1", "first")


def test_closing_a_missing_row_changes_nothing(tmp_db):
    assert storage.close_position("nope", 1.0, "t", "closed_resolution", "x") is False


# ---------------------------------------------------------------------------
# #11 IDEMPOTENT EXTERNAL EVENTS. A settlement/resolution delivered twice,
# and a fill recorded twice, count once.
# ---------------------------------------------------------------------------
def _book_pnl():
    with sqlite3.connect(storage.config.DB_PATH) as c:
        return c.execute("SELECT COUNT(*), COALESCE(SUM(size_usd * (exit_price - entry_price) "
                         "/ entry_price), 0) FROM positions WHERE status != 'open'").fetchone()


def _resolution(price):
    return ExitDecision(position_id="P1", should_exit=True, reason="resolution",
                        current_price=price, pnl_pct=price / 0.40 - 1)


def test_duplicated_resolution_close_counts_once(tmp_db, capsys):
    storage.open_position(_pos())
    executor.close_position(_pos(), _resolution(1.0), status="closed_resolution",
                            exit_reason="market_resolved")
    once = _book_pnl()
    executor.close_position(_pos(), _resolution(0.0), status="closed_resolution",
                            exit_reason="market_resolved")
    assert _book_pnl() == once == (1, pytest.approx(6.0))
    assert "already closed" in capsys.readouterr().out


def test_duplicated_settlement_record_keeps_one_row(tmp_db):
    for _ in range(2):
        storage.save_settled_bucket("WSSS", date(2026, 9, 20), 32, 27, 37, "market_settlement")
    assert len(storage.load_settled_buckets("WSSS")) == 1


def test_duplicated_fill_record_is_one_position(tmp_db):
    storage.open_position(_pos())
    storage.open_position(_pos())
    assert len(storage.load_open_positions()) == 1


def test_a_replayed_fill_cannot_reopen_a_closed_position(tmp_db):
    """INSERT OR REPLACE used to let a re-recorded fill wipe the close."""
    storage.open_position(_pos())
    storage.close_position("P1", 1.0, "t1", "closed_resolution", "first")
    before = _book_pnl()
    storage.open_position(_pos())
    assert _row() == ("closed_resolution", 1.0, "t1", "first")
    assert _book_pnl() == before and storage.load_open_positions() == []
