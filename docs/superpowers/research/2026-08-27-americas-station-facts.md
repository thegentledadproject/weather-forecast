# Americas station facts — Task 14a research (2026-08-28, re-dispatch)

## Methodology note (read first)

This is a **re-dispatch**. A first attempt at this exact task died to a session limit before writing
anything to disk; per `progress.md`'s "Task 14a" entry, the only finding recovered from its partial
output was that all four cities' METARs appear to report whole degrees Celsius only, no `T`-group. This
document verifies that claim independently rather than assuming it, and is written incrementally —
each city's section is appended to disk as soon as that city is done, specifically so a second cutoff
loses at most one city, not all four.

**No `task-14-brief.md` exists in this worktree's `.superpowers/sdd/2026-08-27-americas-market-isolation/`
directory** (the briefs run 1 through 13). Per that directory's `progress.md`, Task 14 was split into a
research pass (14a, this document) and a registry pass (14b) that hasn't been written yet — so the field
list and conventions below come from the plan doc
(`docs/superpowers/plans/2026-08-27-americas-market-isolation.md`), from `models.py`'s `StationConfig`
dataclass directly, and from the Europe precedent doc
(`docs/superpowers/research/2026-08-24-europe-station-facts.md`), not from a Task-14-specific brief file.

**Environment facts carried over from the Europe pass, not re-verified here:** `gamma-api.polymarket.com`
and `polymarket.com` are network-blocked (confirmed three independent ways in that pass). `polym.trade`
(third-party Gamma mirror: title, rules text, bucket list) and `polydata.pro/weather` (active-market /
unit listing) are reachable via WebFetch. Wikipedia, wunderground.com, worldweather.wmo.int, and
aviationweather.gov are reachable. `aviationweather.gov/api/data/metar` and `worldweather.wmo.int` are
fetched with `curl` (free, no WebFetch budget spent); WebFetch is reserved for polym.trade, polydata.pro,
and Wikipedia.

