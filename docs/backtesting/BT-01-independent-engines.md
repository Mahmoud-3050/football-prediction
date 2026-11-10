# BT-01 — Independent Prediction Engines

Status of this document: laboratory architecture under [BT-00](BT-00-decision-addendum.md). L3 is binding. This note does not approve Poisson parameters and does not change the Telegram bot.

The laboratory runs two engines. Gemini estimates probabilities from the pre-match snapshot. Poisson estimates probabilities from the same snapshot using the statistical specification in [BT-03](BT-03-poisson-specification.md). Evaluation, in [BT-05](BT-05-evaluation-metrics.md), scores each stored output against the played match after that output is stored.

## Responsibilities

| Engine | Owns | Does not own |
| --- | --- | --- |
| Snapshot builder | The pre-match view in [BT-02](BT-02-temporal-reconstruction.md). | Probabilities, actual-result scoring. |
| Gemini | One structured prediction and a separate analysis, for one snapshot and one prompt version. | Poisson rates, the other engine's text, scheduling policy beyond the request it was given. |
| Poisson | One score distribution and the markets summed from it, for one snapshot and one parameter set. | Gemini's prompt, Gemini's prose, web search. |
| Orchestrator | Job order, eligibility, and the rule that one engine's failure leaves the other engine's stored output in place. | The numerical contents of either prediction. |
| Evaluation | Metrics beside a stored prediction. | Edits to that prediction. |
| Dashboard | Read models over stored rows. | Execution, refits, and result edits. |

## Shared input

Eligibility is decided once per match, cutoff, and experiment. A match is eligible when the snapshot contains the minimum fields in [BT-02](BT-02-temporal-reconstruction.md) and passes the temporal checks. Both engines receive that eligibility decision and that snapshot identity. They do not receive each other's payloads.

If the snapshot is insufficient, both engines record an exclusion for that match and the reason. An exclusion is not a prediction, and it is not scored as an incorrect selection.

## Gemini boundary

Gemini is called only through the lifecycle in [BT-04](BT-04-gemini-scheduling.md).

Inputs that are allowed:

- Competition, season, kickoff, home club, away club.
- Prior completed matches in that league, already filtered to the cutoff.
- Counts of missing scores and the coverage statement.
- The instruction to return the schema and to abstain on a market the snapshot cannot support.

Inputs that are excluded:

- Full-time and half-time scores of the target match.
- Post-match statistics of the target match.
- Matches that kick off after the cutoff.
- Poisson `λ`, Poisson probabilities, Poisson modal score, and Poisson analysis.
- Tools that can retrieve the web or the result store.
- The bot's tactical-reasoning prompt, which assumes a score already chosen.

The structured probabilities and the analysis are stored as separate fields of one immutable prediction. Prose does not replace a missing probability. Invalid JSON, probabilities that fail the checks in BT-04, and an empty required market are validation failures. A validation failure stores the attempt trace and does not invent a distribution.

## Poisson boundary

Poisson reads prior goals, venue, and league membership from the snapshot. It writes `λ_home`, `λ_away`, the score matrix, and the five market groups. It does not read Gemini's request, response, or probabilities. It does not call an external model.

A Poisson failure, such as a missing declared `k` or a non-finite rate, stores an engine error. It does not block a Gemini prediction that is already valid, and a Gemini failure does not discard a valid Poisson prediction.

## Outputs both engines must be able to store

For each market the engine priced:

- Full-time 1X2: probability of home, draw, and away, plus the most likely result.
- Exact score: the probability of each retained score cell, a ranked list, and the most likely score.
- Half-time 1X2: probability of home, draw, and away, plus the most likely half-time result, or an explicit withhold when [D6](../prediction/06-open-questions-and-decision-register.md) says the sample is unusable.
- Over/under 2.5: probability of over, probability of under, and the most likely selection.
- Over/under 3.5: the same pair of probabilities and the most likely selection.

The most likely selection is the argmax of the stored probabilities. It is not a probability of 100%. A modal exact score may disagree with the most likely 1X2 result. Both are stored. The mode is one cell. The result is a sum of cells.

Gemini may withhold half-time when it reports that the snapshot lacks half-time history. Poisson withholds half-time under the D6 rule. A withheld market is excluded from that market's denominator. It is not an incorrect pick.

## Versioning

Each prediction points at:

- Experiment identity from [BT-08](BT-08-experiment-versioning.md).
- Engine identity: `gemini` or `poisson`.
- Engine configuration version: prompt and model for Gemini, parameter set for Poisson.
- Snapshot identity and snapshot hash.
- Cutoff and stage. P0 stage is the single pre-kickoff cutoff in L5.

A change to any of those creates a new prediction. The previous row stays.

## Execution records

Each engine execution stores status, start and finish timestamps, duration, validation status, and error history. Gemini also stores the request and response trace from BT-04. Poisson stores the parameter set and the rates. These traces are immutable once the execution reaches a terminal success or a terminal failure that will not be retried into the same prediction row. A retry that produces a new valid response writes a new prediction. It does not overwrite a previous valid one.

## Independence checks

A laboratory build is consistent with L3 when all of the following hold:

- The Gemini request hash covers a payload with no Poisson fields and no target score.
- The Poisson input hash covers a payload with no Gemini fields and no target score.
- Both hashes include the same snapshot id when both engines were eligible.
- Stored predictions reference that snapshot id and reference no prediction id of the other engine as an input.
- Evaluation reads both predictions and the actual result, and it is the first component allowed to join those three.

## Error handling

| Situation | Gemini record | Poisson record | Match in the job |
| --- | --- | --- | --- |
| Both succeed | Completed prediction | Completed prediction | Ready for evaluation after persist |
| Gemini succeeds, Poisson fails | Completed prediction | Failed execution, reason stored | Gemini can be evaluated; Poisson cannot |
| Poisson succeeds, Gemini fails | Failed execution, reason stored | Completed prediction | Poisson can be evaluated; Gemini cannot |
| Both fail | Failed execution | Failed execution | No evaluation for either |
| Invalid probabilities | Validation failure | Validation failure, if Poisson produced a bad distribution | That engine stays unevaluated |
| Snapshot insufficient | Excluded | Excluded | Listed as excluded, not incorrect |
| Result missing | Prediction kept | Prediction kept | Evaluation pending |
| Evaluation fails | Prediction kept | Prediction kept | Evaluation failed, retry evaluation only |

## What the bot keeps

The bot's Gemini call remains a reasoning step over a score the bot already computed. That path is specified in [the operational architecture](../architecture.md). Laboratory prompts and bot prompts are different versions. A laboratory prompt version is not installed into the bot by this document.
