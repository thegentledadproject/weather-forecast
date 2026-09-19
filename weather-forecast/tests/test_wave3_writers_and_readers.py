"""
Wave 3 item 3a, the callers. The daemon migrates and declares itself the
writer ONCE, at the top of run_forever, and primes the git sha there too (the
Wave 1 minor: _config_sha shelled out to git on the entry path). Exactly four
modules may call storage.set_writable -- the daemon and the three operator
scripts that write -- and the deploy script stops the daemon, backs up, runs
migrate() as ubuntu, restarts, and only THEN starts the dashboard.
"""
import ast
import pathlib

import config
import scheduler
import storage

PKG = pathlib.Path(__file__).resolve().parents[1]
REPO = PKG.parent

WRITERS = {"scheduler.py", "manual_trigger.py", "bucket_bias.py", "main.py"}


def _py_files():
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG)
        if rel.parts[0] in ("tests", ".venv", "docs") or "__pycache__" in rel.parts:
            continue
        yield rel.as_posix(), path
    for path in sorted((REPO / "deploy").glob("*.py")):
        yield "../deploy/" + path.name, path


def _calls(path, attr):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Attribute) and n.func.attr == attr)
            or (isinstance(n.func, ast.Name) and n.func.id == attr)
        )
    ]


def test_only_the_daemon_and_the_three_operator_writers_set_writable():
    found = {rel for rel, path in _py_files() if rel != "storage.py" and _calls(path, "set_writable")}
    assert found == WRITERS, f"set_writable call sites: {sorted(found)}"


def test_only_the_daemon_migrates_in_code():
    found = {rel for rel, path in _py_files() if rel != "storage.py" and _calls(path, "migrate")}
    assert found == {"scheduler.py"}, f"migrate() call sites: {sorted(found)}"


def test_boot_migrates_then_sets_writable_then_primes_the_sha(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "boot.sqlite3"))
    monkeypatch.setattr(storage, "_WRITABLE", False)
    monkeypatch.setattr(scheduler, "_config_sha_cache", {})
    monkeypatch.setattr(config, "_current_git_sha", lambda: "abc123")
    # No stations -> run_forever returns right after booting.
    monkeypatch.setattr(scheduler, "stations_by_utc_offset", lambda station_icaos=None: {})

    scheduler.run_forever()

    assert storage.is_writable()
    assert "positions" in storage.schema_summary()
    assert scheduler._config_sha_cache == {"sha": "abc123"}
    out = capsys.readouterr().out
    assert "boot: storage migrated" in out and "abc123" in out


def test_boot_order_is_migrate_before_writable(monkeypatch):
    order = []
    monkeypatch.setattr(storage, "migrate", lambda: order.append("migrate"))
    monkeypatch.setattr(storage, "set_writable", lambda flag=True: order.append(("writable", flag)))
    monkeypatch.setattr(storage, "schema_summary", lambda: "stub")
    monkeypatch.setattr(scheduler, "_config_sha", lambda: order.append("sha") or "s")
    scheduler._boot_storage()
    assert order == ["migrate", ("writable", True), "sha"]


def test_deploy_script_stops_backs_up_migrates_restarts_then_starts_the_dashboard():
    script = (REPO / "deploy" / "deploy_daemon.sh").read_text(encoding="utf-8")
    i_guard = script.index("refusing to demote a daemon holding live positions")
    i_stop = script.index("systemctl stop $SERVICE")
    i_backup = script.index(".backup(")
    i_migrate = script.index('import storage; storage.migrate()')
    i_restart = script.index("systemctl restart $SERVICE")
    i_dash = script.index("systemctl start polyweather-dashboard.service")
    assert i_guard < i_stop < i_backup < i_migrate < i_restart < i_dash
    # The generator-copy block no longer starts the dashboard: one start, at the end.
    assert script.count("systemctl start polyweather-dashboard.service") == 1
    # migrate runs as the script's own user (ubuntu), never under sudo.
    migrate_line = next(l for l in script.splitlines() if "storage.migrate()" in l)
    assert not migrate_line.lstrip().startswith("sudo")
    assert '"$VENV/bin/python" -c "import storage; storage.migrate()' in migrate_line
    # Backup through the sqlite backup API (a python heredoc), never cp on a live file.
    assert "import sqlite3" in script[i_backup - 300:i_backup]