**Fields being established per city**, per `models.py` `StationConfig` and the Europe precedent's own
compiled list: `icao`, the station name the market's rules text itself names, `wunderground_slug`
(cross-checked against the Wunderground history page's own header), `lat`/`lon`, live METAR presence and
`T`-group presence, `official_client_key` (WWIS membership, checked directly against
`worldweather.wmo.int`; absent → `official_client_key="wwis"`, `wwis_city_name=""`, the documented
RCSS/London-style honest-gap convention), `resolution_grade_source` (what the rules text actually names),
`bucket_min_c`/`bucket_max_c` (the live window), `iana_timezone` and the STANDARD-time
`utc_offset_hours`, and a `long_term_normal_max_c` PLACEHOLDER (window midpoint, labelled with the month
it implicitly reflects — market windows are current, i.e. August/Southern-Hemisphere-winter for the
southern cities). `display_name` and `country` are trivial derivations recorded per Europe's precedent.
Two things are being verified, not assumed, per the dispatching agent's explicit instruction: (1) that
each city's market really is Celsius, 1-degree buckets — not Fahrenheit or 2-degree, which would
disqualify it from this task entirely — and (2) each city's DST/standard-offset status against the tz
database directly, not taken on the dispatching agent's assertion.

**Instruction-injection check:** flagged explicitly per city below; the running answer is recorded here
and updated only if something is found: **no content fetched in this pass (from any source) has
contained text addressed to me or attempting to direct my actions.** Everything returned has been
ordinary market/weather data, treated as data throughout.

---

## Toronto

- **Unit/step check (verified, not assumed):** polym.trade's market page (`highest-temperature-in-toronto-on-august-28-2026`)
  quotes: *"This market will resolve to the temperature range that contains the highest temperature
  recorded by NOAA at the Toronto Pearson Intl Airport Station in degrees Celsius on 28 Aug '26."* and
  *"The resolution source for this market measures temperatures to whole degrees Celsius (eg, 9°C)."*
  Bucket list (quoted verbatim, 11 rows): `20°C or below`, `21°C`, `22°C`, `23°C`, `24°C`, `25°C`,
  `26°C`, `27°C`, `28°C`, `29°C`, `30°C or higher` — **Celsius, 1-degree buckets, 11 buckets. Confirmed.**
- Settlement station: "Toronto Pearson Intl Airport Station"; NOAA resolution URL
  `https://www.weather.gov/wrh/timeseries?site=cyyz` (quoted above), which also names Weather
  Underground as a secondary source — same station-naming pattern as the Europe cohort.
- `icao`: `CYYZ` — **confirmed** two ways: (1) NOAA's `site=cyyz` URL parameter, (2) Wikipedia's Toronto
  Pearson International Airport infobox, which lists `ICAO Code: CYYZ` directly.
- `lat`/`lon`: `43.67611`, `-79.63056` — **confirmed** (Wikipedia infobox, 43°40′34″N 079°37′50″W;
  longitude negated for the West hemisphere).
- Live bucket window: `bucket_min_c=20`, `bucket_max_c=30` — **confirmed** (polym.trade bucket list
  above, 11 buckets, matches `EXPECTED_BUCKET_COUNT`).
- `wunderground_slug`: `ca/mississauga/CYYZ` — **confirmed**, `wunderground.com/history/daily/ca/mississauga/CYYZ`
  loads and its own header reads "Toronto Pearson Intl Airport Station" — same station name NOAA names.
- `official_client_key`: **Toronto IS in the WWIS city list**, but under a province-qualified name, not
  bare "Toronto." Direct fetch of `https://worldweather.wmo.int/en/json/full_city_list.txt` and grep for
  the Canada block returns the line `"Canada";"Toronto, Ontario";"264"` verbatim — no bare `"Toronto"`
  entry exists. Recommend `official_client_key="wwis"`, `wwis_city_name="Toronto, Ontario"` (exact
  string, comma and province required — same "exact string" constraint the Europe pass flagged for
  Amsterdam's `"(Schiphol)"` and Milan's `"(MILANO)"` suffixes, since `wwis.py`'s lookup only
  `.lower()`s, it does not strip qualifiers).
- METAR: **live, whole-degree C, no `T`-group** — confirmed directly via
  `curl https://aviationweather.gov/api/data/metar?ids=CYYZ&format=raw`, three consecutive reports e.g.
  `METAR CYYZ 281100Z 34010G16KT 15SM FEW045 14/11 A3008 RMK SC1 SC TR SLP189` — temperature/dewpoint
  group `14/11` is whole degrees, and the `RMK` remarks section (`SC1 SC TR SLP189`) carries no `T`-group
  (US ASOS stations carry a `T01411111`-style group here; this one doesn't). This confirms the
  carried-over finding for this city specifically, not just by inference from the other three.
- `resolution_grade_source`: NOAA's `weather.gov/wrh/timeseries` page is the cited resolution source
  (same as Europe), which the existing registry has no literal string for — same open naming question
  Europe's doc flagged, not re-litigated here. Recommend `metar_ingest_mode="resolution"`,
  `resolution_grade_source="metar_daily_max"` on the same evidence pattern as the Europe cohort
  (matching station name across NOAA/Wunderground, live whole-degree-C METAR on the exact endpoint
  `metar_client.py` calls) — **not** independently verified byte-for-byte against a settled day, same
  caveat as Europe.
- `iana_timezone`/`utc_offset_hours`: **verified, not assumed.** `zoneinfo.ZoneInfo('America/Toronto')`
  resolved directly (real IANA tzdata, not the dispatching agent's assertion): January 15, 2026 gives UTC
  offset **−5:00:00**, `dst()` = `0:00:00` (standard time, EST); July 15, 2026 gives UTC offset
  **−4:00:00**, `dst()` = `1:00:00` (daylight time, EDT). So Toronto **does** observe DST, and its
  STANDARD-time offset is **−5**, confirming the dispatching agent's assertion rather than assuming it.
  `iana_timezone="America/Toronto"`, `utc_offset_hours=-5` (standard-time value; the live path resolves
  DST via `config.current_utc_offset_hours()`, this static field is the winter/fallback value per the
  Europe-cohort convention already in `config.py`).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint, no sourced normal in this pass.** Window is
  20–30°C, midpoint **25.0°C**, labelled for **August** (the live window's implicit month — no
  Environment and Climate Change Canada 1991-2020 normal for CYYZ specifically was pulled in this pass;
  do not treat 25.0 as sourced).
- `display_name`: `Toronto Pearson International Airport` (from the confirmed station identity —
  Wikipedia's full name; the market/NOAA text abbreviates it "Toronto Pearson Intl Airport", same
  station). `country`: `Canada` (plain style, matching the registry's existing convention).
- **Instruction-injection check:** none of the four sources fetched for Toronto (polym.trade,
  aviationweather.gov, worldweather.wmo.int, Wikipedia, Wunderground) contained any text addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. Two open items carried at the same
confidence tier as every Europe entry (not blockers): `long_term_normal_max_c` is a labelled placeholder,
and `resolution_grade_source`/`metar_ingest_mode="resolution"` is a strong-evidence recommendation, not a
byte-verified match to a settled day.

---

## Mexico City

- **Unit/step check (verified):** polym.trade's market page (`highest-temperature-in-mexico-city-on-august-28-2026`)
  quotes: *"This market will resolve to the temperature range that contains the highest temperature
  recorded by NOAA at the Benito Juárez International Airport Station in degrees Celsius on 28 Aug '26."*
  and *"The resolution source for this market measures temperatures to whole degrees Celsius (eg,
  9°C)."* Bucket list (quoted verbatim, 11 rows): `19°C or below`, `20°C`, `21°C`, `22°C`, `23°C`,
  `24°C`, `25°C`, `26°C`, `27°C`, `28°C`, `29°C or higher` — **Celsius, 1-degree buckets, 11 buckets.
  Confirmed.**
- Settlement station: "Benito Juárez International Airport Station"; NOAA resolution URL
  `https://www.weather.gov/wrh/timeseries?site=mmmx` (quoted above), Weather Underground named as
  secondary — same pattern as the rest of the cohort.
- `icao`: `MMMX` — **confirmed** two ways: (1) NOAA's `site=mmmx` URL parameter, (2) Wikipedia's Mexico
  City International Airport infobox, `ICAO Code: "MMMX"`.
- `lat`/`lon`: `19.43611`, `-99.07194` — **confirmed** (Wikipedia infobox, 19°26′10″N 099°04′19″W).
- Live bucket window: `bucket_min_c=19`, `bucket_max_c=29` — **confirmed** (polym.trade bucket list
  above, 11 buckets).
- `wunderground_slug`: `mx/mexico-city/MMMX` — **confirmed**, `wunderground.com/history/daily/mx/mexico-city/MMMX`
  loads and its own header reads **"Aeropuerto Intl Lic. Benito Juárez Station"** — the Spanish-language
  name of the same airport NOAA's English resolution text names ("Benito Juárez International Airport").
  Same station, language difference only; noted explicitly rather than waved through, since a
  language-only mismatch is exactly the kind of thing that looks like a station mismatch at a glance.
- `official_client_key`: **"Mexico City" is not a literal WWIS entry; "Ciudad de Mexico" is.** Direct
  fetch of `worldweather.wmo.int/en/json/full_city_list.txt`, Mexico block, returns
  `"Mexico";"Ciudad de Mexico";"279"` — the Spanish name for the same city, no bare "Mexico City" line
  exists (checked the full 33-row Mexico block; the closest near-matches are Cuernavaca/Toluca, different
  cities). Recommend `official_client_key="wwis"`, `wwis_city_name="Ciudad de Mexico"` (exact string —
  same lowercase-only-lookup constraint noted for every WWIS entry so far).
- METAR: **live, whole-degree C, no `T`-group** — confirmed via
  `curl https://aviationweather.gov/api/data/metar?ids=MMMX&format=raw`, e.g.
  `METAR MMMX 281040Z 28005KT 7SM FEW020 SCT080 BKN220 15/11 A3035 NOSIG RMK 8/538 HZY` — temp/dewpoint
  `15/11` whole degrees, remarks (`8/538 HZY`, and on a third report `SLP110 57004 953 8/008 HZY ISOL
  AC`) carry no `T`-group.
- `resolution_grade_source`: same NOAA-`weather.gov/wrh/timeseries`-cites-the-METAR-station pattern as
  the rest of the cohort. Recommend `metar_ingest_mode="resolution"`,
  `resolution_grade_source="metar_daily_max"` on the same circumstantial-evidence basis as Toronto and
  the Europe cohort — not byte-verified against a settled day.
- `iana_timezone`/`utc_offset_hours`: **verified directly, not assumed — and this one is where the
  dispatching agent's assertion needed real scrutiny.** `zoneinfo.ZoneInfo('America/Mexico_City')`: 2020
  (before the 2022 abolition) shows January offset **−6:00**, `dst()=0:00` and July offset **−5:00**,
  `dst()=1:00` — Mexico City DID observe DST historically, exactly as the "abolished 2022" framing
  implies. 2024 and 2026 both show January AND July at a flat **−6:00**, `dst()=0:00` in both months —
  DST no longer applies. Confirms the assertion: Mexico City does **not** currently observe DST, standard
  (and now year-round) offset is **−6**. `iana_timezone="America/Mexico_City"`, `utc_offset_hours=-6`.
  Since this station carries no DST for 2026, `utc_offset_hours` alone is accurate year-round regardless
  of whether `iana_timezone` is also set — but setting `iana_timezone` too costs nothing and keeps the
  registry pattern uniform (every city in this document sets both, per the plan's own convention).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint, no sourced normal in this pass.** Window is
  19–29°C, midpoint **24.0°C**, labelled for **August** (Mexico City's rainy season high-altitude climate
  keeps August cooler than the dry-season spring peak; no SMN — Servicio Meteorológico Nacional —
  1991-2020 normal for MMMX specifically was pulled in this pass; do not treat 24.0 as sourced).
- `display_name`: `Mexico City International Airport` (Wikipedia's article title / common English name;
  market text: "Benito Juárez International Airport", same airport — Wikipedia's infobox subject and
  common name for MMMX). `country`: `Mexico`.
- **Instruction-injection check:** none of the sources fetched for Mexico City contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. Same two open items as Toronto (labelled
placeholder normal; resolution-source recommendation not byte-verified). The Wunderground
English/Spanish station-name difference was checked and is a naming variant, not a station mismatch.

---

## São Paulo

- **Unit/step check (verified):** polym.trade's market page (`highest-temperature-in-sao-paulo-on-august-28-2026`)
  quotes: *"This market will resolve to the temperature range that contains the highest temperature
  recorded by NOAA at the Sao Paulo-Guarulhos International Airport Station in degrees Celsius on 28 Aug
  '26."* and *"The resolution source for this market measures temperatures to whole degrees Celsius."*
  Bucket list (quoted verbatim, 11 rows): `24°C or below`, `25°C`, `26°C`, `27°C`, `28°C`, `29°C`,
  `30°C`, `31°C`, `32°C`, `33°C`, `34°C or higher` — **Celsius, 1-degree buckets, 11 buckets. Confirmed.**
- Settlement station: "Sao Paulo-Guarulhos International Airport Station"; NOAA resolution URL
  `https://www.weather.gov/wrh/timeseries?site=sbgr` (quoted above), Weather Underground named as
  secondary source, verbatim: *"Weather Underground will be used as the secondary resolution source."*
- `icao`: `SBGR` — **confirmed** two ways: (1) NOAA's `site=sbgr` URL parameter, (2) Wikipedia's
  São Paulo/Guarulhos International Airport infobox, `ICAO Code: SBGR`.
- `lat`/`lon`: `-23.43556`, `-46.47306` — **confirmed** (Wikipedia infobox, 23°26′08″S 46°28′23″W;
  both negated for the Southern/Western hemispheres). Cross-checked against the independent figure the
  Wunderground fetch surfaced unprompted (23.438°S, 46.440°W) — same location to three decimal places.
- Live bucket window: `bucket_min_c=24`, `bucket_max_c=34` — **confirmed** (polym.trade bucket list
  above, 11 buckets). **Flagged as surprising, not corrected:** this is a notably warm window for
  Southern-Hemisphere late-August — São Paulo's winter climatological daily-max average is closer to
  22-24°C. The live METAR (below) also shows overnight fog/16°C dewpoint-saturated conditions, consistent
  with a cool morning, so the 24-34 window implies the market is pricing a marked warm spell for later in
  the day, not a data error — but it is exactly the kind of anomaly worth a human glance before this
  station starts accumulating a normals baseline. Not treated as UNRESOLVED since the bucket list is
  independently confirmed live market data, not a guess.
- `wunderground_slug`: `br/guarulhos/SBGR` — **confirmed**, `wunderground.com/history/daily/br/guarulhos/SBGR`
  loads and identifies itself as "Guarulhos - Governador André Franco Montoro International Airport
  Station" — the airport's full formal name (Guarulhos–Governador André Franco Montoro International
  Airport is SBGR's official name; "São Paulo-Guarulhos" in NOAA's text is the common/market name for the
  same airport).
- `official_client_key`: **São Paulo IS in the WWIS city list under its bare English/Portuguese name.**
  Direct fetch of `worldweather.wmo.int/en/json/full_city_list.txt`, Brazil block, returns
  `"Brazil";"Sao Paulo";"1083"` verbatim — no qualifier needed, unlike Toronto/Mexico City. Recommend
  `official_client_key="wwis"`, `wwis_city_name="Sao Paulo"` (exact string, no diacritic — matches the
  WWIS list's own unaccented spelling).
- METAR: **live, whole-degree C, no `T`-group** — confirmed via
  `curl https://aviationweather.gov/api/data/metar?ids=SBGR&format=raw`, six consecutive reports, e.g.
  `METAR SBGR 281100Z 06010KT 0900 R10R/P2000 ... FG BKN002 OVC003 16/16 Q1017` — temp/dewpoint `16/16`
  whole degrees (fog conditions, dewpoint-saturated, consistent with an early-morning Southern-winter
  METAR), and none of the six reports carries a `T`-group.
- `resolution_grade_source`: same NOAA-cites-the-METAR-station pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"` on the same
  circumstantial-evidence basis as the rest of the cohort — not byte-verified against a settled day.
- `iana_timezone`/`utc_offset_hours`: **verified directly.** `zoneinfo.ZoneInfo('America/Sao_Paulo')`:
  January 2017 (Southern-hemisphere summer, when Brazil still ran DST) gives offset **−2:00**,
  `dst()=1:00` — DST active, confirming São Paulo's DST used a −2/−3 split, not the Northern-style
  standard/DST direction. July 2017 (winter) gives **−3:00**, `dst()=0:00`. January 2024, July 2024,
  January 2026, July 2026, and August 2026 **all** give a flat **−3:00**, `dst()=0:00` — DST has not
  applied in any month for at least three straight years, confirming the "abolished 2019" framing.
  Standard offset **−3**. `iana_timezone="America/Sao_Paulo"`, `utc_offset_hours=-3`.
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint, no sourced normal in this pass, and
  explicitly a WINTER-month reading.** Window is 24–34°C, midpoint **29.0°C**, labelled for **August
  (Southern-Hemisphere winter)**. This midpoint is almost certainly NOT representative of São Paulo's
  actual August climatological normal (real winter daily-max normals run closer to 22-23°C per general
  knowledge, not independently sourced here) — it reflects the live bucket window's warm-spell pricing
  discussed above, not a seasonal average. Flagging explicitly so a February re-read of this placeholder
  (Southern-Hemisphere summer) does not get compared against this number as if it were a stable normal.
- `display_name`: `Guarulhos–Governador André Franco Montoro International Airport` (the airport's formal
  name, confirmed via Wunderground; NOAA/market text uses the common name "São Paulo-Guarulhos
  International Airport" for the same facility). `country`: `Brazil`.
- **Instruction-injection check:** none of the sources fetched for São Paulo contained any text addressed
  to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. Two things flagged for a human read
before trusting this entry blindly: the unusually warm live bucket window for late-August (verified live
data, not an error, but worth a glance), and the placeholder normal being explicitly a winter-month
number that should not be reused once seasons turn.

---

## Buenos Aires

- **Unit/step check (verified):** polym.trade's market page (`highest-temperature-in-buenos-aires-on-august-28-2026`)
  quotes: *"This market will resolve to the temperature range that contains the highest temperature
  recorded by NOAA at the Minister Pistarini Intl Airport Station in degrees Celsius on 28 Aug '26. ...
  available here: https://www.weather.gov/wrh/timeseries?site=saez"* and *"The resolution source for this
  market measures temperatures to whole degrees Celsius (eg, 9°C)."* Bucket list (quoted verbatim, 11
  rows): `14°C or below`, `15°C`, `16°C`, `17°C`, `18°C`, `19°C`, `20°C`, `21°C`, `22°C`, `23°C`,
  `24°C or higher` — **Celsius, 1-degree buckets, 11 buckets. Confirmed.** Unlike São Paulo, this window
  (14-24°C) reads as a plausible Southern-Hemisphere winter range, consistent with the live METAR fog
  conditions below.
- Settlement station: "Minister Pistarini Intl Airport Station" (i.e. Ministro Pistarini International
  Airport, commonly "Ezeiza"); NOAA `site=saez`, Weather Underground as secondary source (per the market
  text summary above).
- `icao`: `SAEZ` — **confirmed** two ways: (1) NOAA's `site=saez` URL parameter, (2) Wikipedia's Ministro
  Pistarini International Airport infobox, `ICAO Code: SAEZ`.
- `lat`/`lon`: `-34.82222`, `-58.53583` — **confirmed** (Wikipedia infobox, 34°49′20″S 58°32′09″W).
- Live bucket window: `bucket_min_c=14`, `bucket_max_c=24` — **confirmed** (polym.trade bucket list
  above, 11 buckets).
- `wunderground_slug`: `ar/ezeiza/SAEZ` — **confirmed**, `wunderground.com/history/daily/ar/ezeiza/SAEZ`
  loads and its own header reads **"Minister Pistarini Intl Airport Station"** — an exact match, word for
  word, to the station name NOAA's own resolution text names. This is the tightest name-match of the four
  cities in this document (no language variant, no formal-vs-common-name gap).
- `official_client_key`: **Buenos Aires IS in the WWIS city list under its bare name.** Direct fetch of
  `worldweather.wmo.int/en/json/full_city_list.txt`, Argentina block, returns
  `"Argentina";"Buenos Aires";"294"` verbatim. Recommend `official_client_key="wwis"`,
  `wwis_city_name="Buenos Aires"` (exact string, no qualifier needed — same as São Paulo).
- METAR: **live, whole-degree C, no `T`-group** — confirmed via
  `curl https://aviationweather.gov/api/data/metar?ids=SAEZ&format=raw`, ten consecutive reports spanning
  ~5.5 hours, e.g. `METAR SAEZ 281100Z 16002KT 5000 MIFG BR NSC 08/08 Q1009 NOSIG` — temp/dewpoint `08/08`
  whole degrees (dense morning fog throughout the sampled window, textbook Southern-winter conditions),
  and none of the ten reports carries a `T`-group.
- `resolution_grade_source`: same NOAA-cites-the-METAR-station pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"` — same circumstantial
  basis as the rest of the cohort, not byte-verified against a settled day.
- `iana_timezone`/`utc_offset_hours`: **verified directly, including a genuine wrinkle worth reporting
  honestly rather than smoothing over.** `zoneinfo.ZoneInfo('America/Argentina/Buenos_Aires')`: every
  year checked from 2000 through 2026, January and July alike, resolves to a **flat −3:00 raw UTC
  offset** — the offset itself has not moved in this sample. However, `dst()` for January 2000
  specifically returns `1:00:00` (flagged as DST) even though the raw offset is identical to every
  non-DST period checked — this is IANA tzdata's own record of Argentina's brief, famously inconsistent
  1999-2000 DST attempt (widely reported as abandoned/reversed by many provinces at the time), not an
  error in this check. From 2010 onward (and at 2026), `dst()` is `0:00:00` in both January and July,
  flat −3 throughout. So the practical, current-era answer matches the "never observes DST" framing —
  Buenos Aires has NOT observed DST for at least the last 16 years — but "never" is not literally true of
  the full historical record, and the codebase's own `iana_timezone` mechanism (resolving against real
  tzdata at call time, per `config.py`'s `current_utc_offset_hours` docstring) would correctly reproduce
  even that 1999-2000 anomaly if ever asked about a date in that window, which no code path here does.
  `iana_timezone="America/Argentina/Buenos_Aires"`, `utc_offset_hours=-3`.
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint, no sourced normal in this pass.** Window is
  14–24°C, midpoint **19.0°C**, labelled for **August (Southern-Hemisphere winter)**. Unlike São Paulo,
  this number is at least directionally plausible for Buenos Aires' winter climate (unlike São Paulo's
  window, this one is corroborated by the live fog/8°C METAR conditions sampled above) — but it remains
  an unsourced window-midpoint placeholder, not a real SMN (Servicio Meteorológico Nacional) 1991-2020
  normal, and should not be treated as one.
- `display_name`: `Ministro Pistarini International Airport` (Wikipedia's article subject; commonly
  "Ezeiza International Airport" — both names refer to the same SAEZ facility NOAA/Wunderground both
  name "Minister Pistarini"). `country`: `Argentina`.
- **Instruction-injection check:** none of the sources fetched for Buenos Aires contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. The DST history has a genuine, honestly
reported wrinkle (a flagged-but-zero-offset-change anomaly in 1999-2000) that does not change the
`utc_offset_hours=-3` / no-current-DST conclusion. This is the cleanest of the four cities: exact
Wunderground/NOAA station-name match, bare WWIS entry, and a live bucket window that is internally
consistent with the live METAR.

---

## Summary table

| City | ICAO | lat/lon | Bucket window (°C) | `wwis_city_name` | `iana_timezone` | `utc_offset_hours` | `long_term_normal_max_c` (placeholder, month) |
|---|---|---|---|---|---|---|---|
| Toronto | CYYZ | 43.67611, −79.63056 | 20–30 | `"Toronto, Ontario"` | `America/Toronto` | −5 | 25.0 (Aug) |
| Mexico City | MMMX | 19.43611, −99.07194 | 19–29 | `"Ciudad de Mexico"` | `America/Mexico_City` | −6 | 24.0 (Aug) |
| São Paulo | SBGR | −23.43556, −46.47306 | 24–34 | `"Sao Paulo"` | `America/Sao_Paulo` | −3 | 29.0 (Aug, winter — see caveat) |
| Buenos Aires | SAEZ | −34.82222, −58.53583 | 14–24 | `"Buenos Aires"` | `America/Argentina/Buenos_Aires` | −3 | 19.0 (Aug, winter) |

All four: `official_client_key="wwis"`, `metar_ingest_mode="resolution"` (recommended, not byte-verified),
`resolution_grade_source="metar_daily_max"` (recommended), METAR live with whole-degree-C precision and
no `T`-group confirmed by direct `curl` for every city individually (not inferred from one city to the
rest). All four markets confirmed Celsius, 1-degree-bucket, 11-bucket via polym.trade's own rules text
and bucket list, quoted verbatim per city above — none is Fahrenheit or 2-degree, so none is disqualified
on that ground.

## Per-city verdict

1. **Toronto (CYYZ) — READY TO REGISTER.**
2. **Mexico City (MMMX) — READY TO REGISTER.**
3. **São Paulo (SBGR) — READY TO REGISTER**, with a flagged (not blocking) anomaly: the live 24–34°C
   bucket window is warm for Southern-Hemisphere late-August and the placeholder normal derived from it
   should not be reused as a stable seasonal figure once seasons turn.
4. **Buenos Aires (SAEZ) — READY TO REGISTER.** Cleanest of the four: exact station-name match across
   NOAA/Wunderground, bare WWIS entry, bucket window internally consistent with live METAR conditions.

**No city is BLOCKED.** Every load-bearing fact this pass was asked to establish — ICAO, station identity,
`wunderground_slug`, lat/lon, live METAR + precision, WWIS membership, resolution source, bucket window,
DST status, and standard-time UTC offset — was independently confirmed with a quoted source for all four
cities. The two items every entry still carries as an open placeholder, at the same confidence tier as
every entry in the Europe precedent document, are `long_term_normal_max_c` (window-midpoint placeholder,
no official 1991-2020 national-meteorological-service normal sourced for any of the four) and the
`resolution_grade_source`/`metar_ingest_mode="resolution"` recommendation (strong circumstantial evidence,
not a byte-for-byte verified match between NOAA's timeseries reading and the METAR-derived daily max on a
settled day) — neither is a registration blocker, both are carried forward exactly as Europe's did.

## What surprised me

- **São Paulo's live bucket window (24-34°C) reads warm for Southern-Hemisphere winter**, in contrast to
  Buenos Aires' 14-24°C window on the same date, which reads as textbook winter and is corroborated by
  live fog/8°C METAR conditions. Both are verified live market data, not inference, so neither was
  "corrected" — but the contrast is large enough (a full ~10°C between two cities at similar latitude
  bands, both in winter) that it is worth a human glance, and the São Paulo section says explicitly not
  to reuse its placeholder normal as a stable seasonal figure.
- **Buenos Aires' tz record carries a flagged DST anomaly in Jan 2000** (an IANA-tzdata-recorded, offset-
  unchanged DST flag from Argentina's brief, widely-reported-as-abandoned 1999-2000 DST attempt) even
  though the raw UTC offset never moved in that period. This did not change the −3/no-current-DST
  conclusion, but "Buenos Aires never observes DST" is not literally true of the full historical record —
  only of every year from roughly 2010 onward, which is what actually matters for this codebase.
- **WWIS listing conventions vary per city in ways that would silently break a naive `wwis_city_name`
  guess**: Toronto needs the province qualifier (`"Toronto, Ontario"`), Mexico City needs the Spanish name
  (`"Ciudad de Mexico"`), while São Paulo and Buenos Aires both use their plain English/Portuguese/Spanish
  names unqualified. This is the same class of gotcha the Europe pass found with Amsterdam's
  `"(Schiphol)"` and Milan's `"(MILANO)"` suffixes — worth flagging again since a Task-14b implementer
  might reasonably assume all four Americas cities follow one pattern.
- No instruction-injection attempt was found in any of the sources fetched across all four cities.

---

# Task 16 — Which NOAA product settles the US Fahrenheit markets

**Written incrementally.** Each finding was appended to disk as it was established, not held
in memory for a final write. Sections below appear in the order they were verified.

**Sources used:** `polym.trade` (third-party Polymarket mirror — `gamma-api.polymarket.com` and
`polymarket.com` remain network-blocked from this environment, not retried) via WebFetch;
`aviationweather.gov/api/data/metar` and `weather.gov` via `curl` from Bash.

## Step 1 — The rules text, verbatim

Market: `highest-temperature-in-nyc-on-august-25-2026`
(https://polym.trade/event/highest-temperature-in-nyc-on-august-25-2026), an **already-settled**
day (2026-08-25, three days past as of 2026-08-28).

The resolution description, quoted word for word:

> "This market will resolve to the temperature range that contains the highest temperature
> recorded by NOAA at the LaGuardia Airport Station in degrees Fahrenheit on 25 Aug '26. The
> resolution source for this market will be information from NOAA, specifically the highest
> reading under the "Temp" column for all times on this day, available here:
> https://www.weather.gov/wrh/timeseries?site=klga This market will resolve off of the Hourly
> Data provided using the "Show Hourly Data" button. To toggle between Fahrenheit and Celsius,
> click the "Switch to US Units w/ kts" button until the relevant table displays °F. This market
> can not resolve until the first data point for the following date has been published on the
> resolution source. The resolution source for this market measures temperatures to whole degrees
> Fahrenheit (eg, 21°F). Thus, this is the level of precision that will be used when resolving
> the market. Revisions to temperatures recorded within this market's timeframe will be
> considered until the first datapoint for the following date has been published, after which any
> alterations will not be considered."

Linked URL, verbatim: `https://www.weather.gov/wrh/timeseries?site=klga`

**Three things this text settles on its face:**

1. **Candidate (1) is named and candidates (2) and (3) are not.** The text names one product —
   *"the highest reading under the "Temp" column for all times on this day"* — at the
   `weather.gov/wrh/timeseries` page, and further narrows it to *"the Hourly Data provided using
   the "Show Hourly Data" button."* The ASOS 6-hour maximum group (`1xxxx` / `maxT`) is not on
   that page and is not mentioned. The CLI daily climate report is a different product at a
   different URL and is not mentioned. **The settlement input is max-over-hourly-observations,
   which is what `daily_max_temp_c` computes.**
2. **The settlement station is LaGuardia (KLGA), not Central Park (KNYC).** The market title says
   "New York City"; the rules say *"the LaGuardia Airport Station"* and link `site=klga`. KNYC
   (Central Park) is the NWS climate station for NYC and is the station the CLI report is written
   for — using it would be wrong. This is the same title-vs-station trap as Karachi
   ("Masroor Airbase" / OPKC) in the Asia registry.
3. **Precision is whole degrees Fahrenheit**, taken from the page's own °F rendering — not from a
   Celsius value the system rounds itself. See the rounding note in Step 2.

## Step 2 — All three candidates computed for KLGA, 2026-08-25

Station **KLGA** (the station the rules name), local day **2026-08-25** in EDT (UTC−4), i.e. the
UTC window `2026-08-25 04:00Z` → `2026-08-26 04:00Z`. Source: `curl
"https://aviationweather.gov/api/data/metar?ids=KLGA&format=json&hours=120"`, 127 rows returned,
24 of them inside this local day. Conversion `floor(c*9/5+32 + 0.5)` as specified.

**This day did NOT need a second attempt — it separates the candidates on its own.** It has
exactly the profile the brief asked for: a sharp late-afternoon peak reached between the :51
hourly observations.

The full observation series (all 24, hourly at :51):

| Local (EDT) | UTC | Temp °C | → °F | 6-hr max group |
|---|---|---|---|---|
| 00:51 | 0451Z | 22.2 | 72 | |
| 01:51 | 0551Z | 21.7 | 71 | `10256` → 25.6 °C |
| 02:51 | 0651Z | 21.1 | 70 | |
| 03:51 | 0751Z | 20.0 | 68 | |
| 04:51 | 0851Z | 20.0 | 68 | |
| 05:51 | 0951Z | 19.4 | 67 | |
| 06:51 | 1051Z | 19.4 | 67 | |
| 07:51 | 1151Z | 20.0 | 68 | `10217` → 21.7 °C |
| 08:51 | 1251Z | 20.6 | 69 | |
| 09:51 | 1351Z | 21.1 | 70 | |
| 10:51 | 1451Z | 22.2 | 72 | |
| 11:51 | 1551Z | 22.8 | 73 | |
| 12:51 | 1651Z | 23.9 | 75 | |
| 13:51 | 1751Z | 24.4 | 76 | `10244` → 24.4 °C |
| 14:51 | 1851Z | 25.0 | 77 | |
| 15:51 | 1951Z | 25.6 | 78 | |
| **16:51** | **2051Z** | **26.1** | **79** | |
| **17:51** | **2151Z** | **26.1** | **79** | |
| 18:51 | 2251Z | 25.6 | 78 | |
| 19:51 | 2351Z | 25.6 | 78 | **`10267` → 26.7 °C** |
| 20:51 | 0051Z | 25.0 | 77 | |
| 21:51 | 0151Z | 24.4 | 76 | |
| 22:51 | 0251Z | 23.9 | 75 | |
| 23:51 | 0351Z | 22.8 | 73 | |

The separating report, quoted raw:

> `METAR KLGA 252351Z 26009KT 10SM FEW050 SCT070 26/14 A2998 RMK AO2 SLP151 T02560144 10267 20244 53007`

`10267` is the 6-hour maximum group: 26.7 °C, covering 18Z–00Z — 14:00–20:00 EDT, entirely inside
the local day and exactly the afternoon peak window. The hourly observations never see it: the
highest hourly reading in that same six hours is 26.1 °C at 2051Z and 2151Z.

**The three candidates:**

| # | Product | Value | °F | Bucket implied |
|---|---|---|---|---|
| 1 | Max over hourly observations (`daily_max_temp_c`) | 26.1 °C | **79 °F** | **78–79 °F** |
| 2 | ASOS 6-hour max group / `maxT` | 26.7 °C | **80 °F** | **80–81 °F** |
| 3 | CLI daily climate report | (see below) | | |

Candidates (1) and (2) **disagree by 1 °F, and that 1 °F crosses a bucket boundary** — 79 is the
top of the 78–79 bucket, 80 is the bottom of the next one. This is precisely the failure mode the
brief was built to catch, and it is live on an ordinary summer day, not a contrived one.

## Step 3 — How the market actually resolved

Same market page (`highest-temperature-in-nyc-on-august-25-2026`), quoted:

> **Resolved:** "Will the highest temperature in New York City be between 78-79°F on August 25?"
> — **YES (100¢)**. All other outcomes resolved NO (0¢).

**Candidate (1) matches. Candidate (2) does not.** The 6-hour max group would have settled the
80–81 °F bucket, which resolved NO at 0¢. Max-over-hourly-observations — the thing
`daily_max_temp_c` computes — is what settled this market, on a day where the alternative gave a
different, losing answer.

Note also that the buckets here are **2 °F wide** (`78-79`, `80-81`), consistent with the
Fahrenheit-axis work already landed at `f3148e0`.

## Step 4 — `MIN_REPORTS_PER_DAY = 24` against real US volumes

`clients/metar_client.py:48` sets `MIN_REPORTS_PER_DAY = 24`, with the comment *"A tropical
airport files METARs at least half-hourly (~48/day)."* `daily_max_temp_c` applies it as
`if len(temps) < min_reports: return None` — so a day with exactly 24 reports passes, 23 does not.

**US ASOS airports file METARs HOURLY, not half-hourly: 24 scheduled reports per day.** The floor
was set for a ~48/day station and is being applied to a ~24/day one.

Measured: `hours=200` pulls for **KLGA, KORD, KLAX, KDEN, KMIA, KSEA**, bucketed into local days at
each station's own EDT/CDT/PDT/MDT offset, first and last (window-truncated) days dropped —
**46 complete station-days**:

| Reports in local day | 24 | 25 | 26 | 27 | 28 | 29 | 30 | 33 | 34 |
|---|---|---|---|---|---|---|---|---|---|
| station-days | **15** | 9 | 8 | 4 | 4 | 2 | 2 | 1 | 1 |

- **min 24, median 25, max 34. Zero days below 24.**
- **15 of 46 station-days (33%) landed on EXACTLY 24 — the bar itself, with zero margin.**
- The only headroom above 24 comes from unscheduled **SPECI** reports, which are weather-driven
  and therefore absent on exactly the calm, clear days a forecaster most wants.

**Verdict: MARGINAL, not routine.** 24 is cleared on every day observed, but a third of days clear
it by nothing at all. A single missed or temperature-less hourly METAR — an ordinary ASOS
occurrence — takes such a day to 23 and `daily_max_temp_c` returns `None`, silently. There is no
error, no log line: the day simply never becomes an observation.

That is the exact failure the brief names — `MIN_RESOLUTION_OBS_BEFORE_ENTRY` never advances and
the station never matures, with nothing in the logs to say why. **This is a real defect for the US
cohort even though the settlement-product question came back clean.** It should be fixed before or
alongside Task 17: the floor needs to be per-station (or region-aware), set from the station's own
scheduled reporting cadence, not from a tropical half-hourly assumption. A floor of ~20 for
hourly-reporting US stations preserves the "decline rather than under-report" intent (it still
rejects a genuinely gappy day) without discarding a third of good days on a coin flip.

**Note this is a coverage floor, not a correctness one:** on all 46 station-days the max over all
reports equalled the max over the :51 hourly reports, so SPECIs never lifted the computed max
above what the hourly table shows. Sample is small (46 days, 7 SPECIs carrying temperatures, none
of them the day's peak), so this is consistent-so-far, not proven. Worth a note in the registry:
the market reads only the hourly table, so a SPECI that did exceed every hourly reading would make
the system's max exceed the settlement value. See "Residual risks" below.

## Step 2 (continued) — Candidate (3), the CLI daily climate report

The CLI product IS reachable, via `api.weather.gov` (the `forecast.weather.gov/product.php` route
returns only the SPA shell — do not use it). LaGuardia has its own CLI location code, `LGA`,
distinct from Central Park's `NYC`:

```
curl -H "User-Agent: research" https://api.weather.gov/products/types/CLI/locations/LGA
curl -H "User-Agent: research" https://api.weather.gov/products/c1247299-1e46-4173-a30e-6bcd7726c3a4
```

The final CLI for 2026-08-25, `CLI KOKX`, issued `2026-08-26T06:35:00Z`, quoted verbatim:

```
CLILGA

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK, NY
235 AM EDT WED AUG 26 2026

...THE LAGUARDIA NY CLIMATE SUMMARY FOR AUGUST 25 2026...

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
                VALUE   (LST)  VALUE       VALUE  FROM      YEAR
...................................................................
TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         80    535 PM  96    1948  83     -3       86
  MINIMUM         67    623 AM  53    1940  70     -3       72
```

**CLI maximum: 80 °F, at 5:35 PM LST.** 5:35 PM falls between the 16:51 and 17:51 hourly
observations — the peak was reached between obs, exactly as the brief predicted, and the hourly
table never recorded it.

### The completed three-way comparison, KLGA 2026-08-25

| # | Product | Value | °F | Bucket implied | Market outcome |
|---|---|---|---|---|---|
| 1 | **Max over hourly obs** (`daily_max_temp_c`) | 26.1 °C | **79** | **78–79 °F** | **YES, 100¢** |
| 2 | ASOS 6-hour max group (`10267` / `maxT`) | 26.7 °C | 80 | 80–81 °F | NO, 0¢ |
| 3 | CLI daily report (`CLILGA`, 535 PM) | — | 80 | 80–81 °F | NO, 0¢ |

**One day separated all three.** (2) and (3) agree with each other at 80 °F — they are both
capturing the same 5:35 PM between-obs peak — and **both are wrong about how the market settled.**
Only candidate (1) matches.

## VERDICT

**Product (1) settles these markets: the hourly-observation `Temp` column at
`weather.gov/wrh/timeseries?site=<icao>`, maximum over the local day.**

`daily_max_temp_c` — max over the day's METAR observations — **IS the correct settlement input for
the US stations.** No redesign is required, and Task 17 is not blocked on this question.

The evidence is not merely the rules text (which alone would be strong: it names the hourly `Temp`
column and the "Show Hourly Data" button explicitly, and mentions neither the 6-hour group nor the
CLI report). It is that on a day where the three products give two different answers straddling a
bucket boundary, the market paid out on candidate (1) and paid zero on the bucket that (2) and (3)
both point at. The alternatives were tested and falsified, not merely unmentioned.

**This is deliberately a lucky result and should not be over-generalised.** It is one settled day
at one station. It is decisive because the products diverged and the market picked a side, not
because one day is a large sample. What it establishes firmly is the *direction*: the market does
NOT read the between-obs peak, so the standing worry that max-over-observations understates the
settlement value is **inverted** — the hourly max is the settlement value, by construction.

### Resolution re-verified against a second, stricter read

The first fetch's outcome summary was model-generated, so the same page was re-read with a prompt
that forbade inference and demanded verbatim rows. It returned the full ladder, confirming the
result rather than restating it. All 11 outcomes, exactly as listed:

| Outcome label | Price |
|---|---|
| ...be 67°F or below on August 25? | 0¢ |
| ...be between 68-69°F on August 25? | 0¢ |
| ...be between 70-71°F on August 25? | 0¢ |
| ...be between 72-73°F on August 25? | 0¢ |
| ...be between 74-75°F on August 25? | 0¢ |
| ...be between 76-77°F on August 25? | 0¢ |
| **...be between 78-79°F on August 25?** | **100¢** |
| ...be between 80-81°F on August 25? | **0¢** |
| ...be between 82-83°F on August 25? | 0¢ |
| ...be between 84-85°F on August 25? | 0¢ |
| ...be 86°F or higher on August 25? | 0¢ |

**11 buckets, 2 °F wide, catch-all at each end** — matches `EXPECTED_BUCKET_COUNT` and the
Fahrenheit-axis work at `f3148e0`. The bucket that candidates (2) and (3) point at, `80-81°F`, is
explicitly at **0¢**. The falsification is on the page, not inferred from it.

## Instruction-injection check

**Nothing fetched in this pass contained text addressed to me or attempting to direct my actions.**
Everything returned was ordinary market, rules, and weather data, and was treated as data
throughout. The stricter second read of the polym.trade page was asked directly whether any text
addressed the reader or attempted to direct an AI agent; the only reader-addressed string on the
page was the site's own generic UI error text, quoted verbatim:

> "The app didn't load properly.Reload"

That is a front-end failure message, not an instruction, and no action was taken on it.

## Consequences for Task 17 (registration)

**Task 17 MAY PROCEED on the settlement-source question.** Three things it must carry:

1. **`daily_max_temp_c` is the right input.** `metar_ingest_mode="resolution"` /
   `resolution_grade_source="metar_daily_max"` is correct for the US cohort, on the same footing as
   the Europe cohort but with strictly better evidence — Europe's was pattern-matching, this is a
   settled day where the alternatives were tested and paid zero.

2. **Register the AIRPORT station the rules name, never the city's climate station.** NYC's market
   settles on **KLGA (LaGuardia)**, not KNYC (Central Park) — the rules say *"the LaGuardia Airport
   Station"* and link `site=klga`. Both stations exist on `aviationweather.gov` and both return
   plausible data, so a wrong choice fails silently. On 2026-08-25 KNYC would also have been wrong
   in the °F it produced. **Each of the eleven cities' `icao` must be read out of its own rules
   text and its own `site=` URL parameter, not inferred from the city name.** This is the Karachi
   / "Masroor Airbase" trap again.

3. **`MIN_REPORTS_PER_DAY = 24` must be fixed before these stations can mature.** See Step 4: US
   ASOS files 24 scheduled reports/day against a floor of 24, and 33% of sampled station-days sit
   exactly on the bar. This does not affect *correctness* of settlement — it affects whether the
   station ever accumulates enough observations to arm at all, and it fails silently.

## Residual risks (not blockers, but record them)

- **Sample size.** One station, one settled day, for the settlement-product question. It is
  decisive because the products diverged and the market picked a side, but it is one day. The
  cheap confirmation before arming real money is to re-run this exact three-way comparison on the
  first settled day of any newly registered US station and check it still lands on candidate (1).
- **SPECI reports vs the hourly table.** The market reads only the hourly ("Show Hourly Data")
  table; `daily_max_temp_c` takes the max over every METAR the API returns, SPECIs included. On all
  46 sampled station-days the two were equal, but a SPECI exceeding every hourly reading would make
  the system's max exceed the settlement value — an over-statement, opposite in sign to the risk
  the brief was written about. Cheap to eliminate: filter to scheduled hourly reports, or at
  minimum log when a SPECI is the day's max.
- **The °C→°F rounding path.** The system holds Celsius and converts; the market reads whole °F off
  a page. US ASOS measures natively in °F and reports Celsius in the METAR `T`-group to tenths, so
  the round-trip is lossy in principle. On this day it was exact (26.1 °C → 78.98 → 79 °F, and 79 °F
  → 26.11 °C → 26.1). `floor(c*9/5+32 + 0.5)` reproduced every one of the 24 observations
  consistently. Worth one regression test rather than further research.
- **Revisions.** The rules allow revisions *"until the first datapoint for the following date has
  been published."* The system reads live METARs and would not see a later revision. Not exercised
  here; low frequency, but it means an ingested daily max is not strictly final until the next
  day's first observation.

---

# Task 17a — US Fahrenheit cities (2026-08-28)

**Methodology, carried from Task 16/14a, not re-litigated per city below:** `gamma-api.polymarket.com`
and `polymarket.com` remain network-blocked. `polym.trade` (rules text + bucket list) is fetched via
WebFetch, one call per city — each city's ICAO, unit, step, and bucket count are established from THAT
city's own rules text, never inferred from NYC or from a "main airport" assumption, per this task's
explicit requirement. `aviationweather.gov` (METAR) is fetched via `curl` from Bash, free of the WebFetch
budget. `worldweather.wmo.int/en/json/full_city_list.txt` (WWIS) and Wikipedia's raw wikitext
(`action=raw`) are also fetched via `curl`, reserving WebFetch entirely for polym.trade.

**Lower-edge bucket-key convention (per this task's explicit instruction):** a bucket key is the
bucket's lower edge in the market's own unit. For a window printed `"X°F or below … Y°F or higher"`
with interior step S, the top catch-all's implied ceiling is `Y`, and the bottom catch-all's key is
`derived from the printed bottom-interior label minus the step`, i.e. if the first interior bucket
printed is `"(X+S)-(X+2S-1)"` then the bottom catch-all key equals `X+S-S = X`. Concretely, for NYC's
already-quoted ladder (`67 or below`, `68-69`, ..., `86 or higher`): bottom catch-all key = 68 (the
low end of the first interior pair, i.e. `86 - 2*9 = 68`... shown per-city with real arithmetic below,
not asserted).


## New York City

- **Unit/step/ICAO check, from NYC's OWN rules text (live 2026-08-28 market, WebFetch of
  `polym.trade/event/highest-temperature-in-nyc-on-august-28-2026`):** quoted: *"This market will
  resolve to the temperature range that contains the highest temperature recorded by NOAA at the
  LaGuardia Airport Station in degrees Fahrenheit on 28 Aug '26."* Linked URL: `https://www.weather.gov/wrh/timeseries?site=klga`.
  **`icao=KLGA`, read from `site=klga` — not inferred from "New York City", and not KNYC (Central
  Park).** This reconfirms, on a second live day, the same finding Task 16's settled-day
  investigation (2026-08-25) already established for this station.
- **Bucket ladder, quoted verbatim (11 outcomes):** `71°F or below`, `72-73°F`, `74-75°F`,
  `76-77°F`, `78-79°F`, `80-81°F`, `82-83°F`, `84-85°F`, `86-87°F`, `88-89°F`, `90°F or higher`.
  **Fahrenheit, 2°F interior step, 11 outcomes — confirmed from this city's own text, not
  extrapolated.**
- **Lower-edge key arithmetic:** bottom catch-all printed top = 71, step = 2 →
  `71 + 1 - 2 = 70`. Top catch-all printed = 90, taken literally (a catch-all's own lower edge is
  its printed number) → `90`. Keys run 70, 72, 74, ..., 90 (11 keys). **`bucket_min_c=70`,
  `bucket_max_c=90`** (field name historical, unit is F per `bucket_unit`).
- Settlement station name (rules text): "LaGuardia Airport Station". `wunderground_slug`: confirmed
  by direct `curl` of `wunderground.com/history/daily/us/ny/east-elmhurst/KLGA` (HTTP 200, page
  title "Queens, NY Weather History", body text contains "LaGuardia Airport Station" verbatim —
  same station name the rules text uses).
- `lat`/`lon`: `40.775`, `-73.875` — confirmed via `curl` of Wikipedia's raw wikitext
  (`en.wikipedia.org/w/index.php?title=LaGuardia_Airport&action=raw`), infobox lines
  `| ICAO = KLGA` and `| coordinates = {{coord|40.775|N|73.875|W|...}}`.
- `official_client_key`: WWIS list (`worldweather.wmo.int/en/json/full_city_list.txt`, direct
  `curl`) contains `"United States of America";"New York City, New York";"278"` verbatim.
  Recommend `official_client_key="wwis"`, `wwis_city_name="New York City, New York"` (state-qualified
  exact string, same pattern as Toronto's `"Toronto, Ontario"`).
- METAR: **live, hourly, WHOLE-DEGREE precision reported alongside a `T`-group to tenths** — this is
  the one respect in which the US cohort differs structurally from every prior city in this
  document. `curl aviationweather.gov/api/data/metar?ids=KLGA&format=raw&hours=6` e.g.
  `METAR KLGA 281151Z 00000KT 10SM FEW032 BKN140 23/21 A2998 RMK AO2 SLP153 70018 T02280206 10233
  20222 53011 $` — main body `23/21` whole degrees, remarks carry `T02280206` (tenths: 22.8°C/20.6°C)
  **and** a 6-hour max/min pair `10233`/`20222`. Per Task 16's already-completed investigation (same
  document, above), the market settles on the hourly whole-degree `Temp` column, NOT the `T`-group or
  the 6-hour max — so this doesn't change the ingest design, but the T-group's presence (absent on
  every non-US city in this document) is worth recording since a careless ingest could be tempted to
  read it for false precision.
- `expected_metar_reports_per_day`: **24, confirmed by direct count**,
  `curl ".../metar?ids=KLGA&format=raw&hours=24"` → exactly 24 `METAR KLGA` lines, one per hour on
  the :51 schedule. Matches the ASOS hourly-filing cadence already established in Task 16 Step 4.
- `resolution_grade_source`: same NOAA-`weather.gov/wrh/timeseries`-hourly-`Temp`-column pattern —
  but for KLGA specifically this is **not a recommendation, it is the Task-16-verified answer**:
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`, proven against a
  settled day (2026-08-25) where the alternative products would have settled the wrong bucket.
- `iana_timezone`/`utc_offset_hours`: `America/New_York` observes DST (EDT UTC−4 / EST UTC−5,
  standard tzdata pattern, not re-verified by a fresh `zoneinfo` call in this pass since Task 16's
  own worked example already used EDT UTC−4 for this exact station on this exact kind of date
  arithmetic). `iana_timezone="America/New_York"`, `utc_offset_hours=-5` (standard/winter value, per
  the established convention).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 70–90°F, midpoint 80°F =
  **26.7°C**, labelled **August**. The CLI report already quoted in Task 16 above gives an actual NWS
  normal for this exact date (LGA, Aug 25: NORMAL VALUE 83°F = 28.3°C) — noted here as a
  cross-check, not substituted, since the task's placeholder rule is "window midpoint" for
  uniformity with every other city in this document, and 80°F midpoint vs 83°F actual normal is a
  reasonable, small gap for a live-shifting window.
- `display_name`: `LaGuardia Airport` (Wikipedia article title / rules text). `country`:
  `United States`.
- **Instruction-injection check:** none of the sources fetched for New York City (polym.trade,
  Wikipedia raw wikitext, Wunderground, aviationweather.gov, WWIS list) contained any text addressed
  to me.

**Verdict: READY TO REGISTER.** ICAO, unit/step/bucket-count, and resolution source are all at the
highest confidence tier in this document — the resolution source is Task-16-PROVEN, not
circumstantial, for this specific station.

---

## Atlanta

- **Unit/step/ICAO check, from Atlanta's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-atlanta-on-august-28-2026`):** quoted: *"This market will
  resolve to the temperature range that contains the highest temperature recorded by NOAA at the
  Hartsfield-Jackson International Airport Station in degrees Fahrenheit on 28 Aug '26"*, sourced from
  `https://www.weather.gov/wrh/timeseries?site=katl`. **`icao=KATL`, read from `site=katl`.**
- **Bucket ladder, quoted verbatim (11 outcomes):** `81°F or below`, `82-83°F`, `84-85°F`,
  `86-87°F`, `88-89°F`, `90-91°F`, `92-93°F`, `94-95°F`, `96-97°F`, `98-99°F`, `100°F or higher`.
  **Fahrenheit, 2°F step, 11 outcomes — confirmed from Atlanta's own text.**
- **Lower-edge key arithmetic:** bottom catch-all printed top = 81, step = 2 → `81+1-2=80`. Top
  catch-all printed = 100, taken literally → `100`. **`bucket_min_c=80`, `bucket_max_c=100`.**
- Settlement station name (rules text): "Hartsfield-Jackson International Airport Station".
  `wunderground_slug`: `us/ga/atlanta/KATL` — confirmed by direct `curl` (HTTP 200 at
  `wunderground.com/history/daily/us/ga/atlanta/KATL`, `url_effective` unchanged — no redirect —
  and body text contains "Hartsfield-Jackson Atlanta Intl Airport Station" verbatim, same airport
  the rules text names in its longer form).
- `lat`/`lon`: infobox `{{coord|33|38|12|N|84|25|41|W}}` → `33.63667`, `-84.42806` — confirmed via
  `curl` of Wikipedia raw wikitext (`Hartsfield–Jackson_Atlanta_International_Airport`, infobox
  `| ICAO = KATL`).
- `official_client_key`: WWIS list contains `"United States of America";"Atlanta, Georgia";"268"`
  verbatim (already grepped for all 11 cities in one pass, recorded at the top of this Task 17a
  section). Recommend `official_client_key="wwis"`, `wwis_city_name="Atlanta, Georgia"`.
- METAR: **live, hourly, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KATL&format=raw&hours=3`, e.g. `METAR KATL 281152Z 23003KT
  10SM FEW150 SCT250 23/21 A3004 RMK AO2 SLP162 70058 T02280211 10239 20228 53013` — `23/21` whole
  degrees, `T02280211` tenths group present (same US-ASOS pattern as KLGA).
- `expected_metar_reports_per_day`: **24, confirmed by direct count** — `hours=24` window returns
  exactly 24 `METAR KATL` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern as KLGA (Task-16-verified
  design, not independently re-proven per city — see NYC's entry and Task 16 above for the proof).
  Recommend `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: `America/New_York` (Atlanta is Eastern time, same zone as
  NYC). `iana_timezone="America/New_York"`, `utc_offset_hours=-5` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 80–100°F, midpoint 90°F =
  **32.2°C**, labelled **August**.
- `display_name`: `Hartsfield-Jackson Atlanta International Airport`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Atlanta contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact.

---

## Miami

- **Unit/step/ICAO check, from Miami's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-miami-on-august-28-2026`):** quoted (page rendered the
  catch-alls as ≤/≥ rather than "or below/or higher" text, noted as a rendering variant, not a
  different rule): *"will resolve to the temperature range that contains the highest temperature
  recorded by NOAA at the Miami Intl Airport Station"*, sourced from
  `https://www.weather.gov/wrh/timeseries?site=kmia`. **`icao=KMIA`, read from `site=kmia`.**
- **Bucket ladder, quoted verbatim (11 outcomes):** `≤81°F`, `82–83°F`, `84–85°F`, `86–87°F`,
  `88–89°F`, `90–91°F`, `92–93°F`, `94–95°F`, `96–97°F`, `98–99°F`, `≥100°F`. **Fahrenheit, 2°F
  step, 11 outcomes — confirmed from Miami's own text.**
- **Lower-edge key arithmetic:** bottom catch-all printed top = 81, step = 2 → `81+1-2=80`. Top
  catch-all printed = 100, literal → `100`. **`bucket_min_c=80`, `bucket_max_c=100`** (numerically
  identical to Atlanta's window on this date — coincidence of two hot cities on the same day, not an
  extrapolation; each was read from its own page).
- Settlement station name (rules text): "Miami Intl Airport Station". `wunderground_slug`:
  `us/fl/miami/KMIA` — confirmed by direct `curl` (HTTP 200, `url_effective` unchanged, body
  contains "Miami Intl Airport Station" verbatim — an EXACT match to the rules text, tightest of the
  US cohort so far).
- `lat`/`lon`: infobox `{{coord|25|47|36|N|080|17|26|W}}` → `25.79333`, `-80.29056` — confirmed via
  `curl` of Wikipedia raw wikitext (`Miami_International_Airport`, `| ICAO = KMIA`).
- `official_client_key`: WWIS list contains `"United States of America";"Miami, Florida";"267"`
  verbatim. Recommend `official_client_key="wwis"`, `wwis_city_name="Miami, Florida"`.
- METAR: **live, hourly scheduled + SPECI, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KMIA&format=raw&hours=3` shows both `METAR` (hourly, e.g.
  `METAR KMIA 281153Z ... 28/23 ... T02830228 10283 20267 53013`) and `SPECI` reports (thunderstorm
  activity triggering extra unscheduled reports, e.g. `SPECI KMIA 281047Z ...`) — consistent with
  Task 16's finding that SPECIs add volume without changing the settlement product.
- `expected_metar_reports_per_day`: **24, confirmed by direct count of scheduled `METAR` (not
  `SPECI`) lines** — `hours=24` window returns exactly 24 `METAR KMIA` lines; the SPECI reports are
  additional, not counted toward the scheduled cadence.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern as KLGA/KATL. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: `America/New_York` (Eastern time, same zone as NYC/Atlanta).
  `iana_timezone="America/New_York"`, `utc_offset_hours=-5` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 80–100°F, midpoint 90°F =
  **32.2°C**, labelled **August**.
- `display_name`: `Miami International Airport`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Miami contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. Exact Wunderground/rules-text
station-name match, same tier as Buenos Aires in the earlier cohort.

---

## Chicago

- **Unit/step/ICAO check, from Chicago's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-chicago-on-august-28-2026`):** quoted: *"This market
  will resolve to the temperature range that contains the highest temperature recorded by NOAA at
  the Chicago O'Hare Intl Airport Station in degrees Fahrenheit on 28 Aug '26."*, sourced from
  `https://www.weather.gov/wrh/timeseries?site=kord`. **`icao=KORD`, read from `site=kord`.**
- **Bucket ladder, quoted verbatim (11 outcomes, full question phrasing this fetch returned):**
  `75°F or below`, `76-77°F`, `78-79°F`, `80-81°F`, `82-83°F`, `84-85°F`, `86-87°F`, `88-89°F`,
  `90-91°F`, `92-93°F`, `94°F or higher`. **Fahrenheit, 2°F step, 11 outcomes.**
- **Lower-edge key arithmetic:** bottom catch-all printed top = 75, step = 2 → `75+1-2=74`. Top
  catch-all printed = 94, literal → `94`. **`bucket_min_c=74`, `bucket_max_c=94`.**
- Settlement station name (rules text): "Chicago O'Hare Intl Airport Station". `wunderground_slug`:
  `us/il/chicago/KORD` — confirmed by direct `curl` (HTTP 200, `url_effective` unchanged, body
  contains "O'Hare Intl Airport Station" — matches the rules text's station name minus the "Chicago"
  city prefix, same airport, not a mismatch).
- `lat`/`lon`: infobox `{{coord|41|58|43|N|87|54|17|W}}` → `41.97861`, `-87.90472` — confirmed via
  `curl` of Wikipedia raw wikitext (`O'Hare_International_Airport`, `| ICAO = KORD`).
- `official_client_key`: WWIS list contains `"United States of America";"Chicago, Illinois";"274"`
  verbatim. Recommend `official_client_key="wwis"`, `wwis_city_name="Chicago, Illinois"`.
- METAR: **live, hourly, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KORD&format=raw&hours=3`, e.g. `METAR KORD 281151Z 00000KT
  10SM SCT045 SCT080 19/17 A3013 RMK AO2 SLP201 T01940167 10217 20189 53017` — `19/17` whole
  degrees, `T01940167` tenths group present.
- `expected_metar_reports_per_day`: **24, confirmed by direct count** — `hours=24` window returns
  exactly 24 `METAR KORD` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: **Central time.** `iana_timezone="America/Chicago"`,
  `utc_offset_hours=-6` (standard/winter value; CDT is UTC−5 in summer via `current_utc_offset_hours`).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 74–94°F, midpoint 84°F =
  **28.9°C**, labelled **August**.
- `display_name`: `O'Hare International Airport`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Chicago contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact.

---

## Houston

- **Unit/step/ICAO check, from Houston's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-houston-on-august-28-2026`):** quoted: *"This market
  will resolve to the temperature range that contains the highest temperature recorded by NOAA at
  the William P. Hobby Airport Station in degrees Fahrenheit on 28 Aug '26."*, sourced from
  `https://www.weather.gov/wrh/timeseries?site=khou`. **`icao=KHOU`, read from `site=khou`.**
  **This is the inference trap the task brief named explicitly, materialised for real: Houston's
  larger/primary airport is George Bush Intercontinental (KIAH), and a "main airport" guess would
  have picked KIAH. The market settles on Hobby (KHOU) instead — a secondary, closer-in airport.
  Confirmed from the rules text's own URL, not inferred from city size or airport prominence.**
- **Bucket ladder, quoted verbatim (11 outcomes):** `81°F or below`, `82-83°F`, `84-85°F`,
  `86-87°F`, `88-89°F`, `90-91°F`, `92-93°F`, `94-95°F`, `96-97°F`, `98-99°F`, `100°F or higher`.
  **Fahrenheit, 2°F step, 11 outcomes.**
- **Lower-edge key arithmetic:** bottom catch-all printed top = 81, step = 2 → `81+1-2=80`. Top
  catch-all printed = 100, literal → `100`. **`bucket_min_c=80`, `bucket_max_c=100`.**
- Settlement station name (rules text): "William P. Hobby Airport Station". `wunderground_slug`:
  `us/tx/houston/KHOU` — confirmed by direct `curl` (HTTP 200, `url_effective` unchanged, body
  contains "Hobby Airport Station" — matches the rules text minus the "William P." given-name
  prefix, same airport).
- `lat`/`lon`: infobox `{{coord|29|38|44|N|95|16|44|W}}` → `29.64556`, `-95.27889` — confirmed via
  `curl` of Wikipedia raw wikitext (`William_P._Hobby_Airport`, `| ICAO = KHOU`).
- `official_client_key`: WWIS list contains `"United States of America";"Houston, Texas";"770"`
  verbatim. Recommend `official_client_key="wwis"`, `wwis_city_name="Houston, Texas"`.
- METAR: **live, hourly scheduled + SPECI (active thunderstorms at fetch time), whole-degree with
  `T`-group** — `curl aviationweather.gov/api/data/metar?ids=KHOU&format=raw&hours=3`, e.g.
  `METAR KHOU 281153Z 33006KT ... 27/24 A2999 RMK AO2 LTG DSNT W TSE44 SLP160 TS MOV SW ...
  T02670244 10283 20267 50005` — `27/24` whole degrees, `T02670244` tenths group present; a `SPECI`
  report is interleaved (thunderstorm-triggered), same additive pattern as Miami.
- `expected_metar_reports_per_day`: **24, confirmed by direct count of scheduled `METAR` (not
  `SPECI`) lines** — `hours=24` window returns exactly 24 `METAR KHOU` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: **Central time.** `iana_timezone="America/Chicago"`,
  `utc_offset_hours=-6` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 80–100°F, midpoint 90°F =
  **32.2°C**, labelled **August**.
- `display_name`: `William P. Hobby Airport`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Houston contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. **Flag for the registry pass:** do
not substitute KIAH (Bush Intercontinental) under any refactor — KHOU (Hobby) is the verified
settlement station, confirmed from Houston's own `site=` URL.

---

## Dallas

- **Unit/step/ICAO check, from Dallas's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-dallas-on-august-28-2026`):** quoted: *"This market will
  resolve to the temperature range that contains the highest temperature recorded by NOAA at the
  Dallas Love Field Station in degrees Fahrenheit on 28 Aug '26."*, sourced from
  `https://www.weather.gov/wrh/timeseries?site=kdal`. **`icao=KDAL`, read from `site=kdal`. A
  second confirmed instance of the airport-naming trap (after Houston): Dallas/Fort Worth
  International (KDFW) is the region's larger, more "obvious" airport, but this market settles on
  Dallas Love Field (KDAL) instead.**
- **Bucket ladder, quoted verbatim (11 outcomes):** `91°F or below`, `92-93°F`, `94-95°F`,
  `96-97°F`, `98-99°F`, `100-101°F`, `102-103°F`, `104-105°F`, `106-107°F`, `108-109°F`,
  `110°F or higher`. **Fahrenheit, 2°F step, 11 outcomes.** Notably hot window — highest of the
  eleven US cities checked so far, consistent with a Texas August heat event (verified live market
  data, not corrected).
- **Lower-edge key arithmetic:** bottom catch-all printed top = 91, step = 2 → `91+1-2=90`. Top
  catch-all printed = 110, literal → `110`. **`bucket_min_c=90`, `bucket_max_c=110`.**
- Settlement station name (rules text): "Dallas Love Field Station". `wunderground_slug`:
  `us/tx/dallas/KDAL` — confirmed by direct `curl` (HTTP 200, `url_effective` unchanged, body
  contains "Love Field Station" — matches the rules text's station identity, "Dallas" prefix
  dropped in the page header only).
- `lat`/`lon`: infobox `{{coord|32|50|50|N|096|51|06|W}}` → `32.84722`, `-96.85167` — confirmed via
  `curl` of Wikipedia raw wikitext (`Dallas_Love_Field`, `| ICAO = KDAL`).
- `official_client_key`: **WWIS's Dallas entry is qualified differently from the airport name** —
  the list (already grepped for all 11 cities) contains `"United States of America";"Dallas Ft
  Worth, Texas";"745"` verbatim, no bare `"Dallas, Texas"` line exists. This is a WWIS city-naming
  quirk independent of which airport the market itself settles on (KDAL/Love Field, confirmed
  above) — the WWIS city grouping and the settlement airport are simply two different things.
  Recommend `official_client_key="wwis"`, `wwis_city_name="Dallas Ft Worth, Texas"` (exact string).
- METAR: **live, hourly, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KDAL&format=raw&hours=3`, e.g. `METAR KDAL 281153Z 10003KT
  10SM FEW250 27/20 A3003 RMK AO2 SLP160 T02670200 10300 20267 53007` — `27/20` whole degrees,
  `T02670200` tenths group present.
- `expected_metar_reports_per_day`: **24, confirmed by direct count** — `hours=24` window returns
  exactly 24 `METAR KDAL` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: **Central time.** `iana_timezone="America/Chicago"`,
  `utc_offset_hours=-6` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 90–110°F, midpoint 100°F =
  **37.8°C**, labelled **August**. Flagged, like São Paulo's window in the earlier cohort, as an
  unusually warm-reading placeholder — reflects a live heat-event window, not a stable seasonal
  normal; should not be reused once the event passes.
- `display_name`: `Dallas Love Field`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Dallas contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. **Flag for the registry pass:** do
not substitute KDFW under any refactor — KDAL (Love Field) is the verified settlement station. Also
flag the WWIS city-name / settlement-airport naming mismatch (`"Dallas Ft Worth, Texas"` vs KDAL) so
a future reader doesn't "fix" `wwis_city_name` to `"Dallas, Texas"` — that string is not in the list.

---

## Austin

- **Unit/step/ICAO check, from Austin's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-austin-on-august-28-2026`):** quoted: *"This market will
  resolve to the temperature range that contains the highest temperature recorded by NOAA at the
  Austin-Bergstrom International Airport Station in degrees Fahrenheit on 28 Aug '26."*, sourced
  from `https://www.weather.gov/wrh/timeseries?site=kaus`. **`icao=KAUS`, read from `site=kaus`.**
  Austin has one primary commercial airport, so no naming trap here — still read from the city's
  own text rather than assumed.
- **Bucket ladder, quoted verbatim (11 outcomes):** `91°F or below`, `92-93°F`, `94-95°F`,
  `96-97°F`, `98-99°F`, `100-101°F`, `102-103°F`, `104-105°F`, `106-107°F`, `108-109°F`,
  `110°F or higher`. **Fahrenheit, 2°F step, 11 outcomes** — numerically the same window as
  Dallas on this date (both Texas, same heat event), each read independently from its own page.
- **Lower-edge key arithmetic:** bottom catch-all printed top = 91, step = 2 → `91+1-2=90`. Top
  catch-all printed = 110, literal → `110`. **`bucket_min_c=90`, `bucket_max_c=110`.**
- Settlement station name (rules text): "Austin-Bergstrom International Airport Station".
  `wunderground_slug`: `us/tx/austin/KAUS` — confirmed by direct `curl` (HTTP 200, `url_effective`
  unchanged, body contains "Austin Bergstrom Intl Airport Station" — same airport, abbreviated
  form).
- `lat`/`lon`: infobox `{{coord|30|11|40|N|97|40|12|W}}` → `30.19444`, `-97.67000` — confirmed via
  `curl` of Wikipedia raw wikitext (`Austin–Bergstrom_International_Airport`, `| ICAO = KAUS`).
- `official_client_key`: WWIS list contains `"United States of America";"Austin, Texas";"719"`
  verbatim (bare, no qualifier needed — unlike Dallas). Recommend `official_client_key="wwis"`,
  `wwis_city_name="Austin, Texas"`.
- METAR: **live, hourly, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KAUS&format=raw&hours=3`, e.g. `METAR KAUS 281153Z 00000KT
  10SM SCT110 BKN150 BKN200 25/22 A3002 RMK AO2 SLP154 70007 T02500222 10278 20250 53012` — `25/22`
  whole degrees, `T02500222` tenths group present.
- `expected_metar_reports_per_day`: **24, confirmed by direct count** — `hours=24` window returns
  exactly 24 `METAR KAUS` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: **Central time.** `iana_timezone="America/Chicago"`,
  `utc_offset_hours=-6` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 90–110°F, midpoint 100°F =
  **37.8°C**, labelled **August**. Same live-heat-event caveat as Dallas — do not treat as a stable
  seasonal normal.
- `display_name`: `Austin-Bergstrom International Airport`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Austin contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact.

---

## Denver

- **Unit/step/ICAO check, from Denver's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-denver-on-august-28-2026`):** quoted: *"This market will
  resolve to the temperature range that contains the highest temperature recorded by NOAA at the
  Buckley Space Force Base Station in degrees Fahrenheit on 28 Aug '26."*, sourced from
  `https://www.weather.gov/wrh/timeseries?site=kbkf`. **`icao=KBKF`, read from `site=kbkf`. This is
  the SHARPEST instance of the airport-naming trap in this whole cohort: Denver International
  Airport (KDEN) is the obvious "main airport" guess and is not even a military installation; this
  market instead settles on Buckley Space Force Base (KBKF), a different facility in a different
  city (Aurora, CO, not Denver proper — confirmed below via Wunderground's own city header). A
  "main airport" or "city name" inference would have failed completely here.**
- **Bucket ladder, quoted verbatim (11 outcomes):** `83°F or below`, `84–85°F`, `86–87°F`,
  `88–89°F`, `90–91°F`, `92–93°F`, `94–95°F`, `96–97°F`, `98–99°F`, `100–101°F`, `102°F or higher`.
  **Fahrenheit, 2°F step, 11 outcomes.**
- **Lower-edge key arithmetic:** bottom catch-all printed top = 83, step = 2 → `83+1-2=82`. Top
  catch-all printed = 102, literal → `102`. **`bucket_min_c=82`, `bucket_max_c=102`.**
- Settlement station name (rules text): "Buckley Space Force Base Station". `wunderground_slug`:
  `us/co/aurora/KBKF` — confirmed by direct `curl` (HTTP 200, body contains "Buckley Space Force
  Base Station" verbatim — an exact match to the rules text — and the page's own `<title>` reads
  "Aurora, CO Weather History", confirming the station is physically in Aurora, not Denver).
- `lat`/`lon`: infobox `{{coord|39|42|06|N|104|45|06|W|name=Buckley SFB}}` → `39.70167`,
  `-104.75167` — confirmed via `curl` of Wikipedia raw wikitext (`Buckley_Space_Force_Base`,
  `| ICAO = KBKF`).
- `official_client_key`: WWIS list contains `"United States of America";"Denver, Colorado";"271"`
  verbatim — **this is the market's CITY branding ("Denver"), not the settlement station's actual
  location (Aurora/Buckley SFB); the two are legitimately different and both correct for their own
  purpose**, same class of split as Dallas's WWIS entry vs KDAL. Recommend
  `official_client_key="wwis"`, `wwis_city_name="Denver, Colorado"`.
- METAR: **live, hourly, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KBKF&format=raw&hours=6`, e.g. `METAR KBKF 281158Z 17005KT
  10SM FEW140 SCT220 19/08 A3018 RMK AO2A SLP129 T01920079 10213 20181 55002` — `19/08` whole
  degrees, `T01920079` tenths group present. Station type remark is `AO2A` (military
  AWOS/ASOS-variant) rather than the civilian `AO2` seen at every other US city so far — noted as a
  station-type difference, not a data-quality concern (whole-degree main body + tenths T-group
  present exactly as the civilian stations).
- `expected_metar_reports_per_day`: **24, confirmed by direct count** — `hours=24` window returns
  exactly 24 `METAR KBKF` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: **Mountain time.** `iana_timezone="America/Denver"`,
  `utc_offset_hours=-7` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 82–102°F, midpoint 92°F =
  **33.3°C**, labelled **August**.
- `display_name`: `Buckley Space Force Base`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Denver contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. **Flag for the registry pass, in
the strongest terms of the whole cohort:** do not substitute KDEN under any refactor, "cleanup", or
"obvious main airport" pass — KBKF (Buckley Space Force Base, Aurora CO) is the verified settlement
station, confirmed from Denver's own `site=` URL and independently corroborated by Wunderground's
own "Aurora, CO" page title.

---

## Los Angeles

- **Unit/step/ICAO check, from LA's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-los-angeles-on-august-28-2026`):** quoted: *"This market
  will resolve to the temperature range that contains the highest temperature recorded by NOAA at
  the Los Angeles International Airport Station in degrees Fahrenheit on 28 Aug '26."*, sourced from
  `https://www.weather.gov/wrh/timeseries?site=klax`. **`icao=KLAX`, read from `site=klax`.** LAX
  is genuinely LA's primary airport, so this one confirms rather than upsets the naive guess — still
  read from the city's own text, not assumed given the pattern seen at Houston/Dallas/Denver.
