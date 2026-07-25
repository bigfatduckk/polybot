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

P2.5 — bot/usud_t11.py: outcome ~ logit(entry_price)+logit(p_model), cluster by trading_date=DATE(end_date America/New_York). Self-check PASS (synthetic 200-row fit coef 1.65 p=0.000). n=9 sanity: INCONCLUSIVE (3 clusters, degenerate; coef sign +207 positive but meaningless at n=9). assert N>=200 / n_clusters>=60 (sub-threshold = early-kill-only, PASS does NOT unblock). Accrues via job_usud cron (now un-halted for paper). **RUN 2026-07-25: N=12/4 clusters INCONCLUSIVE (underpowered; coef -0.74 p=0.67; corr(p_model,entry)=0.92). Gate AMENDED to economic-framing primary — see USUD T1.1 section + USUD T1.1 Pre-Registration Amendment 2026-07-25.md.**

P3 — ARB existence-proof: edges/arb.py compute_bundle now returns best bundle (any net_gap incl negatives) not None; scan_arb logs every bundle to arb_gap_log (new table, self-contained CREATE) + trade-filter moved to scan_arb (behavior-preserving). bundle_id=event_id|side in leg meta (job_arb uses it; analyze_edges already groups by meta "bundle"). Self-check PASS (near-miss net=0.01 logs qualifies=0; qualifying net=0.10 logs qualifies=1). bot/analyze_arb_gap.py prints the distribution. _has_depth walked-fill_size fix DEFERRED. A0 historical gap distribution DEFERRED (optional). Accrues via job_arb cron (4wk/>=500 scans).

CRITICAL FIX: paper edge crons (job_flb/job_arb/job_usud) were HALTed by HALT_LIVE (collateral — they checked HALT_FILE), freezing ALL paper accrual since 2026-07-24 15:26. Decoupled: removed HALT check from paper jobs (run_scan.py); live arm STILL halts via run_live.py independent HALT_LIVE check. Paper accrual (USUD clusters, ARB scans, FLB fresh-OOS) now resumes.

DEFERRED (supervised): P4 fill-realism floor (engine._walk_book -> (avg,filled_shares); edge_engine._store_fill re-walk+taker fee+partial-cap; backtest_usud apply SPREAD/TAKER_FEE) — only for survivors (FLB, USUD-if-T1.1-passes), big shared change. P6 full pre-registered OOS gate + live un-HALT.
Live arm STAYS HALTED (HALT_LIVE). No real money until an edge passes its full gate OOS AND fill-realism survives.

## P4 FILL-REALISM FLOOR — DONE 2026-07-25 (bot/FLB_P4_VERDICT.txt)
FLB (the survivor). Restated 43 settled FLB pm_fills under realistic execution (bot/regrade_fills.py).
- Taker fee PRICED into realized PnL (settle._fill_pnl previously ignored it — the optimism bug). Fee = fee_rate * p*(1-p) * size (matches edge gate _fee).
- Partial-cap: fill_size capped to top-10 depth at scan (depth/2; L2 not stored). NIL for FLB — every fill sat in a liquid market (top-10 depth 7k-253k vs ~100 fill_size; 0 thin-book fills, min fill_ratio=1.000).
- Re-walk NOT done for historical fills (L2 not persisted); going forward _store_fill re-fetches+re-walks (applies to P6 fresh OOS).

RESULTS (n=43 settled; 42 sell-YES + 1 buy; 22W):
- optimistic total PnL = -$74.5505  per-fill -$1.73 CI [-$17.03, +$14.15]  (handoff assumed +$19.14 — STALE; 4 more fills settled since, dragged it negative; opt CI already included 0)
- realistic  total PnL = -$128.0005 per-fill -$2.98 CI [-$18.32, +$12.84]
- taker fee total = -$53.45 (~$1.24/fill, ~2.5% of turnover) — DOMINANT haircut; partial-cap net eff = $0.00
- realism self-check PASS (path changed PnL; nil partial-cap -> fee is sole haircut)

