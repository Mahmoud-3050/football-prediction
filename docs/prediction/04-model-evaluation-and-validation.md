# Model Evaluation and Validation Strategy

Status of this document: this is the methodology for judging the baseline in [the mathematical model](03-mathematical-prediction-model.md). No metric below is a measured result. Nothing here specifies a database, an API, or a data provider. Predictions are immutable as a rule: a later result is attached beside the original prediction, and the original probabilities are not overwritten.

## Four separate jobs

Prediction generation prices a match from information available at kickoff.

Prediction evaluation compares that stored price with the score that was later played.

Calibration asks whether a bucket of matches priced near a stated probability produced that outcome about that often.

Model improvement replaces a version only when the challenger wins on matches that were not used to choose its parameters.

Those four jobs stay separate. A fit that looks better on the matches used to estimate `k`, a decay weight, or `ρ` is not an improvement.

## How a test is run

Use one league. Walk forward in time.

For each match, in chronological order:

1. Estimate league averages, ratios, and the half-time share from matches already played.
2. Price the match and store the rates and the market probabilities.
3. Only then read the played score.
4. Score the stored prediction. Do not refit the stored prediction.

This is rolling-origin, or walk-forward, testing. A random split of matches leaks the future, because a November match would help price a September match. A single train-then-test cut is weaker than a walk forward, because one cut can flatter one part of a season. The walk forward is the comparison that counts.

Do not start the scored portion of a season until the league averages exist. Early matches are still priced, with shrinkage doing the work, and they stay in the report so the first month is visible. Dropping them hides the part of the season where the prior matters most.

Season-to-season stability is checked by reporting the same metrics inside each season, not by pooling until one number looks smooth. A model that wins one season and loses the next is not accepted on the pooled number alone.

## Metrics

Log loss is the primary score for the full-time result, for the exact-score cell that occurred, and for the half-time result. It charges a model that put a tiny probability on the outcome that happened. It is the right score when the output is a full distribution.

Brier score is the primary score for over 2.5 and for over 3.5. Each market is a two-way probability. Brier score is the mean squared error of that probability.

Calibration compares a stated probability bucket with the observed frequency in that bucket. A set of matches priced near 60% should finish near 60% of the time. Accuracy does not check that. A model can win the hit rate by picking the favorite and still be badly priced.

Exact-score accuracy is secondary. In the worked examples the mode is often near 10–15%, so the hit rate stays low even when the distribution is honest. Optimizing it pushes the model toward common scores.

Over/under hit rate is secondary for the same reason. Over 3.5 is a tail. Judge it by log loss or Brier score and by calibration, not by how often the favored side wins.

Ranked probability score for the three-way result can wait until result log loss is being reported. It is a useful extra on an ordered outcome. It is not required to choose the first baseline.

Goal prediction error, the absolute gap between `λ` and the goals that were scored, describes the rates. One match is a terrible estimate of a mean, so this error is reported as an average over many matches. It does not replace log loss on the markets.

No target number is set here. A challenger is better when its primary metric improves on the walk-forward matches, including the slice the change was meant to help. A gain only on the fitting sample is rejected.

## How challengers are compared

The baseline is independent Poisson with unweighted season-to-date ratios, prior mean 1, and no Dixon-Coles factor. Each open parameter is a challenger against that baseline.

- The shrinkage prior `k` is a short grid that includes `k = 0`. Judge result log loss, and calibration in the first weeks of a season.
- A decay weight or a multi-season sample is judged on result log loss split by month, so a window that only wins in August is visible.
- A prior mean taken from last season is judged on the first month against the rest of the season.
- A joint attack-defence solution is judged on result log loss and on a strong-schedule slice against a weak-schedule slice. Reject it if it wins on the fit sample and not on later matches.
- A Dixon-Coles `ρ` is estimated only on matches before the test window. Judge log loss of the actual score cell, and draw calibration. A copied constant is not that test.
- A half-time share is judged on half-time result log loss.

One change at a time. A stack of untested changes can win together and still leave the useful piece unknown.

## What evaluation must not do

It must not use the match being priced, or any later match, in the rates. It must not mix another competition into the league averages unless that mixture is itself the decision under test. It must not replace an old prediction when a new model version is fit. The old version stays, and the new version is a new prediction that can be scored beside it.

The current engine's `ρ = -0.13`, half-time share of 0.45, goal cutoff at 6, and home and away factors of 1.25 and 0.85 are not baselines for these tests. They are candidates. They enter a comparison only as explicit challengers.
