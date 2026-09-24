"""
contract_rules.py -- GAP 7: read each market's own rules text before trading it.

Every station's thermometer, settlement source, unit and rounding are
hand-curated in config.STATIONS. Polymarket can re-point a city to another
station or source at any time, and until this module nothing read the rules
to notice. check_contract() compares one Gamma event against the station's
expected fingerprint; evaluate() adds the history check, persists, alerts,
and returns the status ev_engine carries to the scheduler. Only VALID may
produce ENTRIES. Exits and settlement never consult this.

WHERE THE RULES LIVE (read off 70 live events, 2026-09-24/25): the event's
`description` and `resolutionSource`, repeated verbatim on every bucket
market. The station is identified by the source URL -- NOAA
`weather.gov/wrh/timeseries?site=<icao>`, Wunderground
`wunderground.com/history/daily/<slug>`, or the HK Observatory climate page
(whose station is named in the "recorded by the Hong Kong Observatory"
clause instead). Unit: "in degrees Celsius|Fahrenheit". Precision: "measures
temperatures to whole degrees ..." or "... to one decimal place".

WHAT IS HASHED, AND WHY THE WHOLE TEXT. Across consecutive days the ONLY
difference in any of the 35 stations' text was the date clause
("on 25 Sep '26"). So the date is masked and everything else is hashed: the
fallback source, the dead-data "lowest bracket" rule and the revision window
all decide settlement but sit outside the fingerprint, and a whole-text hash
is the only way a change to them is noticed. A change makes that ONE
station-day UNCERTAIN (RULES_CHANGED) and alerts; the next day compares
against the new text and is VALID again, so a benign template edit costs one
day and a human look, never a permanent halt.
"""
import hashlib
import re
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

import alerts
import storage

VALID, UNCERTAIN, INVALID = "VALID", "UNCERTAIN", "INVALID"
_RANK = {VALID: 0, UNCERTAIN: 1, INVALID: 2}

# family -> (URL regex capturing the station id, expected id for a station)
SOURCES = {
    "noaa": (re.compile(r"weather\.gov/wrh/timeseries\?site=([A-Za-z0-9]+)", re.I),
             lambda st: st.icao.lower()),
    "wunderground": (re.compile(r"wunderground\.com/history/daily/([A-Za-z0-9_/-]+)", re.I),
                     lambda st: st.wunderground_slug.lower()),
    "hko": (re.compile(r"weather\.gov\.hk/en/cis/(climat)\.htm", re.I),
            lambda st: "climat"),
}
_HKO_STATION = "hong kong observatory"
_RECORDED_AT = re.compile(r"recorded (?:by |at )(?:NOAA at )?(?:the )?(.+?) in degrees", re.I)
_UNIT_RES = (
    re.compile(r"in degrees (Celsius|Fahrenheit)", re.I),
    re.compile(r"measures temperatures (?:to whole degrees|in) (Celsius|Fahrenheit)", re.I),
)
_PRECISION = re.compile(r"measures temperatures (to whole degrees|in \w+ to one decimal place)", re.I)
_DATE = re.compile(r"\b\d{1,2} [A-Z][a-z]{2} '\d{2}\b")

_alerted = set()  # (icao, UTC date): one alert per station per UTC day, per process


def _texts(event) -> Tuple[List[str], List[str]]:
    """(distinct non-empty descriptions, distinct non-empty resolutionSource values)."""
    if not isinstance(event, dict):
        return [], []
    objs = [event] + [m for m in (event.get("markets") or []) if isinstance(m, dict)]
    descs = sorted({o["description"].strip() for o in objs if isinstance(o.get("description"), str) and o["description"].strip()})
    srcs = sorted({o["resolutionSource"].strip() for o in objs if isinstance(o.get("resolutionSource"), str) and o["resolutionSource"].strip()})
    return descs, srcs


def normalised_rules(event) -> Optional[str]:
    """The text that is hashed: date masked, whitespace collapsed. None if there is none."""
    descs, srcs = _texts(event)
    if not descs:
        return None
    norm = sorted({" ".join(_DATE.sub("<DATE>", d).split()) for d in descs})
    return "\n\n".join(norm + ["resolutionSource: " + s for s in srcs])


