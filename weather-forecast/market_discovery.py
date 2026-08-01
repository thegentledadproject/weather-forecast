"""
market_discovery.py

PURPOSE
-------
Closes the token_map gap flagged in ev_engine.py and position_manager.py:
automatically discovers a station's Polymarket temperature-bracket
event for a given date, and builds the {bucket_c: {"yes_token_id":,
"no_token_id":}} mapping those modules need, instead of requiring it
by hand.

How it works, per Polymarket's documented Gamma API structure:
  1. Build the event's slug: "highest-temperature-in-{city}-on-{month}-{day}-{year}"
     (confirmed pattern from real Polymarket URLs, e.g.
     "highest-temperature-in-singapore-on-july-21-2026")
  2. GET https://gamma-api.polymarket.com/events?slug=<slug>
  3. The event contains a "markets" array -- one market per bucket.
     Each market has "outcomes" and "clobTokenIds" as STRING-ENCODED
     JSON (confirmed: these need a second json.loads, not just one)
  4. Parse each market's bucket label and outcomes/clobTokenIds into
     one token_map entry.

The same event lookup also answers "has this market closed/resolved?"
via get_market_state() -- position_manager.py needs that to tell a
resolved market's 0.00/1.00 print apart from a genuine price collapse,
so a resolution is never booked as a stop-loss.

CONFIDENCE NOTE
----------------
The event/market discovery shape above (slug lookup, "markets" array,
clobTokenIds as double-JSON-encoded) is grounded in Polymarket's
documented Gamma API structure, cross-checked across multiple sources
-- higher confidence than market_client.py's CLOB endpoints, which
were written from documentation alone. What is NOT independently
confirmed here is exactly which field carries each market's bucket
label (this code tries "groupItemTitle" first, then falls back to
regex-parsing "question") -- that field name should be verified
against one real live response before trusting this in production.
Every parse step fails soft and logs rather than crashing, and
partial results (some buckets discovered, others not) are returned
rather than discarded.

DEPENDENCIES
------------
requests, re, json   (pip install requests)
config.py, models.py (local)
"""

import json
import re
from datetime import date
from typing import Dict, Optional

import requests

from models import StationConfig

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


def build_event_slug(station: StationConfig, target_date: date) -> str:
    """
    Build the Gamma API event slug for a station's temperature-bracket
    market on a given date. Confirmed pattern from real Polymarket
    URLs found during research, e.g.:
      "highest-temperature-in-singapore-on-july-21-2026"
    """
    month_name = target_date.strftime("%B").lower()
    return f"highest-temperature-in-{station.polymarket_city_slug}-on-{month_name}-{target_date.day}-{target_date.year}"


