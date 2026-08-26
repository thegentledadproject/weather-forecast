"""Tests for deploy/generate_realmoney_dashboard.py.

The generator lives in deploy/, which is not on sys.path and is not a
package. It is loaded by file path -- which only works because, unlike its
two sibling generators, this module runs nothing at import time.
"""
import importlib.util
import pathlib
import sys

import pytest

_GEN_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy" / "generate_realmoney_dashboard.py"
)


def load_gen():
    """Import the generator module fresh. Must have NO import-time side effects."""
    spec = importlib.util.spec_from_file_location("generate_realmoney_dashboard", _GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_import_writes_nothing(tmp_path, monkeypatch):
    """Importing must not parse argv or write a page -- both siblings do, which
    is exactly why neither of them has a single test."""
    monkeypatch.setattr(sys, "argv", ["generate_realmoney_dashboard.py", "--out", str(tmp_path / "x.html")])
    load_gen()
    assert not (tmp_path / "x.html").exists()


def test_main_renders_a_page(tmp_path):
    """Safe without store isolation: at Task 1 main() builds no sections and
    so touches no database. From Task 8 the full-render tests take the
    isolated_stores fixture instead."""
    gen = load_gen()
    out = tmp_path / "realmoney.html"
    status = gen.main(["--out", str(out)])
    assert status == 0
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "Real-money stations" in page


def test_render_page_escapes_warnings():
    """A warning is data, not markup -- it can carry an exception string."""
    gen = load_gen()
    page = gen.render_page([], ["boom <script>alert(1)</script>"])
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --- gate 2 probe ------------------------------------------------------------
# The drop-in this probe reads also holds POLYMARKET_PRIVATE_KEY. Every test
# here exists to pin one property: the probe answers a yes/no question about
# a NAME and never surfaces a VALUE.

_BLOB = (
    b"PATH=/usr/bin\x00"
    b"POLYMARKET_PRIVATE_KEY=0xdeadbeefcafe\x00"
    b"POLYMARKET_LIVE_TRADING=true\x00"
    b"HOME=/root\x00"
)


def test_environ_blob_has_name_finds_the_name():
    gen = load_gen()
    assert gen.environ_blob_has_name(_BLOB, "POLYMARKET_LIVE_TRADING") is True


def test_environ_blob_has_name_missing_name():
    gen = load_gen()
    assert gen.environ_blob_has_name(_BLOB, "POLYMARKET_NOT_SET") is False


def test_environ_blob_has_name_returns_a_bool_not_a_value():
    """The only thing that may leave this function is True or False. A prefix
    match that returned the entry would leak the private key."""
    gen = load_gen()
    result = gen.environ_blob_has_name(_BLOB, "POLYMARKET_PRIVATE_KEY")
    assert result is True
    assert isinstance(result, bool)
    assert "0xdeadbeefcafe" not in repr(result)


def test_environ_blob_has_name_does_not_match_a_prefix():
    """POLYMARKET_LIVE must not satisfy a probe for POLYMARKET_LIVE_TRADING,
    and vice versa -- the match is on the full name up to '='."""
    gen = load_gen()
    assert gen.environ_blob_has_name(_BLOB, "POLYMARKET_LIVE") is False


def test_gate2_state_unknown_when_pid_unavailable(monkeypatch):
    gen = load_gen()
    monkeypatch.setattr(gen, "_main_pid", lambda unit: None)
    assert gen.gate2_state() == "unknown"


def test_gate2_state_unknown_when_environ_unreadable(monkeypatch):
    gen = load_gen()
    monkeypatch.setattr(gen, "_main_pid", lambda unit: 4242)
    monkeypatch.setattr(gen, "_read_environ", lambda pid: None)
    assert gen.gate2_state() == "unknown"


def test_gate2_state_present_and_absent(monkeypatch):
    gen = load_gen()
    monkeypatch.setattr(gen, "_main_pid", lambda unit: 4242)
    monkeypatch.setattr(gen, "_read_environ", lambda pid: _BLOB)
    assert gen.gate2_state() == "present"
    monkeypatch.setattr(gen, "_read_environ", lambda pid: b"PATH=/usr/bin\x00")
    assert gen.gate2_state() == "absent"
