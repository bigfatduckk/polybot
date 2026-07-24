# PLAN_STATUS — polybot IEM pivot (hybrid A+B+E)

Plan source: Fable-5 build plan (pasted 2026-07-24). Execution substrate: VPS droplet 188.166.241.19, repo /root/polybot (github bigfatduckk/polybot), venv .venv (py3.12). Paper-A DB 7.5GB. Live arm HALTED (HALT_LIVE present 2026-07-24 15:26 UTC); 27 open positions resolving naturally via ungated job_maintain_live.

## T1.1 — Logistic edge test (FIRST, blocking) — DONE 2026-07-25
Script: bot/test_model_signal.py (vault + VPS). Run: .venv/bin/python bot/test_model_signal.py
Frame: outcome_yes ~ logit(entry_price) + logit(p_model), YES-frame (fill.price=YES price both sides; p_model=P(YES) from candidates matched on market_id+side+closest-ts; outcome from markets.fetch_resolution).

Result: N=217 resolved (249 fills, 32 unresolved), 66 city-date clusters, pseudo-R2=0.0338.
  logit(entry_price): coef +0.6079, cluster-p 0.005 (market price informative, as expected)
  logit(p_model):     coef -0.0694, cluster-p 0.4695 (model adds NOTHING over price; wrong sign)

## D-4 GATE: FAIL -> STOP -> option D
Pre-registered criterion: logit(p_model) coef > 0 AND cluster-robust p < 0.10. Not met (coef negative, p=0.47).

Pressure-test (why the verdict is robust, not a power fluke) — CORRECTED per Fable-5:
- NOTE: "more data cannot flip it positive-significant" was OVERSTATED. A point estimate of -0.07 @ p=0.47 IS statistically consistent with a small true positive. The decision does NOT hinge on that argument.
- The gate stands on (a) pre-registration (don't relitigate a pre-registered gate) and (b) even the optimistic edge of the CI is an edge too small to justify the retargeting infrastructure.
- Selection on edge is by construction; the regression tests exactly "does p_model predict outcome beyond price" and answers no.
- ERA5 vs station agree ~67%; a skilled p_model would still show attenuated-positive coef pre-retargeting. Zero coef = no skill, not "fixable miscalibration." (Decisive only if Check-1 ERA5 control confirms p_model IS positive against ERA5 — i.e. pipeline correct, no market-payoff edge. If ERA5 coef is also ~zero, frame/matching is broken and must be fixed before any verdict.)

## VERIFICATION (Fable-5, before burying) — DONE 2026-07-25, bot/verify_t11.py
- Check-1 (decisive) ERA5 control, N=210: logit(p_model) coef +0.8672 p=0.0000 (strongly positive). logit(entry_price) coef -0.5323 p=0.066. => Pipeline/matching CORRECT; model has genuine ERA5 skill, but zero payoff-edge beyond market price. Kill confirmed with confidence.
- Check-2 hand-verify 15 rows: p_model=P(YES), outcomes=YES-outcomes confirmed. 70 disagreements/210 = 33.3% = known flip rate.
- Check-3 corr(logit_pmodel, logit_entry) = 0.2424 (below 0.3, but explained by selection-on-divergence; ERA5 coef rules out broken match).

## DECISION: KILL EXECUTED (Option D) — 2026-07-25
Verification held (ERA5 coef positive => pipeline correct, not a frame artifact). Executed Fable-5 step 2:
- Crontab: `--job weather --mode paper` lines (Bot A :05, B :10, C :15) commented `# KILLED-D4 2026-07-25 weather edge dead (see PLAN_STATUS.md)`. maintain/settle/maintain-live KEPT to resolve open positions.
- Live arm: stays HALTED permanently for weather (HALT_LIVE). 27 open resolve via maintain-live.
- Phases 2-5 SKIPPED. No engine.py oracle flip, no IEM pipeline, no refit. Only honesty fix = zero-code post-mortem (`Weather Edge Kill - Post-Mortem 2026-07-25.md` in vault root).
- Data kept: regrade.py, test_model_signal.py, verify_t11.py, all 4 DBs.

## PENDING FOLLOW-UPS (not done — await flat / Marcus)
- Once live arm flat: withdraw/reallocate ~$850 (don't leave parked on venue).
- Once all open paper+live resolve: disable maintain/settle crons too, then archive/compress the 7.5GB paper-A DB (quiescent).
- Key rotation (signer 0x8c9d exposed to AI context 2026-07-24) still deferred — re-raise once arm flat + funds withdrawn.
- Reallocation: run same 2-step audit (oracle via fetch_resolution + fill realism walk-book) + T1.1-equivalent pre-registered gate before sizing flb/arb/usud edges.
