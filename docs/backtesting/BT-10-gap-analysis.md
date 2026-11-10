# BT-10 — Logical Gap Analysis

Status of this document: a gap register for the laboratory against this workspace. [BT-00](BT-00-decision-addendum.md) L1 is binding. DA-01 through DA-12C, a BRD, and a core ERD are not in the workspace. This register does not invent their identifiers, and it does not select tables, SQL, or a database product.

The bot's stored run, snapshot, and version, described in [the operational architecture](../architecture.md), are an on-demand trace. They are not the laboratory model. The prediction series specifies evaluation behavior and refuses a physical design. The gaps below are the distance between that material and the laboratory in BT-01 through BT-09.

## How to read a gap

- **Covered.** The workspace already states the rule. The laboratory repeats it.
- **Partial.** A related idea exists and does not meet the laboratory rule.
- **Missing.** The workspace has no logical object for the requirement.
- **Conflict.** The bot and the laboratory both exist and must stay distinct. The conflict is recorded. It is not "fixed" by editing the bot in this document.

## Coverage

| Topic | Workspace | Laboratory gap |
| --- | --- | --- |
| Independent Poisson markets from one grid | Covered in prediction docs 01, 03, and 05 | None for the Poisson shape. Parameters D1–D10 remain open. |
| Walk-forward, log loss, Brier, immutability of probabilities | Covered in prediction doc 04 | Missing an evaluation record that can store those metrics. The rule exists. The logical object does not. |
| Two independent engines | Missing. The bot uses one statistical score and a Gemini commentary that sees that score. | New. Specified in BT-01 and BT-04. Conflict with the bot is intentional under L2. |
| Pre-kickoff snapshot that excludes the target score | Partial. Doc 04 states the rule. The bot snapshot is the fetched payload, which includes scores of finished matches and is built at request time for a future fixture. | New snapshot object with a cutoff and a leakage check. |
| Historical dataset, seasons, raw versus parsed | Missing | New. |
| Sequential multi-match Gemini jobs, quotas, pause and resume | Partial. The bot retries 429 and 503 for one live request. | New job, batch, and rate-limit event. |
| Experiment identity | Missing | New. |
| Dashboard | Missing | New read model. Not a new source of truth. |
| Canonical identity and provider mapping | Missing. The bot stores provider ids. | New logical mapping for the dataset. Not a claim that a DA identity model was approved elsewhere. |
| DA-10 evaluation model | Absent from the workspace | Cannot confirm that it supports two engines. The laboratory evaluation object is specified here instead. |

## Logical objects

Each object is a laboratory concept. Names are for the specification. They are not table names.

### DatasetVersion

Purpose: a frozen set of imported matches and parses.

Attributes: dataset id, version, competition list, import time, source note.

Relationships: one version has many match parses. A later version may supersede an earlier one. Predictions point at the version they used.

Cardinality: a dataset has one or more versions. A version has many matches.

Lifecycle: published, then immutable. A correction is a new version.

Uniqueness: dataset id plus version.

Temporal: the version does not mean "as of kickoff." Kickoff filtering is the snapshot's job.

Immutability: a published version's membership and parse pointers do not change.

Lineage: points at raw payloads and parses.

Audit: who published the version and when.

### RawPayload and Parse

Purpose: keep the provider body separate from the fields the laboratory believes.

Attributes: provider name, provider match id, fetched-at, body hash, parse version, parsed clubs, kickoff, scores, missingness, conflict notes.

Relationships: one raw payload has one or more parses. A match identity has many payloads over time.

Cardinality: many payloads may refer to one internal match. One payload has many parses only across corrections. The accepted parse for a dataset version is one.

Lifecycle: append a parse. Do not edit a previous parse body.

Uniqueness: payload hash plus parse version.

Temporal: `fetched-at` is when the body was obtained, not the kickoff.

Immutability: body and parse results are append-only.

Lineage: dataset version names the parse it accepts.

Audit: parser version on each parse.

### InternalMatch, Club, Competition, Season

Purpose: stable identities inside a dataset so two provider ids can refer to one club.

Attributes: internal id, display name, season bounds, competition membership.

