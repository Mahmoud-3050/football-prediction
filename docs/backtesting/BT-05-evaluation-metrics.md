# BT-05 — Evaluation and Statistical Comparison

Status of this document: laboratory metrics under [BT-00](BT-00-decision-addendum.md) L7 and L8. It applies the roles in [the evaluation strategy](../prediction/04-model-evaluation-and-validation.md) to two engines. No figure in this note is a measured result.

Evaluation reads a stored prediction and a stored actual result. It writes an evaluation record. It does not change the prediction.

## When evaluation runs

Evaluation of a prediction starts after that prediction is stored and a verified result exists for the same match. The other engine's state does not delay it. A missing result leaves the prediction in `awaiting result`. A failed metric calculation leaves `evaluation failed` and keeps the prediction.

A result revision creates a new evaluation pointed at the new revision. The evaluation pointed at the old revision stays.

## What is scored

The scored object is the probability distribution and the selection derived from it.

- The selection is the most likely outcome stored on the prediction.
- A selection is correct when it equals the actual outcome in that market.
- Pending, excluded, withheld, and failed predictions are outside the accuracy denominator. They are not counted as incorrect.
- Probability metrics use the probability the engine assigned to the outcome that happened. They need the distribution. A selection alone is not enough for log loss or Brier score.

Exact-score log loss requires a full distribution over scores, or a schema-defined remainder bucket that evaluation can score. A ranked list that omits the actual score and has no remainder bucket cannot contribute a finite log loss. That prediction counts for hit rate only when the mode equals the actual score, and the missing log loss is reported as unavailable, not as zero.

## Market metrics

Log loss is the mean of the negative log of the probability assigned to the actual class. Probabilities are clipped only by a numeric floor recorded on the experiment so a stored zero does not make the mean infinite without a trace. The floor is a numerical tolerance. It is stored. It is not a football parameter.

Brier score for a binary market is the mean squared difference between the probability of the named side and the 0/1 outcome of that side. For a three-way market, multiclass Brier score is the mean squared error across the three outcome indicators.

### Full-time 1X2

- Selection accuracy.
- Multiclass log loss. Primary.
- Multiclass Brier score.
- Calibration of the predicted probabilities, when the sample is large enough to show buckets. Bucket charts are P1. P0 still stores the probabilities so the chart can be drawn later.

### Exact score

- Hit rate of the most likely score. Secondary. A honest mode near the low teens of percent will miss often.
- Probability assigned to the actual score.
- Rank of the actual score in the stored ranking, when the actual score is in the list.
- Multiclass log loss when the full distribution is available. Primary for the score cell, matching the evaluation strategy.

### Half-time 1X2

- Selection accuracy.
- Multiclass log loss. Primary.
- Multiclass Brier score.
- Calibration, with the same P1 note as full-time.

Matches where the engine withheld half-time, or where the result has no half-time score, stay out of this denominator. The dashboard shows how many matches were withheld.

### Over/under 2.5 and over/under 3.5

Each line is its own market.

- Selection accuracy. One rate per market.
- Binary log loss.
- Brier score. Primary for these two markets, matching the evaluation strategy.
- Calibration, as a later chart.

The actual total goals decide the side. Over 2.5 is 3 or more goals. Over 3.5 is 4 or more goals. The probability of the side that happened is stored on the evaluation even when the selection was the other side.

## Two populations

Every aggregate names its population.

1. **Engine-available.** Matches where that engine has an accepted prediction for the market. Gemini's denominator and Poisson's denominator may differ. The dashboard prints both counts.
2. **Paired.** Matches where both engines have an accepted prediction for that market on the same experiment, snapshot stage, and result revision. This is the population for a head-to-head statement.

A sentence of the form "Gemini is ahead of Poisson" uses the paired population. A sentence about one engine's level uses the engine-available population and says so. The dashboard shows the count of matches present for only one engine so the gap is visible.

Excluded matches and failed executions appear in the execution summary. They do not silently shrink a denominator.

## Calibration

Calibration groups predictions by the probability they stated and compares that probability with the observed frequency. A set of matches priced near 60% should finish near 60% of the time if the prices are reliable. Accuracy can look strong for an engine that always picks the favorite and still be poorly calibrated. P0 stores the inputs. P1 draws the curves. The laboratory does not claim calibration from accuracy.

## Sample size and stability

P0 reports the number of matches beside every rate. A comparison on a handful of matches is displayed as a small sample. It is not phrased as a settled ranking.

P1 and P2 add, without changing stored predictions:

- Confidence intervals on the metric differences.
- A paired test suited to the metric, declared before looking at the result of that experiment.
- The same metrics split by season and by month.
- A note when one engine's exclusions concentrate in a part of the season, which is selection bias in the engine-available population.

No target accuracy is set. A challenger replaces the comparison configuration only when its primary metric improves on walk-forward matches that were not used to choose its parameters, including the slice the change was meant to help. That rule is the prediction series rule. This laboratory stores the evidence. It does not auto-promote the winner into the bot.

## Chronology

Metrics for an experiment are computed in kickoff order from stored predictions. Refitting `k` or a prompt on the same matches and deleting the old scores is not an evaluation. The old experiment remains, and the new configuration is a new experiment.

Season stability is the metric inside each season. A pooled number across seasons is an extra line. It does not hide a season that went the other way.

## Composite scores

P0 does not publish one success percentage across 1X2, exact score, and both totals. Those markets are different tasks. A later composite would need a written formula, weights, and a decision that approves it. None of that is approved here.

## Goal error

The average absolute gap between `λ` and the goals scored describes Poisson's rates. It is reported as an average over many matches. One match is a poor estimate of a mean. This gap does not replace log loss, and it does not apply to Gemini unless a future schema asks Gemini for expected goals. The current Gemini schema does not.
