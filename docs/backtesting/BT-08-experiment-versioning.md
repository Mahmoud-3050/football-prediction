# BT-08 — Experiment Management and Versioning

Status of this document: laboratory rules under [BT-00](BT-00-decision-addendum.md) L9. Creating an experiment record does not approve the parameters inside it. D1–D10 stay open until a walk-forward result closes them in the prediction decision register.

## What an experiment is

An experiment is a frozen question: one dataset version, one league, one date range, one cutoff policy, one snapshot policy, one Gemini configuration, one Poisson configuration, one evaluation methodology, and one execution policy. The stored predictions and evaluations are the answer to that question. A new question is a new experiment.

The comparison experiment is the first intended pair: blind Gemini on the P0 snapshot, and the Poisson comparison configuration in [BT-03](BT-03-poisson-specification.md), with an explicit `k` written on the record. That `k` is the value under test. It is not a default hidden in the engine.

## Lifecycle

- **Draft.** The operator is still choosing versions. No predictions are accepted.
- **Locked.** The configuration tuple is frozen. Jobs may run. Locking writes the creation time and the operator identity if one exists. P0 has a single operator role.
- **Running.** At least one job is active.
- **Paused.** Jobs are paused. Locked configuration stays locked.
- **Completed.** The operator marks the intended match list finished, or every selected match is in a terminal engine state. Evaluation may still be pending where results are missing.
- **Superseded.** A later experiment cites this one as its parent. Superseding does not delete rows and does not change metrics already stored.

Cancelled jobs leave the experiment locked or completed according to whether the operator still wants the rows already stored. Accepted predictions remain part of the experiment.

## What is versioned

| Object | Changes create | Predictions store |
| --- | --- | --- |
| Dataset | A new dataset version when membership, parses, or kickoffs change | Dataset version id |
| Snapshot policy | A new policy version when cutoff rules or feature fences change | Policy version and snapshot hash |
| Gemini configuration | A new version when model, prompt, schema, generation parameters, retrieval flag, or retry policy change | Gemini configuration id |
| Poisson configuration | A new version when any parameter in the BT-03 record changes, including `k` and the tail tolerance | Poisson configuration id |
| Evaluation methodology | A new version when the metric definitions or the probability floor change | Evaluation methodology id on the evaluation, not a rewrite of old evaluations |
| Execution policy | A new version when batch size, windows, or caps change | Job id points at the policy. Predictions do not change if only the cap changes |

Prompt text is stored with the prompt version, not only as a label. Two labels that share a body are still two versions if the operator published them separately. The body hash detects an identical body.

## Reproducibility

A third party with the locked experiment, the dataset version, and the snapshot hashes can see which inputs produced which prediction. Re-running Gemini against a provider can still return different tokens. The stored response remains the evidence. A re-run is a new prediction under a new job, kept beside the original, and it is labeled as a re-run. It does not replace the original. Poisson is deterministic given the snapshot and the parameter record. A Poisson re-run that matches the stored matrix confirms the implementation. A mismatch is a defect record. It does not silently overwrite the stored matrix.

## Comparisons across experiments

The dashboard can show two experiments that share a dataset version and a match list. Differences in model, prompt, or `k` are listed before the metrics. Metrics are not merged into one series. A parent link records intent, such as "same matches, `k` changed from the parent value to this value." The parent link is not proof that the child won. Winning is the evaluation rule in [BT-05](BT-05-evaluation-metrics.md).

Examples that may be created later, each as its own locked experiment:

- Two Gemini model ids on the same snapshots.
- Two prompt versions.
- Two values of `k`.
- Season-to-date against a decay window.
- Independent Poisson against a Dixon-Coles challenger with `ρ` fit only on earlier matches.
- Two half-time share estimators.

None of these examples pre-approve the challenger.

## Audit

The experiment stores who locked it, when, and the configuration hashes. Each prediction stores the experiment id and the configuration ids. Each evaluation stores the methodology id and the result revision id. Job pauses, stops, and rate-limit waits are operational events pointed at the job. They are not written into the probability payload.

Deleting an experiment is not a P0 operation. If a later decision allows retirement, retirement hides the experiment from default lists and retains the rows.

## Relationship to the bot

Bot prompt version `v2` in `predictor.py` is the bot's reasoning schema. It is not a laboratory Gemini configuration. A laboratory experiment does not reference that prompt as its predictor schema. Citing it as an ancestor in a note is allowed. Pointing laboratory predictions at it as if it produced 1X2 probabilities is not.
