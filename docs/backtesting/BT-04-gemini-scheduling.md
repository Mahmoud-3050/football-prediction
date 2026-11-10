# BT-04 — Gemini Execution and API Scheduling

Status of this document: laboratory rules for the Gemini engine under [BT-00](BT-00-decision-addendum.md) and [BT-01](BT-01-independent-engines.md). It does not approve a prompt as final, and it does not change the bot's reasoning call.

Gemini in this laboratory produces a structured probability prediction. The bot's Gemini call produces tactical reasoning for a score the bot already chose. Those are different prompts and different configurations.

## Request lifecycle

For one eligible match inside a job:

1. Load the pre-match snapshot and confirm the leakage checks passed.
2. Render the prompt from the experiment's prompt version and that snapshot only.
3. Hash the rendered input. If this experiment already has an accepted prediction for this snapshot and this prompt version, do not call the API again.
4. Send one request. Wait until that request finishes, fails, or hits the timeout recorded on the experiment.
5. Validate the response.
6. On success, store the raw response where retention is allowed, the structured prediction, the analysis, and the trace. The prediction row is then immutable.
7. On a retryable failure, record the attempt, wait according to the retry policy, and try again up to the experiment's retry limit. A later success writes a new prediction. It does not edit a previous accepted prediction.
8. On a terminal failure, store the failed execution and move on. Poisson for the same match is unaffected.

The scheduler runs Gemini requests one at a time for a job. The next request starts after the current request has finished or terminally failed. Parallel Gemini calls are outside the initial workflow.

Poisson may run while Gemini is waiting on the provider, including while the scheduler is in a rate-limit wait, as long as Poisson reads the snapshot and not the in-flight Gemini response. The job still records both engine statuses on the match.

## Structured output

The accepted response is JSON matching the experiment's schema version. The required object contains:

- Full-time 1X2 probabilities and the most likely result.
- A list of exact scores, each with a probability, covering the cells the schema requires, and the most likely exact score.
- Half-time 1X2 probabilities and the most likely half-time result, or an explicit withhold with a reason.
- Over 2.5 and under 2.5 probabilities, and the most likely selection.
- Over 3.5 and under 3.5 probabilities, and the most likely selection.
- Analysis text in its own field.

Validation accepts the response only when:

- The payload parses as the schema.
- Each 1X2 triple is non-negative and sums to 1 within the experiment's rounding tolerance.
- Each over/under pair is non-negative and sums to 1 within that tolerance.
- Exact-score probabilities are non-negative. Whether they must sum to 1 depends on the schema version: a full distribution must sum to 1; a ranked partial list must say it is partial and must not be treated as a full distribution for multiclass log loss.
- The most likely label equals the argmax of the probabilities in that market.
- Analysis may be empty only if the schema version allows an empty analysis. Empty probabilities are never accepted.
- The text does not replace a missing number. The validator reads numbers from the structured fields.

Schema `gemini-schema-exp-pl-001` in [EXP-PL-001](BT-11-experiment-exp-pl-001.md) is the locked contract for the first experiment. It requires the rectangle of scores from 0 through 10 on each side, plus any higher cells the model adds, and the listed probabilities must sum to 1. A score absent from the list has probability 0. There is no `other` bucket in that schema. Prompt wording around the schema is versioned with the prompt body. This document does not replace that contract.

A response that fails validation is not repaired by the application guessing a number. One repair attempt may ask the model to return schema-valid JSON for the same snapshot. That attempt is a separate call, still sequential, and it is stored in the retry history. If it succeeds, it is a new prediction row marked as the accepted attempt. The invalid body remains in the trace.

## What the prompt may say

The prompt includes the snapshot fields allowed in [BT-02](BT-02-temporal-reconstruction.md), the schema, the rounding tolerance, and the instruction to withhold half-time when half-time history is missing. It states that the model must not invent scores, tables, or injuries that are absent from the snapshot.

The prompt excludes the target result, Poisson output, and any tool instruction that searches the web. Web search and external retrieval stay off for blind historical runs unless a later decision approves them for a named experiment. An experiment that turns retrieval on is not a blind historical run. Its predictions are labeled as retrieval-enabled and are excluded from the blind comparison.

## Historical knowledge contamination

A Gemini model may already encode finished matches from training, even when the prompt hides the score. The laboratory therefore:

- Keeps web search off on blind runs.
- Keeps result records out of the prompt and out of tools attached to the call.
- Stores a contamination limitation on every historical Gemini prediction.
- Describes the run as a historical simulation with residual contamination risk.
- Does not treat agreement with a famous score as evidence that the prompt was blind.

Genuinely prospective matches, priced before kickoff and scored after the match is played, are an additional evaluation method. They are the runs that avoid pretrained knowledge of that match. Prospective evaluation is in the roadmap. It is not required to store a historical simulation, and a historical simulation is not reported as prospective.

## Trace

Each attempt stores:

- Model id as sent to the API, plus any version or snapshot id the provider returned.
- API version when the client library or the response exposes it.
- System prompt version, user prompt version, and schema version. A display name is stored beside the API id when both exist.
- Input payload, snapshot id, and input hash.
- Request time, response time, and duration.
- Generation parameters actually sent, including any thinking or temperature setting. A change in those parameters is a new configuration version.
- Token counts when the provider returns them.
- Provider response metadata needed to explain a 429 or a retry.
- Raw response body when the experiment's retention flag allows it.
- Validation status, error text, and the attempt index.

If the provider only returns a model alias, the trace says the version was unavailable. The alias alone is the model id, and a later change in the provider's alias is why each experiment pins the id it requested and the time of the call.

## Scheduling limits

The experiment records the limits it intends to respect. They are operator limits. They do not guarantee that the provider will accept that volume.

- Maximum matches in one batch.
- Maximum Gemini requests per time window, including a window of 5 minutes.
- Daily request cap.
- Token caps per minute and per day, when the operator sets them.
- Maximum wall-clock duration of a job before it pauses.
- Retry limit, backoff base, and jitter.
- Timeout of a single call.

The operator can pause, resume, and stop a job. Pause finishes the in-flight request or records it as interrupted if the process dies, and it does not start another. Resume continues from matches that have no accepted prediction for this experiment and snapshot. Stop moves the job to cancelled. Accepted predictions remain. Cancelled matches stay unpriced unless a new job includes them.

## Quota and backoff

The scheduler records provider signals as they occur:

- Requests per minute and per day observed by this job.
- Tokens per minute when usage is returned.
- HTTP 429 and quota errors.
- `Retry-After` when present.
- Timeouts and other transient network failures.

On 429 or a provider quota error, the scheduler waits at least as long as `Retry-After` when that header is present. Otherwise it waits with exponential backoff and jitter, bounded by the experiment. It does not start the next match during that wait. Invalid schema and authentication failures are not retried indefinitely. Authentication failure stops the job in a failed state and leaves completed predictions stored.

The operational history visible to the operator includes completed, pending, failed, and retried matches, the match in flight, remaining batch capacity, current wait, token totals for the job, and a count of matches not yet attempted. Estimates of remaining work are counts of remaining matches. They are not a promise of clock time.

## Configuration identity

A Gemini configuration version is the tuple of model id, prompt versions, schema version, generation parameters, retrieval flag, and retry policy. Any change creates a new version. Predictions store the version id. They are not rewritten when a newer prompt is drafted.

The bot's model id and its `tactical_reasoning` schema are a different configuration family. Laboratory documents do not retarget `GEMINI_MODEL` in the bot.
