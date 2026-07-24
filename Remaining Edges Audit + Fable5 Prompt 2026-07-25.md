# Remaining-edges audit + Fable-5 prompt — 2026-07-25

Weather edge killed at D-4 (see `Weather Edge Kill - Post-Mortem 2026-07-25.md`). This doc audits the four surviving paper-only edges for the same class of error that killed weather (wrong settlement oracle / unrealistic fill assumptions), states what's right and wrong, and ends with a copy-paste prompt for Fable-5 (planning only; glm-5.2 executes).

All facts below were read from the canonical code on the VPS (`/root/polybot/bot/`) + the live DBs on 2026-07-25.

---

## Part 1 — Audit findings

### Headline (the good news)
**The edges do NOT have the weather wrong-oracle bug.** The edge settlement path `settle.py:sweep_resolutions(edge)` (line 63) calls `markets.fetch_resolution(market_id)` → gamma `/markets?id&closed=true&archived=true` → `outcomePrices` (the venue's actual resolution) → writes `pm_settlements.resolved_yes` + `pnl` per fill via `_fill_pnl(side, price, size, yes_won)`. So **all edge settlements are market-graded against the venue**, not an internal reanalysis. `job_settle` runs this for `flb`, `arb`, `usud` (crossvenue has no fills). The weather bug was specifically `engine.sweep_settlements` → `_fetch_observed_high` (ERA5); that path is weather-only and now dead.

### Shared fill-realism concern (all paper-traded edges)
Candidate pricing is **realistic**: `flb`/`arb`/`usud` all compute `effective_price = _walk_book(asks/bids, target_shares)` — a proper volume-weighted book-walk at target size (`engine._walk_book`, verified).

But **execution is optimistic.** `edge_engine.edge_execute` → `_store_fill` records the fill at `order.price` (= the scan-time `effective_price`), full `order.size`, **instantly**, with: no re-walk at execution time, no partial fills, no maker-wait, no adverse-selection model. The "maker" flag (`edge_engine:135`, set when `edge_after_costs >= MIN_EDGE+0.02` and `horizon > 0.25d`) is cosmetic in paper — maker orders fill instantly at the maker price, which never happens for free in reality. So paper PnL assumes frictionless full-size fill at the scan-time walked price.

### Per-edge data substrate (as of 2026-07-25, from `polymarket_bot.db`)

| edge | candidates | fills | settled | W/L | settled PnL | gate target | status |
|---|---|---|---|---|---|---|---|
| flb | 79 | 66 | 39 | 21/18 | +$19.14 | n≥200 | far |
| usud | 13 | 12 | 9 | 8/1 | +$161.69 | n≥200 | far (tiny) |
| arb | 0 | 0 | 0 | — | — | n≥50 bundles | **dormant — never triggered** |
| crossvenue | n/a (scan-only) | 0 | n/a | n/a | n/a | n/a | scanner, no fills |

**No edge is anywhere near a live verdict.** The samples are tiny and/or zero. Accrual continues (edge crons `flb` 06:30, `arb` :07/:37, `usud` 15:00/19:00 weekdays, `settle` :45 — all still running; only weather scans were killed).

### Per-edge findings

#### FLB (favorite-longshot bias) — `edges/flb.py`
- **What it is:** generic binary markets with volume/liquidity + horizon in `[FLB_HORIZON_DAYS]`. `p_model = calib[price_bucket, horizon]` from `data/flb_calib.json` (present, 3 KB, built 07-16 from the Zenodo Polymarket historical dataset per the edge-expansion memory). If calib missing → `p_model = price` → edge 0 → no bets (inert, safe).
- **Oracle:** venue (`fetch_resolution`). ✅
- **RIGHT:** candidate `effective_price` walk-book ✅; calib is a price→prob map learned from historical *resolutions* (venue), not an internal reanalysis — so no ERA5-style circularity at the data-source level.
- **WRONG/RISK — the gate is too weak (the weather lesson, restated):** `analyze_edges._flb_gate` = n≥200 + reliability ≤10pp + PnL CI excludes 0. There is **no test of whether `p_model` adds information beyond the raw market price.** This is the decisive gap: since `p_model` is literally a function of price (the calib is a price→prob table), the FLB "edge" could be fiction exactly like weather's ERA5 "edge" was — *the same transformation-of-information, not addition-of-information*. A T1.1-equivalent logistic (`outcome ~ logit(entry_price) + logit(p_model)`, cluster-robust) must gate FLB pre-live, OOS. If `logit(p_model)` adds nothing beyond `logit(entry_price)`, FLB is dead for the same reason weather died.
- **WRONG/RISK — calib OOS unverified:** the calib table is fixed (07-16); its Brier edge has not been measured out-of-sample on the *venue* resolution. (The in-sample reliability table in `analyze_edges` is exactly the kind of in-sample number that lied for weather.)
- Fill realism: directional taker, same class as weather — weather filled cleanly live (~0 slippage), so acceptable, but the scan-price-instant-fill optimism still applies.

#### ARB (NegRisk multi-outcome arbitrage) — `edges/arb.py`
- **What it is:** NegRisk events (2–`ARB_MAX_OUTCOMES` outcomes); buy all YES legs if `sum(eff_ask) < 1` (net_gap = 1 − sum − fees > `ARB_MIN_GAP`), or sell all if `sum(eff_bid) > 1`.
- **CORRECTION during this audit:** I first suspected arb was scan-only. It is NOT — `job_arb` builds an `EdgeOrder` per leg, risk-checks each, and `edge_execute`s each leg into `pm_fills`. `settle` resolves each leg via `fetch_resolution`. So arb has a real paper-trade + settlement path.
- **HOWEVER — arb is DORMANT:** `pm_fills`/`pm_candidates`/`pm_settlements` for `arb` are all **0**. `compute_bundle` has never found a `net_gap > ARB_MIN_GAP` in production. The arb edge is entirely unvalidated AND has produced zero paper data.
- **WRONG/RISK — fill realism is worst for arb (the most fill-sensitive edge):** each leg fills *independently* at its scan-time walk-book price, full `shares`, instantly. Real arb requires **all legs to fill or you carry naked exposure**; real arb gaps are thin (cents) and **any leg's book moving or thinning between scan and execution eats the gap**. The paper assumes frictionless simultaneous full-size leg fills. Paper arb PnL is an upper bound; the real edge could be negative after leg slippage, partial fills, and the fact that you often can't cancel one leg after another fills.
- **Gate too weak:** `_arb_gate` = n≥50 settled bundles + net PnL CI excludes 0. No fill-realism adjustment, no modeling of leg-execution risk. At 0 bundles, irrelevant until it triggers.
- kelly_fraction=0 (arb has no `p_model`; it's structural) — correct.

#### CROSSVENUE (Polymarket vs Kalshi) — `edges/crossvenue.py`
- **What it is:** matches Polymarket and Kalshi markets by question text (Jaccard match_score ≥ 0.34), logs the YES-price gap to `cv_gaps`.
- **WRONG — structurally dead for HK (kill candidate):** Kalshi is CFTC-regulated, **US-persons only, not accessible from Hong Kong.** Plan v0.2 already cut cross-platform arb for this exact reason. `crossvenue.py` scans a gap on a venue Marcus **cannot trade one leg of**. Not a viable live arm.
- **WRONG — fill model is crude:** uses `pm_yes = (best_bid + best_ask)/2` (mid, not walk-book) and subtracts a flat `SPREAD_ESTIMATE = 0.01` for the cross spread. Overstates executable gaps. Moot if killed.
- **Not even cron'd:** no `--job crossvenue` in the crontab. It is a dormant scanner that has never run automatically. `cv_gaps` appears empty.
- Recommendation: kill crossvenue (or retain as info-only, no live arm, no build effort).

#### USUD (US stock up/down) — `edges/usud.py`
- **What it is:** Polymarket "X up or down on [date]" daily stock-direction markets (SPY/SPX/DJIA/NVDA/TSLA). `p_model = N(d₂)` (binary-call) using `spot = Yahoo regularMarketPrice`, `strike = prior_close = Yahoo chartPreviousClose`, `vol = intraday realized vol` (5-min closes, 1-day range, annualized at 252×78 bars).
- **Oracle:** venue (`fetch_resolution`). ✅
- **WRONG/RISK — the weather lesson, unverified (HIGHEST PRIORITY):** the model prices `P(spot > prior_close)` using **intraday Yahoo spot + realized vol**, but the market resolves on the **close** (resolution source **NOT confirmed from the market description this session** — the gamma slug search came back empty, so I could not read the resolution-source text). This is exactly the class of error that killed weather (model grades one quantity, market resolves another). Must: (a) read the USUD market description, confirm what resolves it (likely official NYSE/Nasdaq close — but confirm, don't assume); (b) audit whether the model's inputs match that quantity — intraday spot ≠ close (early-day spot is a noisy proxy; `tau` handles diffusion to close but drift `r=0`, no overnight-jump / fat-tail model), Yahoo spot vs official close, realized vol as a proxy for the market-expected (implied) vol.
- **Gate exists in WEAKER form:** `backtest_usud.py` runs a walk-forward sim with Brier(model) vs Brier(market_ask) + reliability + PnL CI. But **Brier < market can be beaten by recalibration alone, not necessarily by adding information beyond price.** The decisive T1.1 logistic (`outcome ~ logit(entry_price) + logit(p_model)`) has not been run on USUD.
- **Minor bug:** `backtest_usud.py` defines `SPREAD = 0.05` and `TAKER_FEE = 0.04` but `_sim`/`_trade_pnl` never apply them — they're dead constants (printed in the footer, not used). The backtest doesn't model spread+fee despite its PASS message referencing them.
- Data source fragility: Yahoo chart API (`query1.finance.yahoo.com/v8/finance/chart`) can rate-limit/block; `_fetch_quote` returns None on failure → no candidate (safe-fail).

### Cross-cutting (applies to all surviving edges)
1. **No T1.1-equivalent gate has been run on any edge.** The lesson from weather: a pre-registered test of "does the model's signal add information beyond the entry price for the *payoff* outcome" is the cheapest decisive kill-trigger and must gate each edge pre-live.
2. **Fill-at-scan-price optimism** in `edge_engine.edge_execute` affects flb/usud (and arb worst). Paper PnL is an upper bound.
3. **All samples are far below gate thresholds** (flb 39, usud 9, arb 0). No edge is near a live verdict. Accrual is ongoing.

### What I did NOT verify (Fable/glm must)
- USUD market resolution source (couldn't fetch description live — slug empty in gamma).
- FLB calib construction details (the Zenodo dataset's resolution source) — `build_flb_calib.py` not read this session.
- Whether `_walk_book`'s `levels` always include depth ≥ target_shares (edge cases where the book is thinner than target → `filled < target_shares` → returns a partial-walk avg price, which is then filled at full `order.size` in paper → another fill-realism gap).

---

## Part 2 — Fable-5 prompt (paste into Cursor, planning only)

> Copy everything below the line into Cursor with Fable 5 selected. Do not let it write code — plan only. glm-5.2 will execute in ultracode mode, task-by-task.

---

**To:** Fable 5 (Claude, max reasoning, planning only — do NOT write code)
**From:** Marcus
**Date:** 2026-07-25
**Role I want from you:** Senior quant + ML-strategy second opinion. Read this cold (it is self-contained). The weather edge was just killed by a pre-registered D-4 gate (logistic test: the model's `p_model` added no information beyond the entry price for the payoff). I am now auditing the four surviving paper-only edges before any of them gets a live arm. I have already done the primary-source audit (code + live DBs) — your job is to (a) tell me which edges are worth pursuing and which to kill, (b) propose per-edge corrections, and (c) produce a phased, file-level, gate-driven **verification** plan that glm-5.2-ultracode can execute task-by-task. Be direct; prioritize correctness over politeness; I have been burned by in-sample numbers lying twice.

### The system (post-weather-kill)
- Repo `github.com/bigfatduckk/polybot`, deployed `/root/polybot/bot/` on a DigitalOcean SG droplet (1 GB). venv `.venv` (py3.12; `statsmodels`/`scipy`/`httpx`/`numpy`/`pandas` installed). DBs: `polymarket_bot.db` (paper, shared by weather-A-remnant + edges). Live arm HALTED permanently for weather (`HALT_LIVE`); ~$850 to withdraw once the 27 open positions resolve.
- Edge crons still running: `flb` 06:30, `arb` :07/:37, `usud` 15:00 + 19:00 weekdays, `settle` :45. (`crossvenue` has no cron.)
- Constraints: solo dev ~5–15 h/wk, OpenRouter $20/mo cap, ponytail/YAGNI (minimum code, no speculative abstraction, no dashboards/config systems), one self-check per non-trivial change, single-file where possible. Planning in Fable-5; execution in glm-5.2 ultracode.

### The audit I already did (verified facts — trust these, but push back if you see a hole)

**Shared (all paper-traded edges):** settlement uses the venue oracle. `settle.py:sweep_resolutions(edge)` → `markets.fetch_resolution(market_id)` (gamma `/markets?id&closed=true&archived=true` → `outcomePrices`) → `pm_settlements.resolved_yes` + `pnl` via `_fill_pnl(side, price, size, yes_won)`. The weather wrong-oracle bug (ERA5 via `engine._fetch_observed_high`) does NOT exist for the edges. Candidate `effective_price = _walk_book(levels, target_shares)` (proper volume-weighted book-walk — realistic at scan time). **BUT execution is optimistic:** `edge_engine.edge_execute` → `_store_fill` fills at the scan-time `effective_price`, full `order.size`, instantly — no re-walk at execution, no partial fills, no maker-wait/adverse-selection. The "maker" flag (`edge_engine:135`) is cosmetic in paper (maker orders fill instantly at the maker price).

**FLB** (`edges/flb.py`): generic binary markets, `p_model = calib[price_bucket, horizon]` from `data/flb_calib.json` (3 KB, built 07-16 from a Zenodo historical Polymarket dataset). If calib missing → `p_model=price` → no bets. Paper-traded via `job_flb` → `edge_propose` → `edge_execute`. Data: 79 candidates / 66 fills / **39 settled** (21W/18L, +$19.14). Gate (`analyze_edges._flb_gate`): n≥200 + reliability ≤10pp + PnL CI excludes 0 — **NO "p_model beyond price" test, and p_model IS a function of price → this is the exact circularity-risk that killed weather.**

**ARB** (`edges/arb.py`): NegRisk multi-outcome; buy all YES if `1 − sum(eff_ask) − fees > ARB_MIN_GAP` (sell symmetric). `job_arb` builds an `EdgeOrder` per leg and `edge_execute`s each into `pm_fills`; `settle` resolves each leg via `fetch_resolution`. **But `pm_fills`/`pm_candidates`/`pm_settlements` for arb are all 0 — `compute_bundle` has never found a qualifying gap in production.** Fill realism worst: each leg fills independently at scan-time walk-book price, full size, instant — no modeling of leg books moving, partial fills on one leg (real arb is all-or-nothing; a failed leg leaves naked exposure), or slippage eating the thin gap. Gate (`_arb_gate`): n≥50 settled bundles + net PnL CI excludes 0 (no fill-realism adjustment). kelly_fraction=0 (structural, correct).

**CROSSVENUE** (`edges/crossvenue.py`): matches Polymarket↔Kalshi by question Jaccard ≥ 0.34, logs YES-price gap to `cv_gaps`. **Scan-only — no `pm_fills`, no `edge_execute`, not even cron'd.** Uses `pm_yes = (best_bid+best_ask)/2` (mid, not walk-book) minus flat `SPREAD_ESTIMATE=0.01`. **Kalshi is US-persons-only, not accessible from Hong Kong** (Plan v0.2 already cut cross-platform arb for this). Likely dead on arrival.

**USUD** (`edges/usud.py`): Polymarket "X up or down on [date]" (SPY/SPX/DJIA/NVDA/TSLA). `p_model = N(d₂)` (binary-call) with `spot = Yahoo regularMarketPrice`, `strike = prior_close = Yahoo chartPreviousClose`, `vol = intraday 5-min realized vol` (1-day range, annualized 252×78). Paper-traded via `job_usud`. Data: 13 candidates / 12 fills / **9 settled** (8W/1L, +$161.69). Gate exists in weaker form (`backtest_usud.py`: walk-forward sim, Brier(model) vs Brier(market_ask) + reliability + PnL CI) — **but Brier can be beaten by recalibration, not necessarily by adding info beyond price; the decisive T1.1 logistic has not been run.** `backtest_usud.py` defines `SPREAD=0.05`/`TAKER_FEE=0.04` but never applies them (dead constants). **The weather lesson, UNVERIFIED here:** the model prices `P(spot > prior_close)` using intraday Yahoo spot + realized vol, but the market resolves on the close — and I could NOT fetch the USUD market description this session (gamma slug search empty), so the resolution source is unconfirmed. This is the same class of error that killed weather.

### My specific questions for you
1. **Kill/pursue per edge.** FLB, ARB, USUD, CROSSVENUE — which survive, which die? (CROSSVENUE looks dead-for-HK to me; confirm or object.)
2. **The T1.1 gate, generalized.** Weather died because `logit(p_model)` added nothing beyond `logit(entry_price)` for the payoff. For each surviving edge, specify the pre-registered "does the signal add info beyond price" test and its kill threshold. For FLB this is existential (p_model is a function of price — if it adds nothing, FLB is fiction by construction). For USUD, what's the right predictor set beyond the market price (N(d₂)? logit(p_model) + logit(price) + realized-vol + tau + time-of-day)? For ARB (no p_model), what's the equivalent gate — is it purely "do detected net_gaps survive a realistic leg-execution simulation"?
3. **USUD oracle audit (the weather lesson applied).** Concretely: what should glm verify from the USUD market description (resolution source = official close? which exchange?), and what's the falsification — if the model's spot/vol source differs from the resolution source, is USUD dead-on-arrival like weather was, or is the spot-vs-close gap a correctable bias? Is N(d₂) with realized vol even the right model, or should it be implied vol / a jump-diffusion / a simpler "is the stock currently above prior_close weighted by time-to-close"?
4. **Fill realism.** The paper fills at scan-time walk-book price, full size, instant, no partial/maker-wait. For ARB (worst) and FLB/USUD, specify the realistic paper-fill model glm should build before trusting any paper PnL — re-walk at execution? partial-fill / failed-leg modeling for arb? adverse-selection for maker fills? What's the minimum that makes paper PnL trustworthy enough to gate on?
5. **Data starvation.** FLB 39 / USUD 9 / ARB 0 settled. None near 200. Is there a cheaper decisive gate (like T1.1 was for weather) that kills or advances an edge on the existing thin data + historical archive, before waiting 8–10 weeks for 200 fresh signals? (For USUD, `backtest_usud.py` reads `usud_quotes` — is there enough history to run the T1.1 logistic NOW on archived quotes + resolutions?)
6. **Anything I missed.** The dead `SPREAD`/`TAKER_FEE` constants in `backtest_usud.py`; the `_walk_book` partial-walk edge case (book thinner than target → avg of partial depth, then filled at full `order.size`); the FLB calib's own OOS validity; anything else.

### What I want from you (the plan, for glm-5.2-ultracode to execute)
A concrete, phased, file-level **verification + upgrade** plan. For each surviving edge: the cheapest decisive test first (T1.1-equivalent), the corrections (fill realism, oracle audit), and the pre-registered go/no-go gate. STOP-gates between phases (a D-trigger that halts the edge if its signal adds nothing beyond price). Mark which tasks are independent (fan out) vs sequential. Keep scope minimal per YAGNI — no observability dashboards, no config systems, no new abstractions beyond what each task needs. Each phase ends with a one-line "done = …". Note where glm must NOT improvise (e.g., the USUD resolution source must be read from the market description, not assumed; the T1.1 logistic must be cluster-robust by city/event-date).

Live arm stays HALTED until an edge passes its full pre-registered gate. No real money until a T1.1-equivalent gate passes OOS AND the fill-realism model shows the paper edge survives realistic execution.

**Do not write code. Output the plan only.** Detailed enough that glm-5.2-ultracode executes it task-by-task without re-asking you.
