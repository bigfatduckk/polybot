# W1 — Station regime-bias T1.1 pre-registered gate

**Pre-registered 2026-07-25, BEFORE the T1.1-W1 regression runs (Task 1.4).** This locks the
verdict rule, thresholds, n, OOS window, controls, and the falsification clause so the gate cannot
be p-hacked in hindsight. Committed to git before Task 1.4 executes; Task 1.4 verifies this commit
exists in git history before running. Thresholds copied VERBATIM from
`Weather Angles W1-W4 - Verdicts + Test-Build Plan 2026-07-25.md`.

**Scope:** the 48-station, °C+°F, non-US-heavy W1 oracle set from Phase 0 (Task 0.1 + 0.2). VHHH
(excluded, cause unestablished), the 1,639 manual rows (non-METAR oracle class), and the 133
null-date rows stay excluded. p_model_w1 = EMOS predictive distribution (Task 1.2-1.3), fit on
station-days ≤ 2025-09-30 ONLY, never seeing market prices or test-window outcomes.

## Row definition
One bucket of one resolved city-date market, with snapshot mid ∈ [0.05, 0.95]. Snapshot = last
price at or before 00:00 local standard time of the market day D (inclusion requires a quote
within the preceding 12 h) — fetched by Task 0.3 (`price_snapshots`).

## Regression (the statistical gate)
`outcome ~ logit(snapshot_mid) + logit(p_model_w1)`, cluster-robust SE by city-date.
- OOS window: markets resolving 2025-10-01 → 2026-06-30.
- **Statistical survive: coef(logit p_model_w1) > 0 AND cluster-robust two-sided p < 0.05.**

## Economic gate (must ALSO hold)
Policy sim "trade when |p_model_w1 − snapshot_mid| > threshold", threshold pre-registered at
**0.06** (= 2¢ half-spread + 2% taker fee + margin). Entry = mid ± 2¢ against you;
PnL = payout − entry − 2% taker fee. Cluster-bootstrap CI by city-date, **n_resample=2000,
seed=42** (matches T1.1/P6 conventions).
- **Economic survive: per-trade EV after costs, 95% CI lower bound > 0.**

## Survive = BOTH
1. Statistical: coef(logit p_model_w1) > 0 AND cluster-robust p < 0.05.
2. Economic: EV-after-costs cluster-bootstrap CI lower bound > 0.

## Sample floor + single pre-registered fallback
- **N ≥ 500 rows across ≥ 250 city-date clusters.**
- Fallback if short: extend OOS window back to **2025-07-01** (never overlapping training, which
  ends 2025-09-30). This is the SINGLE pre-registered extension; no other window shopping.
- Still short → verdict = INSUFFICIENT-DATA → weather closed on cost grounds.

## Pre-registered subgroup (locked now, not post-hoc)
- Primary verdict = pooled.
- Secondary: non-US-stations-only, held to stricter **p < 0.01**.
- If pooled fails BUT non-US passes at p < 0.01 AND its economic gate passes → continue restricted
  to non-US cities.
- Any other subgroup slicing = p-hacking, forbidden.

## Three controls (all mandatory)
1. **Meteorological-skill control:** EMOS must beat the raw grid forecast on OOS CRPS and show a
   roughly flat PIT histogram on held-out station-days. Fails → pipeline bug, fix before any verdict.
2. **Harness-replication control:** same retrospective T1.1 with p_model = the killed NWP-blend
   (reconstruct via `climatology.py` pooling, α=0.30). Expected: coef ≈ 0 (replicates the D-4 kill
   on the retrospective frame, validates the new harness end-to-end). If the killed model shows up
   significant here, the retrospective frame is broken — STOP and debug before any verdict.
3. **Circularity guard:** training data ends 2025-09-30; test markets start 2025-10-01; the EMOS
   target is the validated METAR-replica max, which is the same thing the venue resolves on — so
   oracle and payoff coincide by construction (that's the fix for the old bug, not a new instance).

## Grading source — explicit (so the 90 residual mismatches cannot confuse anyone)
**T1.1 outcomes come from the VENUE resolutions (`resolved_outcome` in `markets_map`), NEVER from
the replica.** The replica's jobs are (a) proving we understand the oracle — done, Task 0.2 — and
(b) the EMOS *training target*. The 0.44% replica-vs-venue residual therefore only injects tiny
target noise into EMOS training (harmless, slightly conservative); it **cannot contaminate
grading**, because grading reads the venue outcome directly. Known confound for the subgroup
readout: the 90 residuals are concentrated in ZGSZ Shenzhen (54) / RKSI Seoul (24) / MPMG Manila
(8) — if those cities drive a non-US subgroup signal, the rounding-convention noise is a confound
to weigh, not a reason to dismiss.

## Falsification clause (the verdict's meaning)
If the EMOS shows genuine meteorological skill (control 1 passes: CRPS beats raw forecast, PIT
calibrated) BUT T1.1 coef(p_model_w1) ≤ 0 OR p ≥ 0.05 pooled AND non-US at p ≥ 0.01 — then **the
market already embeds station-level post-processing, W1 is the same trap with a different oracle,
and the ENTIRE weather class (W1-W4) is closed permanently.** No recalibration appeal —
recalibration re-maps, it doesn't add. That result is the exact analog of the D-4 kill and gets
the same treatment. (This is the $0 answer to whether weather deserves another evening — a clean,
valuable verdict either way.)

## Anti-p-hacking commitments
- No threshold tuning after seeing data. The 0.06 trade-rule threshold is locked here.
- No post-hoc subgroups. The pooled + non-US-at-0.01 are the only two reads.
- No window shopping beyond the single pre-registered 2025-07-01 fallback.
- No re-running with a different seed. seed=42, n_resample=2000, locked.
- No "it's close, let it run more" — if the freeze lands and the gate fails, that is the verdict.

## Deliverable at freeze
`research/weather2/W1_T11_VERDICT.txt`: coefficients, cluster-p, N, clusters, subgroup result,
control results (CRPS/PIT, killed-blend coef), EV + CI, and the verdict per the pre-registered
rule. Append `bot/PLAN_STATUS.md` + vault `Changelog.md` + memory. Human review before Phase 2
regardless. Live arm STAYS HALTED; no live-trading code path in any phase; un-HALT is a manual
user action after a P3 SHIP verdict only.

## Status
- Pre-registered 2026-07-25 (this commit). Committed BEFORE Task 1.4 runs.
- Phase 0 done (Tasks 0.1, 0.2 — oracle gate PASSED 99.56%). Task 0.3 (price snapshots) running
  in parallel with 1.1-1.3. Task 1.4 runs only when both tracks land AND this commit is verified
  in git history.
