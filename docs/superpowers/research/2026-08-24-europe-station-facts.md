# Europe station facts — Task 7 research (2026-08-25)

## Methodology note (read first)

**The Gamma API (`gamma-api.polymarket.com`) and `polymarket.com` itself are network-blocked in this
environment** — confirmed via three independent paths: `curl` from Bash (connection timeout, not a DNS
failure — other hosts resolve fine), the browser tool (`navigation ... denied or failed` for both hosts),
and `WebFetch` (`connect ECONNREFUSED` on the same hosts). `google.com`, Wikipedia, `wunderground.com`,
`worldweather.wmo.int`, and `aviationweather.gov` are all reachable, so this is a targeted block on the
Polymarket domain family, not a general outage.

Given that, Step 1 was done against **[polym.trade](https://polym.trade)**, a third-party Polymarket
trading terminal that mirrors Gamma event data (title, full rules/description text, bucket list, live
odds) and was reachable via `WebFetch`. This is a **substitution, not the primary source the brief asked
for**, and is flagged as such throughout. To compensate, every fact pulled from polym.trade that matters
for registry correctness (ICAO, station name, bucket count) was cross-checked against a second,
independent source:

- ICAO codes cross-checked against Wikipedia airport infoboxes.
- Station identity cross-checked against Wunderground's own history page for the same ICAO (same station
  name shown on both sides).
- METAR data availability cross-checked directly against `aviationweather.gov`'s API (the same endpoint
  `clients/metar_client.py` calls).
- WWIS city-list membership fetched directly from `worldweather.wmo.int` (not via polym.trade).

**No content fetched from any source (polym.trade, Wikipedia, Wunderground, WWIS, aviationweather.gov)
contained any text addressed to me or attempting to give me instructions.** Everything returned was
ordinary weather/market data, treated as data throughout.

**Today's date is 2026-08-25.** The `-on-august-25-2026` slug resolved (returned a live, unresolved
event) for all seven cities — no fallback to `august-26-2026` was needed.

---

## Cross-cutting finding: settlement source is NOAA, and it is the SAME station as METAR/Wunderground

Every one of the seven cities' market description reads (verbatim, from polym.trade, city name substituted):

> "This market will resolve to the temperature range that contains the highest temperature recorded by
> NOAA at the **{Station Name}** in degrees Celsius on 25 Aug '26."
>
> "The resolution source for this market will be information from NOAA, specifically the highest reading
> under the 'Temp' column for all times on this day, available here:
> `https://www.weather.gov/wrh/timeseries?site={icao}`"
>
> "The resolution source for this market measures temperatures to whole degrees Celsius (eg, 9°C)."

This confirms the brief's framing: **Europe's markets name NOAA, not Wunderground, as the resolution
source** — a real difference from the Asian stations' Polymarket text, which cites Wunderground.

**But** — and this is the part that isn't in the brief's framing — the station NOAA's `weather.gov/wrh/timeseries`
page points at, for all seven cities, has the **same station name and same ICAO** as the corresponding
Wunderground history page (`wunderground.com/history/daily/<cc>/<city>/<ICAO>`), and that same ICAO has
live METAR coverage right now on `aviationweather.gov/api/data/metar` — the exact endpoint
`clients/metar_client.py` already calls. Concretely:

| City | NOAA site= | Wunderground station header | aviationweather.gov METAR present? |
|---|---|---|---|
| London | eglc | "London City Airport Station" | Yes (checked directly, whole-degree C) |
| Paris | lfpb | "Paris-Le Bourget Airport Station" | Yes |
| Madrid | lemd | "Adolfo Suárez Madrid–Barajas Airport Station" | Yes |
| Amsterdam | eham | "Amsterdam Airport Schiphol Station" | Yes |
| Milan | limc | "Malpensa Intl Airport Station" | Yes |
| Munich | eddm | "Munich Airport Station" | Yes |
| Warsaw | epwa | "Warsaw Chopin Airport Station" | Yes |