def fetch_event(slug: str, timeout: int = 10) -> Optional[dict]:
    """
    Fetch one event by slug from Gamma. Returns the event dict (with
    its nested "markets" array) or None on failure/not-found.
    """
    try:
        resp = requests.get(
            f"{GAMMA_API_BASE}/events",
            params={"slug": slug},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload:
            print(f"[market_discovery] no event found for slug '{slug}'")
            return None
        # Gamma's /events?slug= returns a list; the exact-slug match is
        # expected to be the only (or first) entry.
        return payload[0] if isinstance(payload, list) else payload
    except (requests.RequestException, ValueError, IndexError) as exc:
        print(f"[market_discovery] fetch_event failed for slug '{slug}': {exc}")
        return None


# Matches bucket labels like "31°C", "31", "25 or below", "35 or above",
# "25° or below". Deliberately permissive -- real label formatting has
# not been confirmed against a live response (see module docstring).
_BUCKET_NUM_RE = re.compile(r"(\d+)\s*°?\s*C?")
_OR_BELOW_RE = re.compile(r"or\s+(below|lower|less)", re.IGNORECASE)
_OR_ABOVE_RE = re.compile(r"or\s+(above|higher|more)", re.IGNORECASE)


def parse_bucket_label(market: dict, bucket_min: int, bucket_max: int) -> Optional[int]:
    """
    Extract the whole-degree-C bucket this market represents, from
    whichever field actually carries it. Tries groupItemTitle first
    (Polymarket's typical field for a sub-market's short label within
    a grouped event), then falls back to parsing the "question" text.
    Returns None if neither yields a parseable bucket -- callers skip
    that market rather than guessing.
    """
    label = market.get("groupItemTitle") or market.get("question") or ""

    if _OR_BELOW_RE.search(label):
        return bucket_min
    if _OR_ABOVE_RE.search(label):
        return bucket_max

    match = _BUCKET_NUM_RE.search(label)
    if match:
        return int(match.group(1))

    return None


def parse_token_ids(market: dict) -> Optional[Dict[str, str]]:
    """
    Extract {"yes_token_id":, "no_token_id":} from one market's
    outcomes + clobTokenIds fields. Both are documented as
    STRING-ENCODED JSON arrays that map 1:1 by index -- this function
    does the double-parse and matches by outcome name (case-insensitive)
    rather than assuming index 0 is always "Yes", to be robust to any
    ordering variation.
    """
    try:
        outcomes = json.loads(market["outcomes"])
        token_ids = json.loads(market["clobTokenIds"])
        if len(outcomes) != len(token_ids):
            return None

        result = {}
        for outcome_name, token_id in zip(outcomes, token_ids):
            name_lower = outcome_name.strip().lower()
            if name_lower == "yes":
                result["yes_token_id"] = token_id
            elif name_lower == "no":
                result["no_token_id"] = token_id

        if "yes_token_id" in result and "no_token_id" in result:
            return result
        return None
    except (KeyError, ValueError, TypeError) as exc:
        print(f"[market_discovery] parse_token_ids failed: {exc}")
        return None


def _as_bool(value) -> bool:
    """
    Coerce a Gamma boolean-ish field to a real bool. Gamma returns these
    as JSON booleans in the responses seen so far, but string "true"/
    "false" has been observed on some Polymarket endpoints -- handle both
    rather than have `bool("false")` silently report a live market as
    closed. Anything unrecognised is treated as falsey (i.e. NOT closed),
    which is the safe direction: a position is only ever closed as
    resolved on a POSITIVE signal, never on an ambiguous one.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def get_market_state(
    station: StationConfig,
    target_date: date,
    bucket_c: Optional[int] = None,
    bucket_min: int = 0,
    bucket_max: int = 0,
    timeout: int = 10,
) -> Optional[dict]:
    """
    Report whether a station's market is closed/resolved, per Gamma.

    Pass bucket_c to ask about ONE bucket's market (bucket_min/bucket_max
    are then needed so the "or below"/"or above" edge labels resolve to
    real bucket numbers, same as discover_token_map). Omit it to ask
    about the event as a whole.

    Returns:
        {"slug": str, "bucket_c": Optional[int], "closed": bool,
         "closed_flag": bool, "archived": bool}
      -- where "closed" is the answer callers should act on (closed OR
      archived, since an archived market is not tradeable either).

    Returns None for UNKNOWN -- network failure, event not found, or the
    requested bucket not present in the event. Unknown is deliberately
    distinct from False: position_manager.py treats only an explicit True
    as grounds to close a position as resolved, and never raises out of
    here on a network failure (fetch_event already fails soft).
    """
    slug = build_event_slug(station, target_date)
    event = fetch_event(slug, timeout=timeout)
    if event is None:
        return None

    source = event
    if bucket_c is not None:
        markets = event.get("markets", []) or []
        source = None
        for market in markets:
            if parse_bucket_label(market, bucket_min, bucket_max) == bucket_c:
                source = market
                break
        if source is None:
            print(
                f"[market_discovery] bucket {bucket_c}°C not found in event '{slug}' "
                f"({len(markets)} market(s) listed) -- market state unknown"
            )
            return None

    closed_flag = _as_bool(source.get("closed", False))
    archived = _as_bool(source.get("archived", False))

    return {
        "slug": slug,
        "bucket_c": bucket_c,
        "closed": closed_flag or archived,
        "closed_flag": closed_flag,
        "archived": archived,
    }


def discover_token_map(
    station: StationConfig,
    target_date: date,
    bucket_min: int,
    bucket_max: int,
) -> Dict[int, Dict[str, str]]:
    """
    Top-level entry point. Returns whatever subset of
    {bucket_c: {"yes_token_id":, "no_token_id":}} could be discovered
    and parsed -- may be a partial map if some buckets fail to parse
    or the event isn't found at all (empty dict in that case).
    Callers (ev_engine.py, position_manager.py) should treat a smaller
    -than-expected map as a signal to log and investigate, not crash.
    """
    slug = build_event_slug(station, target_date)
    event = fetch_event(slug)
    if event is None:
        return {}

    markets = event.get("markets", [])
    token_map = {}

    for market in markets:
        bucket_c = parse_bucket_label(market, bucket_min, bucket_max)
        if bucket_c is None:
            print(f"[market_discovery] could not parse bucket label from market: {market.get('question', '?')}")
            continue

        ids = parse_token_ids(market)
        if ids is None:
            print(f"[market_discovery] could not parse token ids for bucket {bucket_c}")
            continue

        token_map[bucket_c] = ids

    print(f"[market_discovery] discovered {len(token_map)}/{len(markets)} buckets for '{slug}'")
    return token_map
