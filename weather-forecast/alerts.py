"""
alerts.py

One-way operator alerts via ntfy.sh. The topic comes from the NTFY_TOPIC
environment variable; unset means alerts are off (a no-op, not an error).

send() NEVER RAISES. An alert is a side channel: it must never be the reason
a trade path, an exit or a settlement crashes. Failures are printed and
swallowed.
"""
import os
import urllib.request

NTFY_URL = "https://ntfy.sh/"
TIMEOUT_S = 5


def send(title: str, message: str, priority: str = "default") -> bool:
    """POST one alert. Returns True if it was delivered, False otherwise."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    try:
        req = urllib.request.Request(
            NTFY_URL + topic,
            data=message.encode("utf-8"),
            # HTTP headers are latin-1; drop what cannot be sent rather than fail.
            headers={"Title": title.encode("latin-1", "replace").decode("latin-1"),
                     "Priority": priority},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S):
            return True
    except Exception as exc:  # noqa: BLE001 - see module docstring
        print(f"[alerts] could not send alert {title!r}: {type(exc).__name__}: {exc}")
        return False
