"""alerts.send(): ntfy POST, no-op without a topic, never raises."""
import urllib.request

import alerts


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append((req, timeout))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


def test_no_topic_is_a_silent_no_op(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    sent = _capture(monkeypatch)
    assert alerts.send("t", "m") is False
    assert sent == []


def test_posts_to_the_topic(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "polyweather-test")
    sent = _capture(monkeypatch)
    assert alerts.send("Brake: daily loss", "body", priority="high") is True
    req, timeout = sent[0]
    assert req.full_url == "https://ntfy.sh/polyweather-test"
    assert req.get_method() == "POST"
    assert req.data == b"body"
    assert req.get_header("Title") == "Brake: daily loss"
    assert req.get_header("Priority") == "high"
    assert timeout == 5


def test_a_network_failure_never_raises(monkeypatch, capsys):
    monkeypatch.setenv("NTFY_TOPIC", "polyweather-test")

    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert alerts.send("t", "m") is False
    assert "could not send alert" in capsys.readouterr().out