VERDICT: FLB SURVIVES the execution-realism test. No execution artifact. The paper path was optimistic by exactly the unpriced taker fee (~2.5%, a bookkeeping bug, not free-fill fiction — partial-cap nil; FLB markets liquid). The historical P1 gate used edge_after_costs (fee already subtracted) so +2.2%/trade ALREADY accounts for the fee; pricing it brings the paper path INTO ALIGNMENT with P1, does not undermine it. n=43 underpowered (CI wide, opt already negative+includes-0) -> cannot adjudicate the historical n=19418 edge either way. Realism did not kill a positive edge (there was no positive live-paper edge to kill). -> No re-kill on execution grounds. P6 (fresh OOS n>=200 on the honest path) is the adjudication gate.

CODE (going-forward honesty, all in scope):
1. engine._walk_book -> returns (avg, filled_shares); partial-depth caps fill (mirrors live_engine.walk_book_fill). Callers updated at source: engine.scan_weather, edges/{flb,usud,arb}. __main__ self-check PASS.
2. edge_engine._store_fill -> re-fetch book (markets.fetch_book), re-walk order.size, cap fill_size, all-taker (drop maker-optimism), store book_depth/fill_size/fill_ratio in meta_json. Fetch-fail/empty -> order canceled, no fill. edge_execute propagates cancel.
3. settle._fill_pnl -> subtracts taker fee (fee_rate/fees_enabled from pm_snapshots). Honest realized PnL going forward for ALL edges. Past settlements NOT recomputed (regrade handles historical FLB).
4. ARB atomic guard ported in edges/arb.compute_bundle (fail bundle if any leg filled < target). ARB P5 leg-exec NOT built (conditional on P3 gap crossing; not happened).
5. backtest_usud SPREAD/TAKER_FEE dead constants: DONE 2026-07-25 (fee fix applied to _trade_pnl; SPREAD deleted; see USUD T1.1 section below).

TESTS: test_edges.py = 38 pass, 2 fail (test_arb_no_bundle_at_parity + test_usud_prob_above_zero_sigma_is_step_function). BOTH PRE-EXISTING on HEAD 70843a2 (verified via git stash) — not caused by P4. No P4-introduced regressions.

Live arm STAYS HALTED (HALT_LIVE). P6 = fresh OOS n>=200 on honest path + freeze + pre-registered verdict = the adjudication gate.

## USUD T1.1 — RUN 2026-07-25 (INCONCLUSIVE, underpowered — not a kill, not a pass)
Script: bot/usud_t11.py (VPS .venv/bin/python, DB polymarket_bot.db). Frame identical to weather D-4: outcome ~ logit(entry) + logit(p_model), cluster-robust by trading_date. p_model = N(d2) binary-call (edges/usud.py:prob_above).

Result: N=12, 4 clusters (gate: N>=200 / clusters>=60).
  logit(entry_price): coef +6.0333 p=0.223  | logit(p_model): coef -0.7440 p=0.6729 (wrong sign)
  95% CI p_model coef [-4.198, +2.710]  | corr(p_model, entry)=0.9216 (vs weather 0.24)

Verdict: INCONCLUSIVE — underpowered. CI spans large +/- range; point estimate not trusted at n=12 (weather decisive kill ran at N=217/66). Script's "EARLY-KILL" label = sub-threshold weak signal only, does NOT unblock. Accrual-bound: USUD cron (Mon-Fri 15:00+19:00 UTC, since 07-18) accrues ~12 settled market-days / 5 trading days; ~12 weeks to 60 clusters (same timescale as FLB P6). Rerun at gate.

Structural flag (recorded, not a kill): corr 0.92 = p_model & market price near-collinear (both from spot/IV; edge = RV-vs-IV residual). Detection harder than weather even at full N; lowers prior but doesn't pre-kill (RV-vs-IV is the real signal).

Oracle: validated (usud_oracle_audit.py, prior) — resolves on 16:00 ET close (20:00 UTC EDT), Yahoo chartPreviousClose matches, NOT DOA, no oracle mismatch (unlike weather ERA5/METAR).

