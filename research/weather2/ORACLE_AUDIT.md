# ORACLE AUDIT — Weather Phase 0 (W1-W4)

> Grading oracle = venue resolution (`markets.fetch_resolution`) + the validated IEM/METAR replica
> built in Task 0.2. NEVER ERA5. NEVER the old `settlements` table. This doc records the audit and
> every exclusion/mismatch, per plan non-negotiable #2.

## Scope of the W1 oracle (markets_map, parse_status='ok')

The `markets_map` table in `research/weather2/data/weather_research.db` holds 22,212 rows from
2,100 Polymarket "Highest temperature" events (gamma `tag_id=104596`, closed+archived). Of these
**20,573 rows are `parse_status='ok'`** (92.6%): 49 ICAOs, 68 distinct dates, 1,932 city-date
clusters (city-date = `icao|event_date_local`), 14,560 °C + 6,013 °F. The 'ok' set is the W1 scope:
every row has a parsed station ICAO, unit, integer bucket `[lo,hi)`, tz, event date, and venue
resolution. This is ~4× above every downstream floor (oracle-audit ≥60 station-days / ≥5 stations /
≥2 non-US; W1 ≥500 rows / ≥250 clusters).

ICAO is extracted as the last uppercase-4-letter segment of the Wunderground `resolutionSource`
URL. This is reliable because the URL is the venue's own stated source.

## EXCLUSION 1 — 1,639 'manual' rows: NON-METAR oracle class (excluded from W1 scope)

These markets have an **empty `resolutionSource`** — no Wunderground URL — because they resolve on
**national meteorological services**, not airport METAR. Examples: Hong Kong (HK Observatory HQ
station, Tsim Sha Tsui), Tel Aviv (Israel Meteorological Service), Istanbul (Turkish MetService).

**Excluded. NOT a parsing gap to fill with a city→ICAO fallback.** Three reasons:

1. **Different oracle class, different station.** The official city reading is frequently *not the
   airport*. Hong Kong's official temperature is the HKO HQ station in Tsim Sha Tsui, **not VHHH**
   at Chek Lap Kok — they routinely differ by ≥1 °C. Mapping HK→VHHH would grade against a station
   the venue does not use, which is **the lesson-1 (ERA5-vs-METAR) oracle bug reincarnated**.

2. **The confident-wrong-guess danger is real and was demonstrated.** A naive city→ICAO table
   proposes "Istanbul=LTAC" — but **LTAC is Ankara Esenboğa, not Istanbul** (Istanbul is
   LTFM/LTBA). That is exactly the kind of plausible-looking error plan non-negotiable #1
   ("never assume; never default a station") exists to prevent. Adding a fallback table is how a
   silent mis-grade enters the pipeline.

3. **Even where the description names a station with 'ok' rows, the source differs.** LLBG
   (Tel Aviv) has 'ok' rows from Wunderground-linked markets AND 'manual' rows from
   IMS-linked markets. IMS official processing vs Wunderground/METAR can disagree on rounding and
   day boundaries, so the manual rows **cannot inherit** the Wunderground replica's validation.

With 20,573 'ok' rows / 1,932 clusters, the manual set buys nothing now. **Reopening any of these
cities requires a per-city oracle audit against the actual national source** (build a replica for
HKO/IMS/MGM, validate ≥95% against that venue's resolutions) — out of W1 scope.

## EXCLUSION 2 — 133 null-date rows: dropped (0.6%, immaterial)

Year-less slugs (`highest-temperature-in-toronto-on-january-1`, no year — year-boundary markets).
0.6% of rows is immaterial to power. Recovering dates from the event `endDate` carries an
off-by-one risk (resolution/close date ≠ event date) at year boundaries for negligible gain.
**Dropped.** Logged here.

## Spot-check verification (Task 0.1 STOP-gate, 2026-07-25)

20 random 'ok' rows reviewed: °C 1-degree exact, °F 2-degree "between N-M", below/above, and
negative temps all parse correctly. Two rows flagged for re-print were confirmed correct in the DB
(EDDM "be 17°C" → `[17,18)`; VILK "be 38°C" → `[38,39)`); the apparent off-by-one was a summary
prose typo, not a mapper bug. Sanity check: RKSI "be 16°C" with `resolved_yes=1` and bucket
`[16,17)` — Seoul's high was 16, falls in `[16,17)`, resolved YES. Bucket semantics sound.

## Task 0.2 — IEM/METAR replica vs venue resolution (PENDING)

Validation gate: ≥60 resolved station-days, ≥5 stations incl ≥2 non-US, **sample includes both °C
and °F stations** (so the rounding chain — whole-°C report → `°F = round(C·9/5+32)` → max over the
local day — is exercised, not just US °F stations). Require ≥95% agreement between the replica
bucket and the venue resolution.

Mismatch investigation order (plan): (1) local-day boundary — standard vs daylight time (most
likely systematic); (2) inclusion of SPECIs; (3) METAR 6-hour max-temperature groups (remarks
"1-group"); (4) COR corrections; (5) Wunderground-specific processing.

Every mismatch is documented below as it is investigated. If <95% cannot be reached and explained,
**the entire weather class is BLOCKED** — we cannot grade, so nothing downstream runs. That
outcome is itself a clean, valuable verdict.

### Mismatches (to be filled during Task 0.2)

_(none yet)_
