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
