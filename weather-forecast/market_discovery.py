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
from typing import Dict, Optional, Tuple

import requests

import config
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


# A bucket number, and ONLY a number that is explicitly marked as degrees:
# "31°C", "31 °C", "36°". The degree marker is not decoration here, it is
# the entire guard. The old pattern made the marker optional and took the
# FIRST match, which is catastrophic on the question-text fallback: "Will
# the highest temperature in Tokyo on August 6, 2026 be 27°C or below?"
# parsed as bucket 6 (the calendar day). Requiring "°" throws out dates,
# years and stray digits; taking the LAST match throws out anything that
# still sneaks past (a label that mentions the date after the degrees).
_BUCKET_NUM_RE = re.compile(r"(\d+)\s*°")
_OR_BELOW_RE = re.compile(r"or\s+(below|lower|less)", re.IGNORECASE)
_OR_ABOVE_RE = re.compile(r"or\s+(above|higher|more)", re.IGNORECASE)

# Plausibility band for a parsed bucket, in whole degrees C. Every
# registered city's live window sits inside 25..40, and Polymarket only
# drifts it a few degrees seasonally, so 5..50 never vetoes a real bucket
# while still rejecting a year ("2026°"? never seen, but the cost of
# allowing it is a silently wrong map) or any other numeric artifact.
MIN_PLAUSIBLE_BUCKET_C = 5
MAX_PLAUSIBLE_BUCKET_C = 50


def _degree_numbers(label: str) -> list:
    """Every plausible degree-marked number in a label, in order of appearance."""
    return [
        n for n in (int(m) for m in _BUCKET_NUM_RE.findall(label))
        if MIN_PLAUSIBLE_BUCKET_C <= n <= MAX_PLAUSIBLE_BUCKET_C
    ]


def parse_bucket_label(
    market: dict,
    bucket_min: Optional[int] = None,
    bucket_max: Optional[int] = None,
) -> Optional[int]:
    """
    Extract the whole-degree-C bucket this market represents, from
    whichever field actually carries it. Tries groupItemTitle first
    (Polymarket's typical field for a sub-market's short label within
    a grouped event), then falls back to parsing the "question" text.
    Returns None if neither yields a parseable bucket -- callers skip
    that market rather than guessing.

    THE EDGE BUCKETS PARSE THEMSELVES. "27°C or below" and "37°C or
    higher" both carry their own bucket number, and that number IS the
    bucket -- it is not a synonym for "whatever the caller thinks the
    range floor/ceiling is". bucket_min/bucket_max are therefore only a
    LAST-RESORT fallback for an edge label that carries no parseable
    number at all, and default to None (no fallback) for callers that
    would rather see a honest miss.

    WHY THIS IS WORTH BEING FUSSY ABOUT: a mis-parsed or unparseable
    label doesn't just lose one bucket, it manufactures a trade. A bucket
    missing from the token map gets model_prob 0.0 in ev_engine's
    lookup, so its NO side shows raw_edge ~ 1.0 - 0.0 - 0.80 ~ 0.20 --
    under MAX_PLAUSIBLE_RAW_EDGE (0.25), over MIN_ABS_RAW_EDGE, and
    through both EV windows. Every guard in entry_manager passes, and
    the system confidently sizes a "20-cent edge" that exists only
    because a regex read a date. Discovery correctness is a risk
    control, not a convenience.
    """
    # groupItemTitle first (the short, authoritative label); the question
    # text is a fallback because it is prose and prose contains dates.
    labels = [market.get("groupItemTitle") or "", market.get("question") or ""]

    for label in labels:
        numbers = _degree_numbers(label)
        if numbers:
            return numbers[-1]

    # Nothing degree-marked anywhere. An edge label can still be PLACED
    # from the caller's bounds if it supplied any -- strictly a fallback,
    # never the primary path.
    for label in labels:
        if _OR_BELOW_RE.search(label) and bucket_min is not None:
            return bucket_min
        if _OR_ABOVE_RE.search(label) and bucket_max is not None:
            return bucket_max

    return None


def derive_bucket_bounds(token_map: Dict[int, dict]) -> Optional[Tuple[int, int]]:
    """
    The (min, max) bucket bounds implied by a DISCOVERED token map --
    the authoritative bounds for the trading path, since Polymarket
    re-centers a city's window seasonally and StationConfig's
    bucket_min_c/max_c are only a cross-check (Singapore moved 25-35 ->
    27-37 between July and August 2026 while config still said 25/35).

    Returns None -- meaning "this map is not a well-formed event, do not
    trade it" -- unless the keys are CONTIGUOUS and number exactly
    config.EXPECTED_BUCKET_COUNT. Both conditions matter for the same
    reason: a gap in the middle (one market whose label failed to parse)
    or a short map (discovery only found 9 of 11) leaves buckets the
    event really lists absent from the model's support, and every absent
    bucket becomes a phantom ~0.20 NO-side edge that clears every risk
    gate (see parse_bucket_label). A partial map is worse than no map,
    because no map merely skips the cycle.
    """
    if not token_map:
        return None

    keys = sorted(token_map)
    lo, hi = keys[0], keys[-1]
    if len(keys) != hi - lo + 1:
        return None  # non-contiguous: at least one bucket is missing from the middle
    if len(keys) != config.EXPECTED_BUCKET_COUNT:
        return None  # right shape, wrong size: not the 11-outcome event we know how to price
    return lo, hi


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
    bucket_min: Optional[int] = None,
    bucket_max: Optional[int] = None,
    timeout: int = 10,
) -> Optional[dict]:
    """
    Report whether a station's market is closed/resolved, per Gamma.

    Pass bucket_c to ask about ONE bucket's market. bucket_min/bucket_max
    are the same last-resort fallback parse_bucket_label uses for an edge
    label carrying no degree number -- pass the STATION's own cross-check
    bounds, never the frozen module-level globals, so a station whose
    window has drifted isn't matched against Singapore's old range. Omit
    bucket_c to ask about the event as a whole.

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
    bucket_min: Optional[int] = None,
    bucket_max: Optional[int] = None,
) -> Dict[int, Dict[str, str]]:
    """
    Top-level entry point. Returns whatever subset of
    {bucket_c: {"yes_token_id":, "no_token_id":}} could be discovered
    and parsed -- may be a partial map if some buckets fail to parse
    or the event isn't found at all (empty dict in that case).

    A partial map is NOT tradeable: run it through derive_bucket_bounds()
    before pricing anything off it, and skip the station-day if that
    returns None. bucket_min/bucket_max are only parse_bucket_label's
    edge-label fallback (pass the station's own cross-check bounds), not
    a filter -- a discovered bucket outside them is real and is kept, and
    the bounds mismatch is what derive_bucket_bounds surfaces to the
    caller as config drift.
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
