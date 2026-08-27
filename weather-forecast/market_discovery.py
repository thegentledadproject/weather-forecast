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

import bucket_axis
import config
from bucket_axis import AXIS_C1, BucketAxis, UNIT_F
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


# Celsius: unchanged. Requiring "°" throws out dates, years and stray
# digits; taking the LAST match throws out anything that still sneaks past.
# The sign IS captured -- Toronto and Buenos Aires have sub-zero windows,
# and "-2°C" parsed as 2 is a wrong key, not a missed one.
_BUCKET_NUM_RE = re.compile(r"(-?\d+)\s*°")

# Fahrenheit: the unit letter is REQUIRED, and the range form must be
# matched as a range. This is not fussiness -- the Celsius plausibility band
# below cannot police an F axis at all (a real "9°F or below" bucket
# overlaps the day-of-month range 1-31), so the "°F" letter is what replaces
# the band as the guard against parsing a date.
_F_RANGE_RE = re.compile(r"(-?\d+)\s*-\s*(\d+)\s*°\s*F", re.IGNORECASE)
_F_SINGLE_RE = re.compile(r"(-?\d+)\s*°\s*F", re.IGNORECASE)
_C_UNIT_RE = re.compile(r"°\s*C", re.IGNORECASE)

_OR_BELOW_RE = re.compile(r"or\s+(below|lower|less)", re.IGNORECASE)
_OR_ABOVE_RE = re.compile(r"or\s+(above|higher|more)", re.IGNORECASE)

# Plausibility band per unit. The Celsius FLOOR drops from 5 to -30 because
# Toronto and Buenos Aires both run below 5°C, and the old justification
# ("every registered city's live window sits inside 25..40") stopped being
# true when Europe was registered. The Celsius CEILING stays at 50, exactly
# as it was: its job is rejecting a year ("2026°") and it still does that.
# Widening a guard nobody asked to widen is how a real bucket veto turns
# into a phantom edge.
_PLAUSIBLE_BAND = {"C": (-30, 50), "F": (-20, 130)}

# Kept as module-level names because tests and other modules read them.
MIN_PLAUSIBLE_BUCKET_C, MAX_PLAUSIBLE_BUCKET_C = _PLAUSIBLE_BAND["C"]


def _degree_numbers(label: str, unit: str = "C") -> list:
    """Every plausible degree-marked number in a label, in order of appearance."""
    lo, hi = _PLAUSIBLE_BAND[unit]
    return [
        n for n in (int(m) for m in _BUCKET_NUM_RE.findall(label))
        if lo <= n <= hi
    ]


def parse_bucket_label(
    market: dict,
    bucket_min: Optional[int] = None,
    bucket_max: Optional[int] = None,
    *,
    axis: BucketAxis = AXIS_C1,
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

    RETURNS THE BUCKET'S LOWER EDGE, in the axis's own unit. On the Celsius
    whole-degree axis that is the printed number, unchanged. On a step-2
    Fahrenheit axis, "70-71°F" is key 70 and "69°F or below" is key 68
    (= printed_top + 1 - step), so the keys form a uniform grid. See
    bucket_axis.BucketAxis.label() for the inverse.
    """
    # groupItemTitle first (the short, authoritative label); the question
    # text is a fallback because it is prose and prose contains dates.
    labels = [market.get("groupItemTitle") or "", market.get("question") or ""]

    if axis.unit == UNIT_F:
        for label in labels:
            if _C_UNIT_RE.search(label):
                continue  # a Celsius label on an F station is not ours to guess
            m = _F_RANGE_RE.search(label)
            if m:
                low, high = int(m.group(1)), int(m.group(2))
                if high - low != axis.step - 1:
                    continue  # not this axis's width -- reject, never guess
                return low
            m = _F_SINGLE_RE.search(label)
            if not m:
                continue
            n = int(m.group(1))
            lo_band, hi_band = _PLAUSIBLE_BAND[UNIT_F]
            if not lo_band <= n <= hi_band:
                continue
            if _OR_BELOW_RE.search(label):
                return n + 1 - axis.step
            return n
        return None

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


def derive_bucket_bounds(
    token_map: Dict[int, dict], step: int = 1
) -> Optional[Tuple[int, int]]:
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

    step is the market's bucket width in its own unit. At step=1 this is
    provably the same predicate as the contiguity test it replaces. At
    step=2 it additionally rejects the failure today's regex actually
    produces on an American event: "70-71°F" yields only 71, giving a
    step-1 map whose span is 10 rather than 20.
    """
    if not token_map:
        return None

    keys = sorted(token_map)
    if len(keys) != config.EXPECTED_BUCKET_COUNT:
        return None  # short or long: not the 11-outcome event we know how to price
    lo, hi = keys[0], keys[-1]
    # Every listed bucket's lower edge is a multiple of step. A uniformly
    # shifted grid (e.g. 69,71,..,89 instead of 68,70,..,88) is arithmetically
    # contiguous too, but it is not this station's grid.
    if lo % step != 0:
        return None
    if keys != list(range(lo, hi + step, step)):
        return None  # gap, off-grid key, or the wrong step for this station
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
    axis = bucket_axis.for_station(station)
    slug = build_event_slug(station, target_date)
    event = fetch_event(slug, timeout=timeout)
    if event is None:
        return None

    source = event
    if bucket_c is not None:
        markets = event.get("markets", []) or []
        source = None
        for market in markets:
            if parse_bucket_label(market, bucket_min, bucket_max, axis=axis) == bucket_c:
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
    axis = bucket_axis.for_station(station)
    slug = build_event_slug(station, target_date)
    event = fetch_event(slug)
    if event is None:
        return {}

    markets = event.get("markets", [])
    token_map = {}

    for market in markets:
        bucket_c = parse_bucket_label(market, bucket_min, bucket_max, axis=axis)
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