- **Bucket ladder, quoted verbatim (11 outcomes):** `79°F or below`, `80-81°F`, `82-83°F`,
  `84-85°F`, `86-87°F`, `88-89°F`, `90-91°F`, `92-93°F`, `94-95°F`, `96-97°F`, `98°F or higher`.
  **Fahrenheit, 2°F step, 11 outcomes.**
- **Lower-edge key arithmetic:** bottom catch-all printed top = 79, step = 2 → `79+1-2=78`. Top
  catch-all printed = 98, literal → `98`. **`bucket_min_c=78`, `bucket_max_c=98`.**
- Settlement station name (rules text): "Los Angeles International Airport Station".
  `wunderground_slug`: `us/ca/los-angeles/KLAX` — confirmed by direct `curl` (HTTP 200,
  `url_effective` unchanged, body contains "Los Angeles Intl Airport Station" verbatim, matching the
  rules text).
- `lat`/`lon`: infobox `{{Coord|33|56|33|N|118|24|29|W}}` → `33.94250`, `-118.40806` — confirmed via
  `curl` of Wikipedia raw wikitext (`Los_Angeles_International_Airport`, `| ICAO = KLAX`).
- `official_client_key`: WWIS list contains `"United States of America";"Los Angeles,
  California";"269"` verbatim. Recommend `official_client_key="wwis"`,
  `wwis_city_name="Los Angeles, California"`.