So this is **not** the VHHH situation (a different physical station: HKO Observatory vs. the airport) and
**not** the OPKC situation (an unconfirmed station identity — market text names one station, links
another). For all seven European cities, three independent naming paths (NOAA's resolution text, NOAA's
`site=` URL parameter, Wunderground's own header) agree on the same named airport. The only difference
from the Asian default is *which website* Polymarket cites as authoritative, not *which station*.

**What I did NOT verify:** a byte-exact match between a specific day's NOAA `weather.gov/wrh/timeseries`
daily-max reading and `aviationweather.gov`'s METAR-derived daily max for that same day. Both draw on the
global METAR network and both state whole-degree-C precision, so I believe they read the same underlying
observations, but I have not diffed two numbers side by side on a settled day. **This is the one open item
that should gate `metar_ingest_mode="resolution"` vs. leaving it as a placeholder** — see per-city
recommendation below, which I present as a *recommendation with evidence*, not a confirmed fact, since the
brief is explicit that Task 7 must not paper over a gap.

`resolution_grade_source`: none of the existing source strings (`metar_daily_max`, `hko_daily_max`) is a
literal match for "NOAA's weather.gov timeseries page" as the *cited* resolution source, even though the
underlying station is the METAR station. This is a naming/design decision, not a fact I can "confirm" —
I'm flagging it as an open question for whoever writes the `StationConfig` entries rather than picking one.

---

## Fields not explicitly itemized in the brief's table, needed to compile a `StationConfig`

`StationConfig.display_name` and `StationConfig.country` have no default in the dataclass, same as
`long_term_normal_max_c` — Task 8 cannot construct an entry without them. Both are trivial derivations
from facts already confirmed above (the station name NOAA/Wunderground both independently agree on, and
the country each ICAO sits in), not new claims, but recorded explicitly here so Task 8 doesn't have to
infer them itself:

| City | `display_name` (from the confirmed station name) | `country` (matching the plain style already used, e.g. `"Singapore"`, `"Japan"`, not WWIS's formal names) |
|---|---|---|
| London | `London City Airport` | `United Kingdom` |
| Paris | `Paris-Le Bourget Airport` | `France` |
| Madrid | `Adolfo Suárez Madrid-Barajas Airport` | `Spain` |
| Amsterdam | `Amsterdam Airport Schiphol` | `Netherlands` |
| Milan | `Milan Malpensa Airport` | `Italy` |
| Munich | `Munich Airport` | `Germany` |
| Warsaw | `Warsaw Chopin Airport` | `Poland` |

## `long_term_normal_max_c` — every city is a placeholder; confidence tiers

`StationConfig.long_term_normal_max_c` has no default either, so Task 8 needs *a* number for each city to
compile at all, the same way the existing registry carries five "PLACEHOLDER KEPT ON PURPOSE" Asian entries
rather than leaving the field blank. None of the seven numbers below is a properly sourced, station-specific
1991-2020 normal — treat all seven as open items, but at clearly different confidence levels:

| City | Value | Tier | What it actually is |
|---|---|---|---|
| Madrid | 32.8°C | **Medium** — real institutional figure, wrong period | AEMET's own "Madrid Aeropuerto" (Barajas) climatological normals page, August mean maximum, but for reference period **1981-2010**, not the 1991-2020 period used elsewhere in this registry (https://www.aemet.es/en/serviciosclimaticos/datosclimatologicos/valoresclimatologicos?l=3129&k=mad) |
| London | 23.4°C | **Low** — real institutional figure, wrong station | Met Office 1991-2020 August mean maximum for **Heathrow**, not London City Airport — a different site (inland vs. riverside), direction of the bias not established here |
| Warsaw | ~24.7°C | **Low** — secondary aggregator claims 1991-2020 | Not sourced to IMGW (Poland's national met service) directly in this pass; a third-party aggregator states this period but the citation chain wasn't verified |
| Munich | ~24°C | **Low** — secondary aggregator, period unstated | DWD's own Munich Airport climate page was reached but did not yield the August figure in this pass (only confirmed it uses an even older 1981-2010 reference period generally); the 24°C figure is from an unverified secondary source |
| Milan | not recorded | **Unconfirmed — no number** | Sources found disagree by several degrees (city-center ~29-30°C vs. Malpensa-airport aggregator ~26-29°C) with no official ARPA Lombardia figure located; recording either would be picking one arbitrarily |
| Amsterdam | not recorded | **Unconfirmed — no number** | KNMI's own site was reached twice (publication abstract, klimaat-viewer menu) without surfacing a number; a third-party aggregator page 403'd |
| Paris | not recorded | **Unconfirmed — no number** | Météo-France's normals publication was located but the Le Bourget August figure was not extracted from it in this pass |

**Recommendation, not a fact:** for Milan, Amsterdam, and Paris, do not fabricate a number to satisfy the
dataclass constructor. Either spend a follow-up pass pulling the real ARPA Lombardia / KNMI / Météo-France
figure, or — if Task 8 needs these three registrable immediately — pick an explicitly-labeled nominal
placeholder (e.g. a round number in the plausible August range implied by the live bucket window itself,
which IS confirmed: Milan 20-30, Amsterdam 20-30, Paris 22-32) and mark it in the same
`# PLACEHOLDER KEPT ON PURPOSE` style as the existing entries, so it reads as deliberate rather than
silently wrong. That choice belongs to whoever writes the entry, not to this research document.

## London

- `polymarket_city_slug`: `london` — **confirmed**, slug `highest-temperature-in-london-on-august-25-2026` resolved live. (https://polym.trade/event/highest-temperature-in-london-on-august-25-2026)
- Settlement station: "London City Airport Station", resolution source NOAA `https://www.weather.gov/wrh/timeseries?site=eglc` — **confirmed** (polym.trade, quoted verbatim above)
- `icao`: `EGLC` — **confirmed** two ways: (1) the `site=eglc` parameter in NOAA's own resolution URL, (2) Wikipedia's London City Airport infobox (https://en.wikipedia.org/wiki/London_City_Airport). NOT Heathrow (EGLL) — matches the brief's warning exactly.
- `lat` / `lon`: `51.50528`, `0.05528` — **confirmed** (Wikipedia infobox, decimal form of 51°30′19″N 000°03′19″E)
- Live bucket window: `17` (or below) to `27` (or higher) — **confirmed** (polym.trade market list)
- Bucket count: **11** — **confirmed**, passes `EXPECTED_BUCKET_COUNT`
- `wunderground_slug`: `gb/london/EGLC` — **confirmed**, page loads and its own header reads "London City Airport Station" (https://www.wunderground.com/history/daily/gb/london/EGLC), matching the NOAA-named station exactly
- `official_client_key`: **London is NOT in the WWIS city list.** Checked directly against `https://worldweather.wmo.int/en/json/full_city_list.txt` twice, targeted at the "United Kingdom of Great Britain and Northern Ireland" country block and specifically for any City field starting with "London" — confirmed absent both times (the UK block runs ...Liverpool, Manchester... with nothing between). This is the documented Taipei-style exception the brief called out as possible. **Recommendation** (not a hard block): register with `official_client_key="wwis"` and `wwis_city_name=""`, exactly like RCSS/Taipei — the client already handles this honestly (returns `None` rather than guessing). Note `tests/test_station_registry.py::test_wwis_stations_have_city_name_except_taipei` currently hardcodes the exception to RCSS only and will need updating to also except London — that's a code change, out of scope for this research task, but Task 8 (or whoever edits the test) needs to know about it.
- `long_term_normal_max_c`: **placeholder, unverified.** No EGLC-specific 1991-2020 Met Office normal was found; the only concrete figure surfaced was Heathrow's 1991-2020 August mean max, 23.4°C — a different station, so not usable as EGLC's own normal. Do not use 23.4 as EGLC's value without a real EGLC-specific source.
- `iana_timezone` / `utc_offset_hours`: `Europe/London` / `0` — given, not independently re-derived.
- METAR: live coverage confirmed on `aviationweather.gov/api/data/metar?ids=EGLC` (whole-degree C readings present). Recommend `metar_ingest_mode="resolution"` on the evidence above; genuinely unverified point is the byte-exact NOAA-vs-METAR agreement.

## Paris

- `polymarket_city_slug`: `paris` — **confirmed**, live event on the 25th.
- Settlement station: "Paris-Le Bourget Airport Station", NOAA `site=lfpb` — **confirmed**.
- `icao`: `LFPB` — **confirmed** (NOAA URL parameter + Wikipedia infobox for Paris–Le Bourget Airport).
- `lat` / `lon`: `48.96000`, `2.43500` — **confirmed** (Wikipedia infobox, 48°57′36″N 02°26′06″E).
- Live bucket window: `22` (or below) to `32` (or higher) — **confirmed**.
- Bucket count: **11** — **confirmed**, passes.
- `wunderground_slug`: `fr/paris/LFPB` — **confirmed**, header reads "Paris-Le Bourget Airport Station", matching.
- `official_client_key`: `"wwis"`, `wwis_city_name="Paris"` — **confirmed** directly from the WWIS city list (`"France";"Paris";"194"`).
- `long_term_normal_max_c`: **placeholder, unverified.** Météo-France's 1991-2020 normals publication was located but the specific Le Bourget August daily-max figure was not extracted from it in this pass — do not invent a number.
- `iana_timezone` / `utc_offset_hours`: `Europe/Paris` / `1` — given.
- METAR: live coverage confirmed on `aviationweather.gov` for LFPB. Same recommendation/caveat as London.

## Madrid

- `polymarket_city_slug`: `madrid` — **confirmed**.
- Settlement station: "Adolfo Suárez Madrid-Barajas Airport Station", NOAA `site=lemd` — **confirmed**.
- `icao`: `LEMD` — **confirmed** (NOAA URL + Wikipedia infobox for Adolfo Suárez Madrid–Barajas Airport).
- `lat` / `lon`: `40.47222`, `-3.56083` — **confirmed** (Wikipedia infobox, 40°28′20″N 003°33′39″W).
- Live bucket window: `24` (or below) to `34` (or higher) — **confirmed**.
- Bucket count: **11** — **confirmed**, passes.
- `wunderground_slug`: `es/madrid/LEMD` — **confirmed**, header reads "Adolfo Suárez Madrid–Barajas Airport Station".
- `official_client_key`: `"wwis"`, `wwis_city_name="Madrid"` — **confirmed** (`"Spain";"Madrid";"195"`).
- `long_term_normal_max_c`: **placeholder, unverified.** AEMET's "Valores climatológicos normales: Madrid Aeropuerto" page exists (`aemet.es/en/serviciosclimaticos/datosclimatologicos/valoresclimatologicos?l=3129&k=mad`) but the August figure was not extracted from it in this pass.
- `iana_timezone` / `utc_offset_hours`: `Europe/Madrid` / `1` — given.
- METAR: live coverage confirmed on `aviationweather.gov` for LEMD. Same recommendation/caveat.

## Amsterdam

- `polymarket_city_slug`: `amsterdam` — **confirmed**.
- Settlement station: "Amsterdam Airport Schiphol Station", NOAA `site=eham` — **confirmed**.
- `icao`: `EHAM` — **confirmed** (NOAA URL + Wikipedia infobox for Amsterdam Airport Schiphol).
- `lat` / `lon`: `52.30000`, `4.76500` — **confirmed** (Wikipedia infobox, 52°18′00″N 4°45′54″E).
- Live bucket window: `20` (or below) to `30` (or higher) — **confirmed**.
- Bucket count: **11** — **confirmed**, passes.
- `wunderground_slug`: `nl/amsterdam/EHAM` — **confirmed**, header reads "Amsterdam Airport Schiphol Station".
- `official_client_key`: `"wwis"`, `wwis_city_name="Amsterdam (Schiphol)"` — **confirmed**, note the exact parenthetical is part of the match string (`"Netherlands (Kingdom of the)";"Amsterdam (Schiphol)";"143"`). The lookup in `wwis.py` is `.lower()`-only, no other normalization, so the config entry must use this exact string including the `(Schiphol)` suffix — a plain `"Amsterdam"` would NOT match.
- `long_term_normal_max_c`: **placeholder, unverified.** A KNMI climate page for Schiphol exists (`knmi.nl/kennis-en-datacentrum/publicatie/klimaat-voor-amsterdam-airport-schiphol`) but the specific 1991-2020 August daily-max figure was not extracted in this pass; the ~22-23°C "average afternoon" figure surfaced by search is not clearly the same statistic and should not be used as-is.
- `iana_timezone` / `utc_offset_hours`: `Europe/Amsterdam` / `1` — given.
- METAR: live coverage confirmed on `aviationweather.gov` for EHAM. Same recommendation/caveat.

## Milan

- `polymarket_city_slug`: `milan` — **confirmed**.
- Settlement station: "Malpensa Intl Airport Station", NOAA `site=limc` — **confirmed**.
- `icao`: `LIMC` — **confirmed** (NOAA URL + Wikipedia infobox for Milan Malpensa Airport). Note this is Malpensa, not Linate (LIML) — the brief's "not the busiest airport" warning doesn't bite here since NOAA/Wunderground both independently point at Malpensa, but it's worth recording explicitly that Milan has two major airports and the market uses neither the city center nor Linate.
- `lat` / `lon`: `45.63000`, `8.72306` — **confirmed** (Wikipedia infobox, 45°37′48″N 8°43′23″E).
- Live bucket window: `20` (or below) to `30` (or higher) — **confirmed**.
- Bucket count: **11** — **confirmed**, passes.
- `wunderground_slug`: `it/milan/LIMC` — **confirmed**, header reads "Malpensa Intl Airport Station".
- `official_client_key`: `"wwis"`, `wwis_city_name="Milan (MILANO)"` — **confirmed**, note the exact string including the all-caps parenthetical (`"Italy";"Milan (MILANO)";"603"`). Like Amsterdam, this exact string (capitalization aside, since the lookup lowercases) including `(MILANO)` must be used.
- `long_term_normal_max_c`: **placeholder, unverified.** Sources disagree in a way that makes any single number unsafe to record as "sourced": general Milan-city figures (~29-30°C early August, tapering) vs. Malpensa-airport-specific WeatherSpark aggregation (~26-29°C) diverge by a few degrees, and none of these is a cited 1991-2020 official normal.
- `iana_timezone` / `utc_offset_hours`: `Europe/Rome` / `1` — given.
- METAR: live coverage confirmed on `aviationweather.gov` for LIMC. Same recommendation/caveat.

## Munich

- `polymarket_city_slug`: `munich` — **confirmed**.
- Settlement station: "Munich Airport Station", NOAA `site=eddm` — **confirmed**.
- `icao`: `EDDM` — **confirmed** (NOAA URL + Wikipedia infobox for Munich Airport).
- `lat` / `lon`: `48.35389`, `11.78611` — **confirmed** (Wikipedia infobox, 48°21′14″N 011°47′10″E).
- Live bucket window: `13` (or below) to `23` (or higher) — **confirmed**. Notably the coolest window of the seven, consistent with the live odds concentrating around 16-18°C on 2026-08-25 — worth a sanity glance when this station starts trading (a 13-23 window is a much lower band than the other six, all clustered in the low-to-mid 20s/30s).
- Bucket count: **11** — **confirmed**, passes.
- `wunderground_slug`: `de/munich/EDDM` — **confirmed**, header reads "Munich Airport Station".
- `official_client_key`: `"wwis"`, `wwis_city_name="Munich"` — **confirmed** (`"Germany";"Munich";"58"`).
- `long_term_normal_max_c`: **placeholder, unverified.** DWD's own climate page for "München (Flugh.)" [Munich Airport] exists (`dwd.de/DE/wetter/wetterundklima_vorort/bayern/muenchen/_node.html`) but explicitly uses a 1981-2010 reference period per the search snippet, not the 1991-2020 period used elsewhere in this registry, and the August figure wasn't extracted in this pass regardless.
- `iana_timezone` / `utc_offset_hours`: `Europe/Berlin` / `1` — given.
- METAR: live coverage confirmed on `aviationweather.gov` for EDDM. Same recommendation/caveat.

## Warsaw

- `polymarket_city_slug`: `warsaw` — **confirmed**.
- Settlement station: "Warsaw Chopin Airport Station" (confirmed by a second, targeted fetch after the first pull truncated at 125 chars), NOAA `site=epwa` — **confirmed**.
- `icao`: `EPWA` — **confirmed** (NOAA URL + Wikipedia infobox for Warsaw Chopin Airport).
- `lat` / `lon`: `52.16583`, `20.96722` — **confirmed** (Wikipedia infobox, 52°09′57″N 20°58′02″E).
- Live bucket window: `17` (or below) to `27` (or higher) — **confirmed**. (Note: polym.trade returned Warsaw's 11 buckets in non-numeric order in the raw fetch; re-sorted here — 17-or-below, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27-or-higher — still exactly 11, still contiguous.)
- Bucket count: **11** — **confirmed**, passes.
- `wunderground_slug`: `pl/warsaw/EPWA` — **confirmed**, header reads "Warsaw Chopin Airport Station".
- `official_client_key`: `"wwis"`, `wwis_city_name="Warsaw"` — **confirmed** (`"Poland";"Warsaw";"24"`).
- `long_term_normal_max_c`: **placeholder, unverified.** A ~24.7°C August max figure surfaced from a secondary aggregator claiming to use the "1991-2020" period, but it is not sourced to IMGW (Poland's national met service) directly — treat as placeholder, not sourced.
- `iana_timezone` / `utc_offset_hours`: `Europe/Warsaw` / `1` — given.
- METAR: live coverage confirmed on `aviationweather.gov` for EPWA. Same recommendation/caveat.

---

## Additional registry invariant not in the brief's checklist, but relevant

`tests/test_station_registry.py::test_utc_offset_hours_in_registered_timezones` currently asserts
`st.utc_offset_hours in (5, 8, 9)` for every registered station — an Asia-only invariant. Every European
city in this document has `utc_offset_hours` 0 or 1, so **registering any of them will fail this test
as written**, independent of anything else in this document. This is a code change (widening the allowed
tuple, presumably to include 0 and 1), not a fact-gathering gap, so it's out of scope for Task 7 itself —
but Task 8 (or whoever edits `config.py`/the test) needs to know this test will need to change or every
European `StationConfig` addition will fail collection immediately. Same applies to
`test_thirteen_stations_registered`'s hardcoded `EXPECTED_STATION_COUNT = 13`.

---

## Summary

### Registrable now (all registry-blocking invariants satisfied)

All seven cities pass every invariant Task 7 was asked to check: 11-bucket span, unique
`polymarket_city_slug` and `wunderground_slug`, and `official_client_key="wwis"` already exists in
`_CLIENTS`.

1. **London** (EGLC) — with the caveat that `wwis_city_name` must be `""` (London is absent from WWIS;
   confirmed by direct, targeted search of the full list). No official forecast source for this station,
   same tradeoff already accepted for RCSS/Taipei.
2. **Paris** (LFPB) — `wwis_city_name="Paris"`.
3. **Madrid** (LEMD) — `wwis_city_name="Madrid"`.
4. **Amsterdam** (EHAM) — `wwis_city_name="Amsterdam (Schiphol)"` (exact string, parenthetical required).
5. **Milan** (LIMC) — `wwis_city_name="Milan (MILANO)"` (exact string, parenthetical required).
6. **Munich** (EDDM) — `wwis_city_name="Munich"`.
7. **Warsaw** (EPWA) — `wwis_city_name="Warsaw"`.

None of the seven are blocked on the invariants Task 7 checks. `display_name` and `country` are trivial
derivations already spelled out above. What's genuinely unresolved for every one of them, and must not be
silently filled in by Task 8, is:

- `long_term_normal_max_c` — no station's value is sourced to an official 1991-2020 normal in this
  pass (see the confidence-tier table above). Madrid/London/Warsaw/Munich at least have a real number
  from somewhere (wrong period or wrong site, clearly caveated); **Milan, Amsterdam, and Paris have no
  number at all** from any source reached in this pass — those three need either a follow-up sourcing
  pass or a knowingly-nominal placeholder (see recommendation above) before Task 8 can construct a
  `StationConfig` for them, since the field has no default.
- `resolution_grade_source` / `metar_ingest_mode` — strong circumstantial evidence (matching station name
  across NOAA/Wunderground, live METAR coverage on the exact API `metar_client.py` uses, matching
  whole-degree-C precision) supports treating all seven the same as the Asian default
  (`metar_daily_max` / `"resolution"`), but this was not verified to the same standard as VHHH's
  measured HKO-vs-airport offset (no side-by-side numeric check on a settled day was done). This is a
  design/engineering call, not a fact I can hand Task 8 as confirmed — flagging it explicitly rather than
  picking a value.

### Blocked

**None of the seven cities are blocked** on the invariants this task was scoped to check (bucket count,
slug uniqueness, official-client existence). The two open items above (climatological normal, and the
resolution-source-string design question) are gaps to carry forward, not registration blockers — the same
category of "known gap, not a crash" that RCSS's empty `wwis_city_name` and OPKC's `metar_ingest_mode="proxy"`
already represent in the existing registry.
