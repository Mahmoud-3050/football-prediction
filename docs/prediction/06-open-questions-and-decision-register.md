# Open Questions and Decision Register

Status of this document: every row below is unapproved. The [prediction philosophy](01-mvp-prediction-philosophy.md) is the documented baseline those decisions sit on. Accepting a row requires the evidence named here, from a walk-forward test on one league, as specified in the [evaluation strategy](04-model-evaluation-and-validation.md). No performance number in the other documents is a measured result.

Importance is high when the choice changes `λ` or the primary metric for most matches, medium when it changes a slice of the season or one market, and low when it is a numerical tolerance or a display choice.

## D1. Shrinkage prior strength

- Status: unapproved.
- Importance: high.
- Question: what value of `k` goes in `r_shrunk = (n × r + k × 1) / (n + k)`?
- Alternatives: a small prior, a stronger prior, or `k = 0`.
- Evidence: log loss of the result probabilities, and calibration in the first six matchweeks.
- Next step: run a short grid that includes `k = 0`. The illustration `k = 6` in the mathematical model is not a candidate that has been chosen.
- Recommendation until then: keep the formula, and do not ship a silent default as if it had been tested.

## D2. Sample window

- Status: unapproved.
- Importance: high.
- Question: which prior matches enter the ratios?
- Alternatives: the current season only, several seasons with a season weight, or exponential decay inside one season.
- Evidence: result log loss split by month, so a window that only wins in August is visible.
- Next step: lock season-to-date as the comparison baseline. Treat decay as a challenger. Do not pick a half-life in advance.
- Recommendation until then: season-to-date, unweighted. Last five and last ten are not the default.

## D3. Cross-season prior mean

- Status: unapproved.
- Importance: medium. It matters most in the first month.
- Question: should a thin sample shrink toward 1, or toward last season's shrunk attack and defence?
- Alternatives: prior mean 1.0, or last season's ratios.
- Evidence: log loss in the first month versus the rest of the season.
- Next step: keep the prior mean at 1.0 until last season beats it on later matches.
- Recommendation until then: prior mean 1.0. A promoted club and a club with no stored history use the same rule.

## D4. Opponent-adjusted strengths

- Status: unapproved.
- Importance: high. One-pass ratios are biased by the schedule.
- Question: when should attack and defence be solved together so each match updates both clubs?
- Alternatives: the one-pass ratios in the baseline, or a joint solution.
- Evidence: result log loss, plus a strong-schedule slice against a weak-schedule slice.
- Next step: build the joint solution only as a challenger. Reject it if it wins on the fit sample and not on later matches.
- Recommendation until then: one-pass ratios, with the schedule bias stated in the explanation's limits.

## D5. Dixon-Coles dependence

- Status: unapproved.
- Importance: medium for the score cell and for draws. Lower for a market that only cares about the tail.
- Question: should the 0-0, 1-0, 0-1, and 1-1 cells be reweighted?
- Alternatives: independence, or a `ρ` estimated only on matches before the test window.
- Evidence: log loss of the actual score cell, and draw calibration.
- Next step: leave `ρ` out of the baseline. The current engine's `-0.13` is not an estimate for this league and is not a starting value for the test.
- Recommendation until then: independent Poisson.

## D6. Half-time share

- Status: unapproved.
- Importance: high for the half-time market. It does not change the full-time grid.
- Question: what share links `λ_FT` to `λ_HT`?
- Alternatives: one league share, or a club share shrunk toward the league share. A fixed 0.50 or 0.45 is not an alternative under test until it is entered as an explicit challenger.
- Evidence: log loss of the half-time result.
- Next step: estimate the league share from prior half-time scores, shrink club shares toward it, and compare with a single league share.
- Recommendation until then: do not approve 0.45 or 0.50. Withhold the market when the league has no usable half-time sample.

## D7. Grid tail cutoff

- Status: unapproved as a football parameter. It is a numerical tolerance.
- Importance: low, provided the reported cells are renormalized and the three-way result still sums to 100%.
- Question: when does the grid stop?
- Alternatives: a raw-tail limit of 0.1%, or a fixed maximum goal count.
- Evidence: the omitted raw mass on the rates the model actually produces, not a football backtest.
- Next step: adopt "stop when the raw tail is under 0.1%" once a check confirms the renormalized sums. Do not adopt a fixed cutoff at 6.
- Recommendation until then: the tail rule, not a fixed 6.

## D8. First test league

- Status: unapproved.
- Importance: high for the meaning of every later number. Low for the algebra.
- Question: which one league is the first walk-forward sample?
- Alternatives: any league with stable membership and half-time scores.
- Evidence: the league must support the half-time share and a season long enough to split by month. That is a data check, not a performance claim.
- Next step: choose one league before any parameter search. Do not pool leagues to make the first test larger.
- Recommendation until then: leave the league unnamed.

## D9. Extra displays

- Status: unapproved.
- Importance: low for the model. It changes what a reader sees.
- Question: should both-teams-to-score and the modal half-time score be shown?
- Alternatives: show them, or keep the required outputs only.
- Evidence: none from a scoring rule. They are sums and a mode of grids the model already has. The risk is that a reader treats the half-time mode as the half-time result.
- Next step: decide after the probabilities exist. If the half-time score is shown, show the three half-time result probabilities beside it.
- Recommendation until then: required outputs stay the exact-score list, the full-time result, the half-time result, and the two totals.

## D10. A bound on expected goals

- Status: unapproved, and out of the probability model.
- Importance: low if shrinkage is in place. High if a bound silently changes long-sample rates.
- Question: should `λ` be capped?
- Alternatives: no cap, or a printed product cap.
- Evidence: how often a long sample, not a two-match sample, would cross the cap.
- Next step: do not add a cap in the baseline. Revisit only if a product constraint appears, and print it when it binds.
- Recommendation until then: shrinkage restrains short samples. A large rate from a long sample stays.

## Decisions that are not open

These are part of the documented baseline, not parameters waiting on a test:

- Markets are sums of one score distribution. They are not separate guesses.
- The mode and the most likely result are both reported, and they are allowed to disagree.
- Win, draw, and loss records are not inputs.
- A second form multiplier is not added on top of ratios that already include those matches.
- A fixed home factor is not stacked on rates that already use `league_home_avg` and `league_away_avg`.
- Probability and sample coverage are reported as different statements.
- A prediction is kept and scored later. It is not replaced when the match is played or when a new version is fit.
