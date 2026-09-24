"""One provenance helper: `<git sha>[+dirty]:<mode hash>`, prefix-compatible
with the plain sha every earlier row and manifest stored."""
import config


def _fp(monkeypatch, *, dirty=False, env=None, mode=None):
    monkeypatch.setattr(config, "_current_git_sha", lambda: "a" * 40)
    monkeypatch.setattr(config, "_git_dirty", lambda: dirty)
    for k in [k for k in __import__("os").environ if k.startswith("POLYWEATHER_")]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("POLYMARKET_LIVE_TRADING", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return config.config_fingerprint(mode)


def test_prefix_is_the_git_sha(monkeypatch):
    fp = _fp(monkeypatch)
    assert fp.startswith("a" * 40 + ":") and len(fp) == 40 + 1 + 8
    assert config.git_sha_of(fp) == "a" * 40
    assert config.git_sha_of("a" * 40) == "a" * 40  # legacy rows unchanged
    assert config.git_sha_of(None) is None


def test_dirty_tree_is_marked(monkeypatch):
    fp = _fp(monkeypatch, dirty=True)
    assert fp.startswith("a" * 40 + "+dirty:")
    assert config.git_sha_of(fp) == "a" * 40


def test_effective_mode_and_host_env_change_the_hash(monkeypatch):
    base = _fp(monkeypatch, mode={"WSSS": "paper"})
    assert _fp(monkeypatch, mode={"WSSS": "paper"}) == base  # deterministic
    assert _fp(monkeypatch, mode={"WSSS": "live"}) != base
    assert _fp(monkeypatch, mode={"WSSS": "paper"}, env={"POLYWEATHER_MODE": "live"}) != base
    assert _fp(monkeypatch, mode={"WSSS": "paper"}, env={"POLYMARKET_LIVE_TRADING": "true"}) != base


def test_secrets_never_enter_the_hash(monkeypatch):
    base = _fp(monkeypatch)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xsecret")
    assert config.config_fingerprint(None) == base


def test_calibration_gate_compares_the_git_part(monkeypatch):
    """A manifest stamped with the full fingerprint still matches HEAD."""
    monkeypatch.setattr(config, "_current_git_sha", lambda: "b" * 40)
    assert config.git_sha_of("b" * 40 + "+dirty:12345678") == config._current_git_sha()


def test_every_stamping_site_uses_the_one_helper(monkeypatch):
    import scheduler
    from backtest import engine

    monkeypatch.setattr(config, "config_fingerprint", lambda mode=None: "c" * 40 + ":deadbeef")
    monkeypatch.setattr(scheduler, "_config_sha_cache", {})
    assert scheduler._config_sha() == "c" * 40 + ":deadbeef"
    assert engine._git_sha() == "c" * 40 + ":deadbeef"
