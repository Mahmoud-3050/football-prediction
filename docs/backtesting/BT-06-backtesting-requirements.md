# BT-06 — Historical Backtesting Requirements

Status of this document: business requirements for the laboratory. Closed behavior is in [BT-00](BT-00-decision-addendum.md). Priority is in [BT-09](BT-09-roadmap.md). This note does not authorize coding, a database, or a change to the Telegram bot.

## Objective

The laboratory answers a narrow question: on the same historical matches, using only information available before kickoff, how do an independent Gemini predictor and an independent Poisson predictor compare under the metrics in [BT-05](BT-05-evaluation-metrics.md)?

The operator can inspect one match and can inspect the aggregate, including matches one engine missed. The laboratory keeps each prediction reproducible.

## Users and workflows

The operator is the only role specified for P0. Notifications, accounts, and public publishing are out of scope.

### Select a test

The operator creates an experiment: one league, a season or a date range, the pre-kickoff cutoff, a Gemini configuration, and a Poisson configuration that names `k` and the other open fields. The experiment stores that choice before any match is priced.

### Run

The operator starts a job. The job walks matches in kickoff order, builds a snapshot, runs the engines under [BT-04](BT-04-gemini-scheduling.md), and stores outputs. The operator can pause, resume, and stop. The operator can see the match in flight, waits, failures, and how many matches remain.

### Score

After predictions for a match are stored, evaluation may read the actual result. The operator sees pending evaluation when the result is missing, and a new evaluation when a result is corrected.

### Read

The operator opens the dashboard in [BT-07](BT-07-testing-dashboard.md). Reading does not call Gemini and does not change a stored prediction.

## Functional requirements

### Data

- Import historical matches with competition, season, kickoff, both clubs, status, provider identity, and raw payload retention.
- Keep played scores on the result side of the boundary in [BT-02](BT-02-temporal-reconstruction.md).
- Record missing fields and conflicts. Do not replace a missing score with zero.
- Version the dataset. A new import that changes history creates a new dataset version.

### Snapshots

- For each match in an experiment, build one P0 snapshot at the pre-kickoff cutoff.
- Reject snapshots that fail leakage checks.
- Hash the snapshot and keep it addressable by predictions.

### Engines

- Run Gemini and Poisson independently on eligible snapshots.
- Persist a valid output even when the other engine fails.
- Skip a second API call when an accepted prediction already exists for that experiment, snapshot, and configuration.
- Support exclusion when the snapshot is insufficient, without labeling the exclusion incorrect.

### Markets

Store the five groups in [BT-01](BT-01-independent-engines.md): full-time 1X2, exact score, half-time 1X2 or an explicit withhold, over/under 2.5, and over/under 3.5. Store full probabilities, not only the winning label.

### Evaluation

- Score each engine on the metrics in BT-05.
- Keep evaluation off the prediction row's probability fields.
- Support pending, failed, and revised evaluation.

### Dashboard

- Show execution counts, per-engine metrics, and a paired comparison.
- Show one match with both predictions, the actual result, and the market-by-market comparison.
- Filter the population and keep denominators visible.

### Operations

- Sequential Gemini requests.
- Configurable batch size, window limits, daily caps, token caps, timeout, and retries.
- Trace every failure with a class: provider quota, invalid Gemini output, invalid probabilities, Poisson calculation, missing history, temporal check, missing result, evaluation failure, authentication.

## Non-functional requirements

- A prediction payload is immutable after acceptance.
- A job can resume without duplicating an accepted prediction.
- Dashboard reads are free of prediction side effects.
- Blind Gemini runs ship with web retrieval disabled and with the contamination limitation stored.
- The laboratory does not require the bot's process, token, or SQLite file.
- No physical database is selected by these requirements. Logical records are described in [BT-10](BT-10-gap-analysis.md).

## Scenarios

### Both engines finish

A finished league match has prior matches before kickoff. Both engines store valid distributions. Evaluation then attaches log loss, Brier score, and selection correctness per market. The match page shows both and the actual score.

### Gemini is rate-limited

The provider returns 429 with `Retry-After`. The job waits, then continues with the next Gemini request. Poisson predictions already stored for later matches in the walk remain stored. The wait is visible on the job.

### One engine fails validation

Gemini returns probabilities that do not sum to 1. The attempt is stored as invalid. Poisson's accepted prediction remains and can be evaluated on the engine-available population. The paired population excludes this match until Gemini has an accepted prediction.

### Thin history

The first match of a club in the season still receives a Poisson price when `k` is declared, with ratios at the prior mean and the sample size stored. Gemini receives the same thin history and the coverage counts. If the experiment's eligibility rule says the match is below a minimum the operator set, both engines are excluded and the reason is stored. The comparison configuration does not add a minimum beyond the mathematical model's rule that `n = 0` falls back to the prior. An operator minimum is an experiment setting and is visible.

### Result corrected

A provider corrects a full-time score after evaluation. The result revision is appended. A new evaluation is written. The old evaluation and the original probabilities remain available on the match page.

### Historical contamination

The match is from a season the model family may have seen in training. The prediction is stored and evaluated, and the match page shows the contamination limitation. The aggregate does not describe this run as prospective.

## Constraints

- One league per paired experiment until a later decision allows a pool.
- P0 cutoff is pre-kickoff only.
- No ensemble.
- No automatic promotion into the bot.
- D1–D10 stay open. Runs record the values they used.
- Head-to-head, lineups, odds, and news stay out of the P0 snapshot.
- The bot's Dixon-Coles constants stay out of the Poisson comparison configuration.

## Acceptance criteria

The laboratory specification is met when a reviewer can trace a single match from dataset version to snapshot hash, to each engine's configuration version, to an immutable prediction, to a result revision, to an evaluation, and can show that the engine payloads lacked the target score and lacked each other's outputs. Aggregates on the dashboard use the denominators in BT-05. A second experiment with a different `k` or prompt does not change the first experiment's rows.

Implementation readiness is a separate checkpoint in [BT-09](BT-09-roadmap.md). This requirements note does not pass that checkpoint by itself.
