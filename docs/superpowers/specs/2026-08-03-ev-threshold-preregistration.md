# Pre-registration: memo expected-value threshold for BUY quality

**Registered:** 2026-08-03, before any holdout case in the test window was swept,
generated, or scored. Frozen run used to derive the rule:
`backtest-2026-08-03-566ee500959d` (140 cases, window 2025-06-01..2026-03-31).

## Hypothesis

The research brain's self-declared scenario expected value predicts BUY outcome
quality at the primary horizon. Restricting buys to high-EV memos turns the BUY
pool from utility-negative to utility-positive.

## Frozen rule

For each accepted BUY memo, compute
`EV = Σ scenario.probability × scenario.return_pct` over its `scenarios` block.

- **HIGH-EV** ⇔ `EV >= 0.335` (the top-quartile boundary of the 98 clean
  training buys; quartile boundaries 0.18 / 0.25 / 0.335).
- The threshold is frozen at 0.335 and may not be re-fit on holdout data.

## Holdout

- Window: 2026-02-01 .. 2026-04-30, reconstruction source, spacing 3,
  target 60 cases (all 63-session outcomes mature by 2026-08-03).
- Exclusions from the primary comparison: the 6 pre-existing training cases in
  2026-02/03 (they informed the training run), controls, and non-BUY decisions.
- Cases in this window did not exist in the corpus when the rule was frozen
  (except the 6 excluded above).

## Primary endpoint

Mean 63-session utility of HIGH-EV holdout buys vs sub-threshold holdout buys.

- **Support:** HIGH-EV mean utility > sub-threshold mean utility AND
  HIGH-EV mean utility > 0.
- **Refute:** HIGH-EV mean utility ≤ sub-threshold mean utility.
- **Indeterminate:** HIGH-EV beats sub-threshold but is ≤ 0, or HIGH-EV n < 8.

## Secondary (descriptive only, no support/refute claim)

- Hit rates for both groups at 63 sessions.
- Full-replay mean return for both groups (training showed a *reversal* here:
  low-EV buys replayed better; record whether it recurs).
- Policy view: portfolio mean utility of "actual decisions" vs "BUY only if
  HIGH-EV, else PASS-to-benchmark" on holdout.

## Known caveats, declared up front

- The rule came from one split plus quartile inspection on training data (some
  multiple-comparisons exposure; this holdout test is the control for it).
- Training-window regime (2025-06..2026-03) may differ from 2026-02..04.
- Expected HIGH-EV holdout n is small (~5-15); indeterminate is a likely and
  acceptable outcome — it schedules a retest when the live-import cases mature
  in 2026-10, not a re-fit.
