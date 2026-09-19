"""
Wave 3 item 3b. polyweather-dashboard.service ran as root (no User= line),
and its generators opened the trading database read-write with lazy DDL: a
root-owned journal beside a ubuntu-owned database, or a root-owned empty
file where the daemon expected its own, was one timer tick away. The unit
now runs as ubuntu, the web root belongs to ubuntu, and the generators are
pinned by AST to never declare themselves writers.
"""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SETUP = REPO / "deploy" / "setup_dashboard.sh"
GENERATORS = sorted((REPO / "deploy").glob("generate_*.py"))


def _service_block(script):
    start = script.index("polyweather-dashboard.service")
    block = script[start:]
    block = block[block.index("[Service]"):]
    return block[:block.index("UNIT")]


def test_the_dashboard_unit_runs_as_ubuntu():
    script = SETUP.read_text(encoding="utf-8")
    block = _service_block(script)
    assert "User=ubuntu" in block
    assert "Type=oneshot" in block
    exec_line = next(l for l in script.splitlines() if l.startswith("ExecStart="))
    assert "generate_dashboard.py --region asia" in exec_line   # unchanged command line


def test_the_web_root_is_handed_to_ubuntu_idempotently():
    script = SETUP.read_text(encoding="utf-8")
    assert "chown -R ubuntu:ubuntu /var/www/html" in script
    assert "usermod -aG systemd-journal ubuntu" in script
    # Re-runnable: the generators are copied from the repo, not mv'd from $HOME.
    assert "sudo mv /home/ubuntu/" not in script
    assert 'sudo cp "$APP_DIR/deploy/$gen" /usr/local/bin/$gen' in script


def test_three_generators_exist_and_none_is_a_writer():
    assert [g.name for g in GENERATORS] == [
        "generate_backtest_dashboard.py", "generate_dashboard.py", "generate_realmoney_dashboard.py",
    ]
    for gen in GENERATORS:
        tree = ast.parse(gen.read_text(encoding="utf-8"))
        offenders = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Attribute) and n.func.attr in ("set_writable", "migrate", "_connect", "_db"))
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "connect"
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "sqlite3")
            )
        ]
        assert not offenders, f"{gen.name} touches storage beyond its public readers at lines {offenders}"
