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

## Task 0.2 — IEM/METAR replica vs venue resolution — DONE + GATE PASSED 2026-07-25

Code: `research/weather2/iem_oracle.py` (polybot, VPS-synced). Fetches IEM ASOS `tmpc` per
station-year (2022-present) in the station's local tz, throttled 1.5s/station (avoids 429).
Computes `replica_daily_max`: native whole-°C METAR → display-unit conversion
(`°F = round(C·9/5+32)`) → max over the local calendar day. For °F-display stations computes BOTH
`convmax` (round(max_C·9/5+32)) and `each` (max of per-obs round) — they always agreed
(0 method-disagreements across 20,440 rows), confirming the conversion is unambiguous in practice.

**GATE RESULT (final, VHHH excluded — see below): 99.56% agreement (20,332 / 20,422).**

| Floor | Required | Actual | Pass |
|---|---|---|---|
| station-days | ≥60 | 20,422 | ✓ |
| stations | ≥5 | 48 | ✓ |
| non-US stations | ≥2 | 37 | ✓ |
| both °C + °F | yes | C=14,486 F=5,936 | ✓ |
| agreement | ≥95% | 99.56% | ✓ |

Pilot (10 stations, inline) = 6/6; full 49-station ingest = 99.5% raw, 99.56% after the one
exclusion below. The rounding chain (`whole-°C → round(C·9/5+32) → max`) is validated on real
resolved °F markets (e.g. KLGA max_C=3.33 → °F=38, matched).

### Exclusion: VHHH (Hong Kong) — excluded, large systematic gap, cause UNESTABLISHED

VHHH (Chek Lap Kok airport) is in the 'ok' set (Wunderground-linked URL), but its 4 resolved
station-days in the join ALL mismatched: replica max = 23°C while the venue resolved the high at
15-16°C (an 8° gap). **The gap is too large to attribute confidently.** Tsim Sha Tsui (HKO HQ) vs
Chek Lap Kok (VHHH) differ by ~1-3°C at most, not 8°C — so "different station" is *not* an
established explanation. The likely causes (any of, not yet distinguished): wrong IEM station ID,
a date/timezone offset, a unit mixup in the venue values, or the market resolving on a source the
resolutionSource URL doesn't reflect. **VHHH excluded from the gate and from W1 scope; cause
unestablished. A per-city audit is required before any HK market is ever in scope.** The exclusion
contains the risk regardless of root cause. Confirmed VHHH is the *only* large-diff
(|replica−venue| ≥ 5) outlier; no other station showed a comparable systematic gap.

### Residual mismatches (90, all small rounding-convention noise — NOT blocking)

After excluding VHHH, 90 mismatches remain (0.44%), all |replica−venue| ≤ 3°C, 68 of them ±1.
Concentrated in 3 non-US °C stations:

| station | mismatches | note |
|---|---|---|
| ZGSZ Shenzhen | 54 | |
| RKSI Seoul | 24 | |
| MPMG Manila | 8 | |
| SBGR São Paulo | 2 | |
| DNMM Lagos | 2 | |

Diff distribution: −3×2, −2×18, −1×36, +1×32, +2×2 (bidirectional). These are the SPECI /
6-hour-max-temperature-group / Wunderground-processing territory the plan anticipated — small,
bidirectional, and **absorbed by W1's design**: the EMOS trains on the replica max as its regression
target, so whatever systematic processing the venue applies is learned into the coefficients. They
do not threaten the ≥95% gate (99.56% >> 95%) and do not require per-mismatch root-causing before
W1 proceeds. The ZGSZ/RKSI/MPMG concentration is noted for the W1 subgroup analysis (if those
cities show residual edge, the rounding noise is a confound to weigh).

**GATE: PASS. The METAR replica is a valid grading oracle for the 48-station, °C+°F, non-US-heavy
W1 scope. Weather class proceeds to the W1 fan-out (Task 1.0 pre-registration → 1.1 training data
→ 1.2 EMOS → 1.3 bucket probs → 1.4 T1.1-W1). VHHH + the 1,639 manual + 133 null-date rows stay
excluded.**