Relationships: a match has one competition, one season, one home club, one away club. A club has many provider mappings. A match has at most one current result head, plus many revisions.

Cardinality: many matches per club. Many mappings per club.

Lifecycle: an identity is minted for a dataset and reused by later dataset versions when the operator says it is the same club. Merging two identities is a new dataset version, not an in-place rewrite of old snapshots.

Uniqueness: internal id. Provider id is unique per provider, not globally.

Temporal: season bounds limit which matches may enter a season-to-date window.

Immutability: renaming a display label may be a new version. Historical snapshots store the ids they used.

Lineage: mappings point at provider ids.

Audit: merge or split recorded on the dataset version that introduced it.

### PreMatchSnapshot

Purpose: the only engine input for a match and a cutoff.

Attributes: snapshot id, match id, cutoff instant, stage, dataset version, prior match id list, coverage counts, leakage result, payload hash. The payload excludes the target score and both engines' outputs.

Relationships: many snapshots per match, one per cutoff and policy version. Many predictions may reference one snapshot. A snapshot references many prior matches and references no result revision as an input.

Cardinality: one snapshot per match, stage, policy version, and dataset version.

Lifecycle: built, checked, then immutable. A failed leakage check stores a rejected snapshot and no engine call.

Uniqueness: match plus stage plus policy plus dataset version plus hash.

Temporal: every prior match kickoff is before the cutoff. L5 limits P0 to the pre-kickoff stage.

Immutability: payload and hash do not change.

Lineage: dataset version, parse versions of the prior matches, policy version.

Audit: check time and check outcome.

### Experiment and Configuration versions

Purpose: the frozen question in [BT-08](BT-08-experiment-versioning.md).

Attributes: experiment id, status, dataset version, league, range, cutoff policy, Gemini configuration id, Poisson configuration id, evaluation methodology id, execution policy id, parent experiment id when present.

Relationships: one experiment has many jobs and many predictions. Configurations are shared only when the hash is identical; sharing is optional. A prediction references one experiment.

Cardinality: one Gemini configuration and one Poisson configuration per experiment.

Lifecycle: draft, locked, running, paused, completed, superseded.

Uniqueness: experiment id. Configuration identity is the content hash.

Temporal: the range is a filter on kickoff. It does not let the snapshot see future matches inside the range.

Immutability: locked configuration fields do not change. Status may change.

Lineage: parent experiment, configuration hashes, prompt body hash.

Audit: lock time.

### Job, Batch, MatchExecution, EngineExecution

Purpose: operational progress for one locked experiment.

Attributes: job status, batch size, window limits, match id, engine id, attempt index, status, timestamps, error class.

Relationships: a job has many match executions. A match execution has up to two engine executions, one Gemini and one Poisson. An engine execution has many attempts. A successful attempt has one accepted prediction.

Cardinality: many jobs per experiment. One accepted prediction per engine, experiment, and snapshot. Further successes are additional predictions labeled as re-runs, not replacements.

Lifecycle: the states in [BT-07](BT-07-testing-dashboard.md). Terminal failure of one engine leaves the other intact.

Uniqueness: job id. Accepted prediction uniqueness as above.

Temporal: the scheduler walks match executions in kickoff order for snapshot building. Poisson may compute during a Gemini wait. Gemini attempts for one job do not overlap.

Immutability: attempt records are append-only. Job status changes. Prediction payloads do not.

Lineage: job points at the execution policy version.

Audit: pause, resume, stop, and rate-limit waits.

### GeminiInvocation and PoissonComputation

Purpose: engine-specific traces.

Gemini attributes: model id, version if returned, prompt versions, schema version, input hash, times, duration, generation parameters, token counts, raw body if retained, validation result.

Poisson attributes: the parameter record in BT-03, rates, matrix, tail reached, validation result.

Relationships: each is an attempt under an engine execution. The accepted attempt points at one prediction.

Cardinality: many attempts per execution. One accepted prediction per successful attempt that passed validation. Invalid attempts have zero accepted predictions.

Lifecycle: started, succeeded, failed.

Uniqueness: attempt id.

Temporal: request time is when the call ran, which is after the cutoff instant of the match and is not the cutoff.

Immutability: traces are append-only.