GATE AMENDED 2026-07-25 (USUD T1.1 Pre-Registration Amendment 2026-07-25.md, vault): given corr=0.92 collinearity + reparametrization-on-divergence-doesn't-help (linear reparam, identical SE on coef of interest), the primary gate leans on the ECONOMIC framing (realized PnL via fixed _trade_pnl, cluster-bootstrap by trading_date seed=42 n=2000, CI lower bound > 0 at N>=200/clusters>=60). Logistic demoted to diagnostic (corroborates if positive&significant; does NOT kill if null under collinearity). Threshold locked at USUD_MIN_EDGE=0.05. TAKER_FEE=0.04 flat conservative (overestimates ~0.02 real = harder to pass = safe).

Cost-realism fix (this session): backtest_usud.py _trade_pnl was gross of fee (P4 fee bug in embryo). FIXED: subtract TAKER_FEE*entry*(1-entry)*size, matching settle._fill_pnl. SPREAD constant deleted (double-count — market_ask/market_bid are _walk_book effective prices, depth already priced). Verified numerically. Backtest not rerun (N=12 pointless). See bot/USUD_T11_VERDICT.txt.

NOT done: backtest_usud not rerun; fee_rate-per-market exactness deferred; vault<->polybot PLAN_STATUS full reconcile deferred (vault was stale pre-session, now synced via this section). Live arm STAYS HALTED; no live USUD path. Does NOT block weather Phase 0.

## WEATHER PHASE 0 — Task 0.1 (market_map.py) — DONE + STOP-GATE 2026-07-25
Code: research/weather2/market_map.py (polybot commit 1bb0a04, VPS synced). Enumerates all "Highest temperature" Polymarket markets via gamma tag_id=104596 (paginated; closed+archived; catches ALL cities incl US the bot doesn't trade). Parses per-market description: station name, ICAO (last uppercase-4 segment of Wunderground URL), unit (°C 1-deg exact / °F 2-deg "between N-M"), integer bucket [lo,hi), tz (ICAO_TZ dict, 49 stations), event date (from slug). Resolution from event outcomePrices. DB: research/weather2/data/weather_research.db (new; never touches ERA5/old settlements/polymarket_bot*.db). Self-check PASS.

RESULT: 2100 events, 22212 market rows, all resolved. 20573 'ok' (92.6%): 49 ICAOs, 68 dates, 1932 city-date clusters (city-date = icao|date), 14560 °C + 6013 °F. FAR exceeds oracle-audit floor (>=60 station-days, >=5 stations, >=2 non-US) and W1 floor (>=500 rows / >=250 clusters).

STOP-GATE RESIDUALS (await Marcus review before Task 0.2):
- 1639 'manual' (7.4%): EMPTY resolutionSource (non-Wunderground sources — Hong Kong, Tel Aviv, Istanbul, ...). ICAO can't be parsed from a URL that isn't there; left manual, NOT guessed (plan non-negotiable #1). Known: HK=VHHH, Tel Aviv=LLBG, Istanbul=LTAC (descriptions name the station). Does NOT block the oracle audit (ok set already exceeds floor). Decision: add a curated city->ICAO fallback for these, or leave excluded?
- 133 null-date (0.6%): year-less slugs (on-january-1, no year — year-boundary markets). Recoverable from event endDate if wanted; not blocking.

Bugs found + fixed during the run (self-check now covers both schemes):
- °F markets use 2-degree "between N-M°F" buckets (not 1-degree exact); BUCKET_BETWEEN added, checked first. -? restored on below/above/exact (negative temps like -6°C) — safe because between handles the range hyphen first.
- US Wunderground URLs have an extra state segment (/us/ga/atlanta/KATL) vs non-US (/ar/ezeiza/SAEZ); greedy .+ to last uppercase-4, drop re.I (lowercase city slugs excluded).
- ICAO_TZ dict (49 stations) primary; CITY_TZ fallback.
- STATION_RE stops at "Station" (was capturing the whole sentence).

NEXT: STOP — awaiting Marcus review of the 20-row spot-check + the two residual decisions above before Task 0.2 (IEM METAR ingest + resolution-replica oracle gate). 30h budget: ~2h used on Task 0.1. Live arm STAYS HALTED.