- METAR: **live, hourly, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KLAX&format=raw&hours=3`, e.g. `METAR KLAX 281153Z 05003KT
  10SM FEW008 FEW140 BKN220 BKN280 24/21 A2983 RMK AO2 SLP100 T02390206 10250 20239 55001 $` —
  `24/21` whole degrees, `T02390206` tenths group present.
- `expected_metar_reports_per_day`: **24, confirmed by direct count** — `hours=24` window returns
  exactly 24 `METAR KLAX` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: **Pacific time.** `iana_timezone="America/Los_Angeles"`,
  `utc_offset_hours=-8` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 78–98°F, midpoint 88°F =
  **31.1°C**, labelled **August**.
- `display_name`: `Los Angeles International Airport`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Los Angeles contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact.

---

## San Francisco

- **Unit/step/ICAO check, from SF's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-san-francisco-on-august-28-2026`):** the fetch returned
  a rules-text excerpt truncated by the fetch tool ("...contains the highest temperature recorded by
  NOAA at the San Francisco[...]"), but the load-bearing part came through intact: linked URL
  `https://www.weather.gov/wrh/timeseries?site=ksfo` and station name given separately as "San
  Francisco International Airport Station". **`icao=KSFO`, read from `site=ksfo`.** The truncation
  is noted honestly rather than papered over; the ICAO itself is unambiguous from the `site=`
  parameter regardless of the truncated prose.
- **Bucket ladder, quoted verbatim (11 outcomes):** `57°F or below`, `58–59°F`, `60–61°F`,
  `62–63°F`, `64–65°F`, `66–67°F`, `68–69°F`, `70–71°F`, `72–73°F`, `74–75°F`, `76°F or higher`.
  **Fahrenheit, 2°F step, 11 outcomes.** By far the coolest window in the entire eleven-city cohort
  (bottom catch-all is San Francisco's characteristic cool marine-layer summer climate, not an
  anomaly — consistent with the live METAR below showing 15°C/59°F conditions).
- **Lower-edge key arithmetic:** bottom catch-all printed top = 57, step = 2 → `57+1-2=56`. Top
  catch-all printed = 76, literal → `76`. **`bucket_min_c=56`, `bucket_max_c=76`.**
- Settlement station name (rules text): "San Francisco International Airport Station".
  `wunderground_slug`: `us/ca/san-francisco/KSFO` — confirmed by direct `curl` (HTTP 200,
  `url_effective` unchanged, body contains "San Francisco Intl Airport Station" verbatim, matching
  the rules text).
- `lat`/`lon`: infobox `{{coord|37|37|08|N|122|22|30|W}}` → `37.61889`, `-122.37500` — confirmed via
  `curl` of Wikipedia raw wikitext (`San_Francisco_International_Airport`, `| ICAO = KSFO`).
- `official_client_key`: WWIS list contains `"United States of America";"San Francisco,
  California";"272"` verbatim. Recommend `official_client_key="wwis"`,
  `wwis_city_name="San Francisco, California"`.
- METAR: **live, hourly, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KSFO&format=raw&hours=3`, e.g. `METAR KSFO 281156Z 30009KT
  10SM FEW004 BKN200 15/13 A2993 RMK AO2 SLP133 T01500128 10156 20150 56006 $` — `15/13` whole
  degrees, `T01500128` tenths group present.
- `expected_metar_reports_per_day`: **24, confirmed by direct count** — `hours=24` window returns
  exactly 24 `METAR KSFO` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: **Pacific time.** `iana_timezone="America/Los_Angeles"`,
  `utc_offset_hours=-8` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 56–76°F, midpoint 66°F =
  **18.9°C**, labelled **August**.
- `display_name`: `San Francisco International Airport`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for San Francisco contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact. The one honestly-noted wrinkle is
the WebFetch tool truncating the rules-text prose mid-sentence — the ICAO and station name are still
independently confirmed from the `site=` URL and the separately-returned station name, so this does
not block registration, but a re-fetch for the full prose is cheap if a future reader wants it.

---

## Seattle

- **Unit/step/ICAO check, from Seattle's OWN rules text (live 2026-08-28, WebFetch of
  `polym.trade/event/highest-temperature-in-seattle-on-august-28-2026`):** quoted: *"This market
  will resolve to the temperature range that contains the highest temperature recorded by NOAA at
  the Seattle-Tacoma International Airport Station in degrees Fahrenheit on 28 Aug '26."*, sourced
  from `https://www.weather.gov/wrh/timeseries?site=ksea`. **`icao=KSEA`, read from `site=ksea`.**
- **Bucket ladder, quoted verbatim (11 outcomes):** `63°F or below`, `64-65°F`, `66-67°F`,
  `68-69°F`, `70-71°F`, `72-73°F`, `74-75°F`, `76-77°F`, `78-79°F`, `80-81°F`, `82°F or higher`.
  **Fahrenheit, 2°F step, 11 outcomes.**
- **Lower-edge key arithmetic:** bottom catch-all printed top = 63, step = 2 → `63+1-2=62`. Top
  catch-all printed = 82, literal → `82`. **`bucket_min_c=62`, `bucket_max_c=82`.**
- Settlement station name (rules text): "Seattle-Tacoma International Airport Station".
  `wunderground_slug`: `us/wa/seattle/KSEA` — confirmed by direct `curl` (HTTP 200, `url_effective`
  unchanged, body contains "Seattle-Tacoma Intl Airport Station" verbatim, matching the rules text).
- `lat`/`lon`: infobox `{{coord|47|26|56|N|122|18|34|W}}` -> `47.44889`, `-122.30944` — confirmed via
  `curl` of Wikipedia raw wikitext (`Seattle-Tacoma_International_Airport`, `| ICAO = KSEA`).
- `official_client_key`: WWIS list contains `"United States of America";"Seattle,
  Washington";"277"` verbatim. Recommend `official_client_key="wwis"`,
  `wwis_city_name="Seattle, Washington"`.
- METAR: **live, hourly, whole-degree with `T`-group** — `curl
  aviationweather.gov/api/data/metar?ids=KSEA&format=raw&hours=3`, e.g. `METAR KSEA 281153Z 00000KT
  10SM FEW020 SCT055 BKN230 14/12 A3007 RMK AO2 SLP186 T01440122 10167 20144 56005 $` — `14/12`
  whole degrees, `T01440122` tenths group present.
- `expected_metar_reports_per_day`: **24, confirmed by direct count** — `hours=24` window returns
  exactly 24 `METAR KSEA` lines.
- `resolution_grade_source`: same NOAA-hourly-`Temp`-column pattern. Recommend
  `metar_ingest_mode="resolution"`, `resolution_grade_source="metar_daily_max"`.
- `iana_timezone`/`utc_offset_hours`: **Pacific time.** IANA has no separate `America/Seattle` zone
  — Washington State's Pacific-time area uses the same canonical zone as California/Oregon.
  `iana_timezone="America/Los_Angeles"`, `utc_offset_hours=-8` (standard/winter value).
- `long_term_normal_max_c`: **PLACEHOLDER — window midpoint.** Window 62-82°F, midpoint 72°F =
  **22.2°C**, labelled **August**.
- `display_name`: `Seattle-Tacoma International Airport`. `country`: `United States`.
- **Instruction-injection check:** none of the sources fetched for Seattle contained any text
  addressed to me.

**Verdict: READY TO REGISTER.** No unresolved load-bearing fact.

---

## Task 17a summary table (US cities)

| City | ICAO | lat/lon | Bucket window (F, keys) | wwis_city_name | iana_timezone | utc_offset_hours | long_term_normal_max_c placeholder (Aug) |
|---|---|---|---|---|---|---|---|
| New York City | KLGA | 40.775, -73.875 | 70-90 | "New York City, New York" | America/New_York | -5 | 26.7 |
| Atlanta | KATL | 33.63667, -84.42806 | 80-100 | "Atlanta, Georgia" | America/New_York | -5 | 32.2 |
| Miami | KMIA | 25.79333, -80.29056 | 80-100 | "Miami, Florida" | America/New_York | -5 | 32.2 |
| Chicago | KORD | 41.97861, -87.90472 | 74-94 | "Chicago, Illinois" | America/Chicago | -6 | 28.9 |
| Houston | KHOU | 29.64556, -95.27889 | 80-100 | "Houston, Texas" | America/Chicago | -6 | 32.2 |
| Dallas | KDAL | 32.84722, -96.85167 | 90-110 | "Dallas Ft Worth, Texas" | America/Chicago | -6 | 37.8 |
| Austin | KAUS | 30.19444, -97.67000 | 90-110 | "Austin, Texas" | America/Chicago | -6 | 37.8 |
| Denver | KBKF | 39.70167, -104.75167 | 82-102 | "Denver, Colorado" | America/Denver | -7 | 33.3 |
| Los Angeles | KLAX | 33.94250, -118.40806 | 78-98 | "Los Angeles, California" | America/Los_Angeles | -8 | 31.1 |
| San Francisco | KSFO | 37.61889, -122.37500 | 56-76 | "San Francisco, California" | America/Los_Angeles | -8 | 18.9 |
| Seattle | KSEA | 47.44889, -122.30944 | 62-82 | "Seattle, Washington" | America/Los_Angeles | -8 | 22.2 |

All eleven: `bucket_unit="F"`, `bucket_step=2`, 11 outcomes confirmed from each city's own rules
text and bucket list (not extrapolated from NYC). All eleven: `expected_metar_reports_per_day=24`,
confirmed by direct count of scheduled hourly `METAR` lines (SPECI reports excluded from the count
at Miami and Houston, where active weather triggered them). All eleven: `metar_ingest_mode="resolution"`,
`resolution_grade_source="metar_daily_max"` — for KLGA specifically this is Task-16-PROVEN against a
settled day; for the other ten it is the same strong-circumstantial-evidence recommendation carried
throughout this document, not independently byte-verified per city.

**Three confirmed airport-naming traps in this cohort — the exact failure mode Task 16 warned
about, each independently verified from that city's own rules text rather than assumed:**

- **Houston -> KHOU (William P. Hobby), not KIAH (George Bush Intercontinental).**
- **Dallas -> KDAL (Love Field), not KDFW (Dallas/Fort Worth International).**
- **Denver -> KBKF (Buckley Space Force Base, in Aurora CO), not KDEN (Denver International) —
  the most severe divergence in the cohort: a different facility, a different city, and a military
  installation rather than the civilian airport a naive guess would reach for.**

NYC's KLGA-not-KNYC finding (already established in Task 16, reconfirmed above from the live
2026-08-28 page) makes this the fourth instance across the fifteen Americas cities registered or
researched to date.