Lineage: snapshot hash and configuration hash.

Audit: the trace is the audit.

### Prediction

Purpose: immutable probabilities and analysis for one engine.

Attributes: prediction id, engine, market probabilities, exact-score distribution, selections, analysis or Poisson limitations, snapshot id, configuration ids, contamination flag for Gemini.

Relationships: belongs to one experiment, one snapshot, one engine configuration. Has many evaluations over time. Does not reference the other engine's prediction as an input. May record a sibling id for navigation after both exist. The sibling id is not an input feature.

Cardinality: many predictions per match across experiments. One accepted body per attempt.

Lifecycle: accepted, then permanent. Withheld markets are part of the body, not a later edit.

Uniqueness: prediction id. Also unique on experiment, snapshot, engine, configuration, and attempt for the first acceptance.

Temporal: the cutoff on the snapshot is the information time. The prediction timestamp is the computation time.

Immutability: probabilities, selections, and analysis do not change.

Lineage: snapshot hash, configuration hashes, input hash.

Audit: prediction id is the handle used by evaluation.

### ActualResult and ResultRevision

Purpose: played scores outside the engine input path.

Attributes: full-time goals, half-time goals, derived labels, source, verification, revision index, reason for correction.

Relationships: many revisions per match. An evaluation points at one revision. Snapshots do not point at revisions.

Cardinality: one head revision used for new evaluations, plus history.

Lifecycle: unverified, verified, corrected by appending a revision.

Uniqueness: match id plus revision index.

Temporal: the score becomes readable to evaluation after the prediction exists. The kickoff is when the score happened, not when it was imported.

Immutability: a revision body does not change. A correction is a new index.

Lineage: provider payload that supplied the score.

Audit: who recorded the correction and why, when an operator identity exists.

### Evaluation and MetricResult

Purpose: score a prediction against one result revision.

Attributes: metric name, market, value, population tags, sample membership, methodology id.

Relationships: many evaluations per prediction, one per result revision and methodology. Many metric rows per evaluation.

Cardinality: one evaluation per prediction, revision, and methodology.

Lifecycle: pending, completed, failed. A new revision creates a new evaluation.

Uniqueness: prediction plus revision plus methodology.

Temporal: evaluated-at is after both the prediction timestamp and the result import.

Immutability: a completed evaluation's numbers do not change. A methodology change writes a new evaluation.

Lineage: prediction id, revision id, methodology hash.

Audit: failure reason on a failed evaluation.

### RateLimitEvent and ExecutionError

Purpose: explain waits and failures without touching predictions.

Attributes: time, engine, class, provider code, retry-after, attempt id.

Relationships: belong to a job and an attempt.

Cardinality: many events per job.

Lifecycle: appended.

Uniqueness: event id.

Temporal: event time is operational time.

Immutability: append-only.

Lineage: attempt id.

Audit: the event itself.

### Dashboard aggregate

Purpose: a view over the objects above.

It is not a source of truth. A cache, if a later physical design adds one, is invalidated by new predictions or evaluations and never writes back to them. P0 can define the view as a query. This document does not require a stored aggregate.

## Invariants

- An engine payload hash for Gemini contains no Poisson field and no target score.
- An engine payload hash for Poisson contains no Gemini field and no target score.
- Evaluation is the first object allowed to reference both a prediction and a result revision.
- A second engine's failure does not delete the first engine's prediction.
- Job status updates do not update prediction probabilities.
- The bot's prediction-run row is not a laboratory prediction. Laboratory imports do not write bot runs, and bot commands do not write laboratory predictions.

## What this register does not close

- The numeric value of `k`, the league name, the tail tolerance to adopt as a standard, and the Gemini prompt body.
- Whether a future DA corpus, if the operator supplies one, already contains equivalents of these objects. If that corpus appears, this register is reviewed again before any identifier is reused. Until then, these BT objects are the laboratory model.
- Physical keys, indexes, and retention of raw model bodies beyond the rule that a retained body is immutable.

## Readiness

The logical gaps that P0 needs are specified in this document and the notes it cites. The workspace still has no implementation of them. That absence is expected. [BT-09](BT-09-roadmap.md) checkpoint C is the gate for a physical design.
