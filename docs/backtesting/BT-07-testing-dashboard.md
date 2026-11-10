# BT-07 — Testing Dashboard

Status of this document: behavior of the read-only testing dashboard under [BT-00](BT-00-decision-addendum.md) L8 and L10. It specifies what the operator sees. It does not specify visual design, a component library, or a frontend implementation.

The dashboard reads stored predictions, executions, results, and evaluations. Opening it does not run an engine, rebuild a snapshot, or edit a result.

## Populations and states

Every count and rate shows its denominator and the population name from [BT-05](BT-05-evaluation-metrics.md): engine-available or paired.

Displayed states for a match, an engine, or an evaluation:

- Pending, queued, running, waiting for rate limit, waiting for data.
- Completed.
- Partially completed, when one engine has an accepted prediction and the other has not.
- Failed, paused, cancelled, excluded.
- Awaiting result, awaiting evaluation, evaluation failed.

Correct and incorrect apply only to an accepted selection compared with a known actual outcome. Pending, excluded, failed, and withheld are drawn as their own states. They are not drawn as incorrect.

## Overall view

### Execution

- Matches in the selected dataset version.
- Matches selected by the experiment.
- Matches with an accepted Gemini prediction.
- Matches with an accepted Poisson prediction.
- Matches with both.
- Matches with evaluation completed, pending, or failed.
- Failed executions, split by engine and by failure class.
- Exclusions for insufficient data or a failed leakage check.

These counts are not accuracies. The view labels them as execution counts.

### Performance

For Gemini and for Poisson, on the engine-available population, for each market that was priced:

- Selection accuracy.
- The primary probability metric for that market: multiclass log loss for both 1X2 markets and for exact score when the distribution is complete; Brier score for each over/under market.
- The other probability metric when BT-05 defines it.
- Exact-score hit rate, labeled secondary.
- Sample size.

Half-time shows how many matches were withheld.

### Comparison

The paired block repeats the same metrics on the paired population and names which engine has the better primary metric on that sample. It also shows how many matches were dropped from the pair because only one engine succeeded. It does not show a single cross-market success percentage.

A small sample is labeled with its count. The view does not add a significance badge in P0.

## Match list

Each row shows competition, season, kickoff, home club, away club, actual full-time score when a result exists, Gemini execution status, Poisson execution status, and evaluation status. The actual score column is blank when the result is missing, not a fabricated 0–0.

Default order is kickoff order. The list is the filtered population, not a regenerated prediction set.

## Match detail

### Match

Competition, season, kickoff with timezone, both clubs, cutoff, stage, dataset version, snapshot id, snapshot hash, coverage counts, leakage-check result, and data-quality notes.

### Actual result

Half-time score, full-time score, full-time 1X2, half-time 1X2, total goals, over/under 2.5 side, over/under 3.5 side, result source, verification status, and revision list. When the result is missing, this section says the result is unavailable and the comparison stays pending.

### Gemini

Model id, prompt and schema versions, prediction time, the five market blocks, ranked exact scores with probabilities, analysis text, contamination limitation, validation status, and execution errors if the engine did not produce an accepted prediction. A failed engine shows the failure class and attempt history, not an empty score presented as a pick.

### Poisson

Configuration version, declared `k` and the other parameter fields from [BT-03](BT-03-poisson-specification.md), prediction time, `λ_home`, `λ_away`, the score matrix or a ranked view of it plus a way to see the retained cells, the five market blocks, sample sizes, limitations (schedule bias, independence, tail tolerance, half-time policy), and execution status.

### Market comparison

For each of the five markets, one block:

- Gemini selection and probabilities, or withheld, or unavailable.
- Poisson selection and probabilities, or withheld, or unavailable.
- Actual outcome, or pending.
- Whether each available selection matched, when an actual outcome exists.
- Probability each engine assigned to the actual outcome, when both the distribution and the outcome exist.
- The evaluation metrics stored for that prediction and market.

Half-time withheld by rule sits in this block as withheld, with the reason, and does not show a false incorrect mark.

## Filters

P0 filters, applied before metrics are computed for the view:

- Competition and season.
- Date range.
- Home club and away club.
- Experiment.
- Gemini configuration version and Poisson configuration version.
- Execution status and evaluation status.
- Prediction stage. P0 data has one stage; the filter exists so later stages do not overload the same page without a choice.
- Market, so a view can focus on one market's metrics.
- Selection result: correct, incorrect, or not scored. Not scored includes pending, withheld, excluded, and failed.

Data-quality filter: leakage passed, excluded for thin data, or result unverified. The exact quality flags are those stored on the snapshot and the result.

The view offers an explicit switch between engine-available metrics and paired metrics. The switch does not hide the other population's match count.

## Charts in P0

P0 may use simple comparisons that restate the tables: a bar of primary metric by engine for one market, and a bar of execution outcomes. Each bar's caption includes the sample size. Charts that need more interpretation wait:

- Calibration curves.
- Probability histograms.
- A time series of a metric by month.
- Splits by club.
- Confidence intervals.

Those are specified as P1 and P2 in [BT-09](BT-09-roadmap.md). They read the same stored probabilities. They do not require a new prediction.

## Empty and partial pages

- No experiment selected: the page asks for an experiment. It does not run one.
- Experiment with no matches yet: execution counts at zero and an empty list.
- Job still running: statuses show running or waiting for rate limit. Metrics cover accepted predictions only and say the job is open.
- One engine missing on a match: that column shows the failure or exclusion. The other column shows the stored prediction.
- Result missing: predictions show in full. Comparison shows awaiting result.

## Operator actions the dashboard does not include

The dashboard does not start jobs, retry calls, edit probabilities, or apply result corrections. Job control and result revision are operational functions specified in [BT-04](BT-04-gemini-scheduling.md) and [BT-02](BT-02-temporal-reconstruction.md). They can share a product shell later. They stay separate capabilities so a read cannot be confused with a write.
