# W1 posterior-gap readout — PRE-COMMITMENT (locked before looking)

Per Fable-5 assessment 2026-07-26: a 30-min descriptive readout of the posterior-gap
distribution, with the decision rule committed to git BEFORE inspecting the data.
"The human should pre-commit that decision rule before looking at the readout, or
don't look." This doc IS that pre-commitment. Committed 2026-07-26 before the readout
in `posterior_gap_readout.py` is run.

## What the readout computes (descriptive only — no gate, no trade sim)

Using the already-estimated EMOS coefficients (no refit), for each OOS row compute the
"blended posterior gap" = the model's probability of the resolved bucket vs the
snapshot mid, shrunk by the regression coefficient the market applies (the coef says
the market discounts the model's raw divergence to ~18% of face value). Concretely:
`blended_gap = coef_pooled * |logit(p_model) - logit(mid)|` (in logit units), reported
in cents at mid~0.5 (1 logit unit ~ 0.25 at mid 0.5, so blended_gap_cents ~ 0.25 * blended_gap).

Report:
1. Distribution of blended_gap_cents across all OOS rows.
2. Count/frequency of rows with blended_gap_cents > ~4¢ (the "~beats costs" bar).
3. Where those rows live: by station, by date, by bucket position (tail vs middle), by
   whether the model was right (outcome aligned with p_model vs mid).
4. Annualized frequency (rows above 4¢ / OOS-window-days * 365).

## The pre-committed decision rule (locked here, 2026-07-26, before looking)

Fable-5 prior: ~80-85% that the >4¢ tail is thin or pure-model-error territory.

- **IF the annualized frequency of >4¢ blended-gap rows is low (<~50/year) OR the >4¢
  tail shows model-error signature (the model was wrong on the bulk of them, i.e. its
  win-rate on >4¢ rows is NOT clearly above the market's):**
  => **CLOSURE CONFIRMED by arithmetic.** W1 stays dead. Never run another weather
  regression. No split-sample test. Weather fully closed (pending W2 M1 separately).
  Record the readout numbers in W1_T11_VERDICT.txt and stop.

- **IF the >4¢ tail is fat and broad-based (annualized frequency high AND the model's
  win-rate on those rows clearly exceeds the market's — i.e. a real, broad, monetizable
  tail, not noise):**
  => ONE legitimate residual: a single pre-registered split-sample test — fit the
  EMOS/blend on the first half of the OOS window, trade the shrunk rule ONCE on the
  second half, CI>0 ships or dead. This is legitimate science (theory-derived from the
  coef, pre-registered, run once on unexploited data), NOT p-hacking (not a re-run of
  the 6¢ raw rule on the same data). The split-sample test itself needs its own
  pre-registration commit before running.

- **Ambiguous (fat tail but model-error signature, OR thin but model-right):**
  => default to closure (no appeal), record the ambiguity, stop. The pre-reg's
  no-appeal bias binds; ambiguity does not justify a split-sample test.

Hard cap: the readout + any split-sample test <= 8h total (Fable-5 hard cap on all
remaining weather work). If the readout itself takes >1h, stop and report.

## What this pre-commitment forbids

- Running the readout, seeing a thin tail, then "let me also check 3¢" — no. The 4¢ bar
  is locked.
- Running the readout, seeing a fat tail, then tuning the split-sample threshold — no.
  The split-sample uses the shrunk rule at the costs-bar, locked.
- Re-running the original 6¢ raw-divergence rule under any framing — that test is done
  and dead.
- Any maker-execution appeal from paper — refused without live risk (cannot price
  adverse selection on resting orders in METAR-watcher-patrolled books).

## Status

Pre-committed 2026-07-26 (this commit). Readout runs only after this commit is verified
in git history.