def rules_hash(event) -> Optional[str]:
    text = normalised_rules(event)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def check_contract(station, event) -> Tuple[str, List[str]]:
    """
    PURE. (status, sorted reason codes) for one Gamma event against the
    station's fingerprint. Every bucket market's own description is checked,
    because each bucket resolves on its own text.
    """
    descs, srcs = _texts(event)
    if not descs:
        return UNCERTAIN, ["RULES_MISSING"]

    found = {}  # code -> worst status

    def flag(code, status):
        if _RANK[status] > _RANK.get(found.get(code), -1):
            found[code] = status

    family = station.contract_source
    rx, expected_id = SOURCES[family]
    want_unit = "fahrenheit" if station.bucket_unit == "F" else "celsius"
    want_precision = "one decimal" if station.bucket_edge_mode == "floor" else "whole degrees"

    for src in srcs:
        if not rx.search(src):
            flag("UNKNOWN_SOURCE", INVALID)
    for text in descs:
        if not rx.search(text):
            flag("UNKNOWN_SOURCE", INVALID)
        # Every station-bearing URL, in ANY known family, must name this station.
        for fam, (frx, fexp) in SOURCES.items():
            for sid in frx.findall(text) + [s for src in srcs for s in frx.findall(src)]:
                if sid.lower() != fexp(station):
                    flag("UNKNOWN_STATION", INVALID)
        if family == "hko":
            named = [n.strip().lower() for n in _RECORDED_AT.findall(text)]
            if not named or any(n != _HKO_STATION for n in named):
                flag("UNKNOWN_STATION", INVALID)
        units = {u.lower() for r in _UNIT_RES for u in r.findall(text)}
        if not units:
            flag("UNIT_MISMATCH", UNCERTAIN)
        elif units != {want_unit}:
            flag("UNIT_MISMATCH", INVALID)
        precision = [p.lower() for p in _PRECISION.findall(text)]
        if not precision:
            flag("PRECISION_MISMATCH", UNCERTAIN)
        elif any(want_precision not in p for p in precision):
            flag("PRECISION_MISMATCH", INVALID)

    status = max(found.values(), key=_RANK.get, default=VALID)
    return status, sorted(found)


def evaluate(station, target_date: date, event, slug: Optional[str] = None) -> Tuple[str, List[str]]:
    """
    check_contract + RULES_CHANGED against stored history + persist + alert.
    NEVER RAISES for storage or alert failures: history is best-effort (a
    store that cannot be read skips only the change check, loudly), the
    fingerprint verdict stands regardless.
    """
    status, reasons = check_contract(station, event)
    h = rules_hash(event)
    try:
        ref = storage.contract_reference_hashes(station.icao, target_date)
        if h and any(r and r != h for r in (ref["prev"], ref["first"])):
            reasons = sorted(set(reasons) | {"RULES_CHANGED"})
            if _RANK[status] < _RANK[UNCERTAIN]:
                status = UNCERTAIN
        if ref["last_row"] != (h, status, ",".join(reasons)):
            storage.record_contract_check(
                station.icao, target_date, slug, h, status, ",".join(reasons), normalised_rules(event),
            )
    except Exception as exc:  # noqa: BLE001 -- history must never take a cycle down
        print(f"[contract_rules] {station.icao} {target_date}: rules history unavailable "
              f"({type(exc).__name__}: {exc}) -- change detection SKIPPED this cycle")

    if status != VALID:
        print(f"[contract_rules] {station.icao} {target_date}: contract {status} "
              f"({', '.join(reasons)}) -- no new entries for this station-day")
        key = (station.icao, datetime.now(timezone.utc).date())
        if key not in _alerted:
            _alerted.add(key)
            alerts.send(
                f"Contract rules {status}: {station.icao}",
                f"{station.icao} {target_date} ({slug or 'slug unknown'}): {', '.join(reasons)}. "
                f"Entries refused for this station-day; exits unaffected. "
                f"Diff contract_checks.rules_text to see what changed.",
                priority="high",
            )
    return status, reasons