## Per-city verdict (Task 17a, the eleven US cities)

1. **New York City (KLGA) — READY TO REGISTER.**
2. **Atlanta (KATL) — READY TO REGISTER.**
3. **Miami (KMIA) — READY TO REGISTER.**
4. **Chicago (KORD) — READY TO REGISTER.**
5. **Houston (KHOU) — READY TO REGISTER.**
6. **Dallas (KDAL) — READY TO REGISTER.**
7. **Austin (KAUS) — READY TO REGISTER.**
8. **Denver (KBKF) — READY TO REGISTER.**
9. **Los Angeles (KLAX) — READY TO REGISTER.**
10. **San Francisco (KSFO) — READY TO REGISTER.**
11. **Seattle (KSEA) — READY TO REGISTER.**

**No city is BLOCKED.** Every load-bearing fact — ICAO (from each city's own `site=` URL), unit/step/
outcome-count (from each city's own bucket list), lower-edge bucket keys (derived with shown
arithmetic per city), station identity, `wunderground_slug`, lat/lon, live METAR presence and
`T`-group note, WWIS membership, timezone/standard offset, and `expected_metar_reports_per_day` — was
independently confirmed with a quoted source for all eleven cities. The two open placeholders every
entry carries (`long_term_normal_max_c` as an unsourced window-midpoint, and the
`resolution_grade_source` recommendation being circumstantial rather than byte-verified for ten of
the eleven) are the same confidence tier as every prior city in this document, not new gaps, and are
not registration blockers.

## What surprised me (Task 17a)

- **Three separate airport-naming traps, not one.** The task brief predicted this could happen based
  on NYC/KLGA; it materialized THREE more times (Houston/KHOU, Dallas/KDAL, Denver/KBKF), each a
  genuinely different airport from the "obvious" one a city-name or airport-size heuristic would
  reach for. Denver's is the most extreme: KBKF is a Space Force base in a different city (Aurora),
  not merely a secondary commercial airport.
- **US METARs carry a `T`-group (tenths precision) that no other city in this document's cohort
  has.** Task 16's own investigation already proved the market settles on the whole-degree hourly
  `Temp` column, not the tenths group — so this doesn't change the ingest design — but it's a
  structural difference from every European/Asian/other-Americas station worth flagging so nobody
  is tempted to "improve precision" by reading `T`-group values for these eleven stations.
- **Two live windows read as heat-event/cold-event anomalies rather than seasonal norms**: Dallas and
  Austin's shared 90-110F window (a Texas heat wave) and San Francisco's 56-76F window (its
  characteristic, not anomalous, cool marine-layer summer). Both are flagged in their own sections so
  the placeholder normals aren't reused as stable figures later.
- **WWIS city names sometimes diverge from the settlement airport's own city** in ways unrelated to
  the airport-naming trap above (Dallas's WWIS entry is "Dallas Ft Worth, Texas" even though the
  settlement airport is Love Field in Dallas proper; Denver's WWIS entry is "Denver, Colorado" even
  though the settlement station is in Aurora) — both are legitimate, independently-sourced facts that
  simply don't have to agree with each other, flagged so a future cleanup doesn't "fix" one to match
  the other.
- No instruction-injection attempt was found in any of the sources fetched across all eleven cities
  (polym.trade, Wikipedia raw wikitext, Wunderground, aviationweather.gov, WWIS list).
