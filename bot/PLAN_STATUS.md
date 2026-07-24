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

## REMAINING-EDGES VERIFICATION (Fable-5 plan, executed 2026-07-25 by glm-5.2)
Plan: Remaining Edges Audit + Fable5 Prompt 2026-07-25.md (vault). Q1/Q2 fixed by Fable-5; Q3-Q6 + phased plan synthesized via 4-lens adversarial workflow. Executed P0,P1,P2.1,P2.4,P2.5,P3 tonight. P4/P6 deferred (survivors only, supervised).

P0 — CROSSVENUE KILLED (Q1): edges/crossvenue.py docstring marked KILLED (Kalshi US-persons-only, untradeable from HK; gap info has no action). Scan-only, not crond. Kept for reference.

## REMAINING-EDGES VERIFICATION (Fable-5 plan, executed 2026-07-25 by glm-5.2)
Plan: Remaining Edges Audit + Fable5 Prompt 2026-07-25.md (vault). Q1/Q2 fixed by Fable-5; Q3-Q6 + phased plan synthesized via 4-lens adversarial workflow. Executed P0,P1,P2.1,P2.4,P2.5,P3 tonight. P4/P6 deferred (survivors only, supervised).

P0 — CROSSVENUE KILLED (Q1): edges/crossvenue.py docstring marked KILLED (Kalshi US-persons-only, untradeable from HK; gap info has no action). Scan-only, not cron'd. Kept for reference.

P1 — FLB kill-now gate: DECISIVE PASS.
- Free NO-GO (build_flb_calib._report): longshot [0.00-0.05] freq=0.005 vs midpt 0.025, favourite [0.95-1.00] freq=0.996 vs 0.975 -> FLB present on modern Polymarket, NOT dead-by-construction.
- OOS gate (bot/flb_oos_gate.py): md5(market_id)%5 80/20 split (train 198099 / test 49268; scored 37651 24h+7d points, final snapshot TAUTOLOGY-EXCLUDED, 25553 clusters).
  GATE-1 INFO: OOS log-loss improvement calib vs raw = 0.003723, 95% CI [0.002195, 0.005257] > 0 -> calib ADDS info beyond price. Isotonic control 0.002115 (calib beats generic recalibration ~76%) -> FLB-specific.
  GATE-2 PnL-after-costs (both sides, |gap|>COST=0.03): calib n=19418 mean +$0.0219 CI [+$0.0145, +$0.0295] > 0 (edge exceeds 3% costs); isotonic +$0.0204; calib wins; raw_price 0 trades (sanity).
  VERDICT (bot/FLB_GATE_VERDICT.txt): FLB SURVIVES the T1.1-equivalent gate that killed weather. Real but THIN (~2.2%/trade after costs). Next: P4 realistic-fill + P6 fresh-OOS n>=200 before live un-HALT.

P2.1 — USUD oracle audit: NOT DOA (bot/data/usud_resolution_audit.txt).
- SPY/NVDA/TSLA resolve on Pyth 4pm-ET regular-session close (1-min candle, primary exchange; Pyth falls back to exchange close on its failure); SPX/DJIA on WSJ official index close. All close-vs-prior-trading-day-close. end_date=20:00 UTC=16:00 EDT (NOT midnight; _tau_to_close correct as-is — plan 21:00 UTC assumed winter EST, corrected).
- Yahoo chartPreviousClose=prior-trading-day close=strike; Yahoo exchange matches primary (SPY=PCX/NYSE Arca, NVDA/TSLA=NMS/Nasdaq, SPX=SNP, DJIA=DJI). Same price stream as resolution -> NOT the weather bug (ERA5-vs-METAR was different systems; here Pyth/WSJ close == Yahoo close). Intraday spot is the model INPUT, close is the PREDICTION -> correct forward-digital setup. Correctable calibration bias, not fatal.
- N(d2)+realized-vol form correct; only the vol estimator (1d 5-min, noisy/stale at 19:00 UTC scan) is weak -> correctable, gated behind T1.1.

P2.4 — usud.py hardening: prob_above returns 0.5 (not 0.0/1.0) when sigma<=0/tau<=0 (latent fake-edge guard; unreachable from _price existing guard but defensive). No tau pin (end_date correct).

P2.5 — bot/usud_t11.py: outcome ~ logit(entry_price)+logit(p_model), cluster by trading_date=DATE(end_date America/New_York). Self-check PASS (synthetic 200-row fit coef 1.65 p=0.000). n=9 sanity: INCONCLUSIVE (3 clusters, degenerate; coef sign +207 positive but meaningless at n=9). assert N>=200 / n_clusters>=60 (sub-threshold = early-kill-only, PASS does NOT unblock). Accrues via job_usud cron (now un-halted for paper).

P3 — ARB existence-proof: edges/arb.py compute_bundle now returns best bundle (any net_gap incl negatives) not None; scan_arb logs every bundle to arb_gap_log (new table, self-contained CREATE) + trade-filter moved to scan_arb (behavior-preserving). bundle_id=event_id|side in leg meta (job_arb uses it; analyze_edges already groups by meta "bundle"). Self-check PASS (near-miss net=0.01 logs qualifies=0; qualifying net=0.10 logs qualifies=1). bot/analyze_arb_gap.py prints the distribution. _has_depth walked-fill_size fix DEFERRED. A0 historical gap distribution DEFERRED (optional). Accrues via job_arb cron (4wk/>=500 scans).

CRITICAL FIX: paper edge crons (job_flb/job_arb/job_usud) were HALTed by HALT_LIVE (collateral — they checked HALT_FILE), freezing ALL paper accrual since 2026-07-24 15:26. Decoupled: removed HALT check from paper jobs (run_scan.py); live arm STILL halts via run_live.py independent HALT_LIVE check. Paper accrual (USUD clusters, ARB scans, FLB fresh-OOS) now resumes.

DEFERRED (supervised): P4 fill-realism floor (engine._walk_book -> (avg,filled_shares); edge_engine._store_fill re-walk+taker fee+partial-cap; backtest_usud apply SPREAD/TAKER_FEE) — only for survivors (FLB, USUD-if-T1.1-passes), big shared change. P6 full pre-registered OOS gate + live un-HALT.
Live arm STAYS HALTED (HALT_LIVE). No real money until an edge passes its full gate OOS AND fill-realism survives.
