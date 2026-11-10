# BT-11 — Experiment EXP-PL-001

Status of this document: the operator accepted [BT-00](BT-00-decision-addendum.md) through [BT-10](BT-10-gap-analysis.md) as the laboratory baseline, and named the first experiment. The values below are locked for this experiment only. They do not close D1–D10 in [the prediction decision register](../prediction/06-open-questions-and-decision-register.md). They do not change the Telegram bot. Checkpoint C is recorded in [BT-09](BT-09-roadmap.md).

## Operator choices

| Choice | Recorded value |
| --- | --- |
| League | English Premier League. Provider code `PL` where Football-Data.org is the source. One league. No pool. |
| Shrinkage | `k = 6` for this experiment. See below. |
| Tail tolerance | `0.001`, which is 0.1% of raw probability. The operator wrote `0.1`. That is recorded as the documented candidate 0.1%, not as a leftover mass of 0.1 (10%). |
| Half-time | Priced from the start with estimator `HT-SHARE-1`. A match with no usable prior half-time sample withholds that market only. |
| Documents | BT-00 through BT-10 accepted as the laboratory baseline, including the split from the Telegram bot. |
| Gemini model | `gemini-3.5-flash`, confirmed from `.env`. Fallbacks below. |

## Identity

- Experiment id: `EXP-PL-001`.
- Status: locked as a specification. No job has run.
- Parent: none.
- Dataset version: unset until an import is authorized. The experiment applies to every English Premier League season included in that future dataset version. Each season is walked forward on its own. Seasons are not pooled to estimate one club's strength.
- Cutoff: `pre-kickoff`, as in L5.
- Stage: one stage.
- Dependence: independent Poisson. No Dixon-Coles factor.
- Window: unweighted season-to-date in `PL` (D2 recommendation, still open).
- Prior mean for attack and defence: 1 (D3 recommendation, still open).
- Ratios: one-pass (D4 recommendation, still open).
- Cap on `λ`: none (D10 recommendation, still open).
- Both-teams-to-score: not a required market (D9, still open).
- Web retrieval for Gemini: off.
- Evaluation methodology: [BT-05](BT-05-evaluation-metrics.md). Log-loss probability floor: `1e-6`.
- Rounding tolerance: a market's probabilities must sum to 1 within `0.01` absolute.

## Why k = 6

The operator asked for the recommendation. The recommendation for this experiment is the worked example in [the mathematical model](../prediction/03-mathematical-prediction-model.md):

```text
r_shrunk = (n × r + k × 1) / (n + k)
k = 6
```

Six imaginary average matches pull a two-match spike toward the league and leave a twenty-match rate mostly intact. The number is written here, so the run is not using a hidden default. D1 stays open. A later experiment with `k = 0`, and a short grid around 6, is still required before anyone treats 6 as better than no shrinkage. The illustration in the mathematical model is now the declared value of `EXP-PL-001`. It is not a universal constant.

The same `k` is the prior strength on the half-time club share below. The prior mean of that share is the league share, not 1.

## Tail

The full-time grid and the half-time grid each grow from 0 goals until the raw probability outside the grid is under `0.001`. Kept cells are divided by their total. A fixed stop at 6 goals is not this experiment. Before any measured claim, an arithmetic check must show that the renormalized cells sum to 1 on the rates this experiment actually produces. That check is not a football result.

## Half-time estimator HT-SHARE-1

Half-time is part of the first experiment. The share is estimated from prior matches. It is not 0.45 and it is not 0.50.

A prior match is usable for the share when it is in this league, its kickoff is before the cutoff, both half-time scores and both full-time scores are present, and each side's half-time goals are less than or equal to its full-time goals.

On that sample:

```text
league_share = (sum of all half-time goals) / (sum of all full-time goals)
```

For the home club and the away club separately, using that club's goals in the same sample, at either venue:

```text
club_share = (club half-time goals) / (club full-time goals)
share = (n × club_share + k × league_share) / (n + k)
```

`n` is the number of usable prior matches for that club. `k` is 6. If the club has no full-time goals in the sample, its share is `league_share`. Then:

```text
λ_home_HT = λ_home × share_home
λ_away_HT = λ_away × share_away
```

The half-time grid uses these rates, independence, and the same tail tolerance. Half-time 1X2 is the sum of that grid.

Withhold half-time for a match when the league sample has no usable prior match, or when the league's full-time goals in that sample are 0. Full-time markets are still priced. The withheld market stays out of the half-time denominator.

Gemini uses the same rule on the snapshot. The snapshot includes those prior half-time scores. Gemini withholds half-time only when the snapshot has no usable prior half-time match. A priced half-time market from Gemini is still Gemini's own distribution. It is not given Poisson's share or Poisson's rates.

Club-versus-league share remains an open comparison for a later experiment. This experiment runs the shrunk club share, which is the estimator named in the mathematical model, and it does so from the first matchweek that has a prior sample.

## Poisson configuration id

`poisson-exp-pl-001`.

The record stores `k = 6`, prior mean 1, season-to-date, one-pass ratios, independence, tail `0.001`, estimator `HT-SHARE-1`, and no cap on `λ`.

## Gemini model

Confirmed from `.env` on 9 November 2026. The same values are in `.env.example` and in the defaults in `config.py`. Secret keys from `.env` are not copied into this document.

- Preferred model id: `gemini-3.5-flash` (`GEMINI_MODEL`).
- Fallback order, used only after a retryable provider failure: `gemini-3.8-flash`, then `gemini-3.5-flash-lite` (`GEMINI_FALLBACK_MODELS`).
- Configuration id for the preferred model: `gemini-exp-pl-001`.
- Schema version: `gemini-schema-exp-pl-001`.
- Prompt version: `prompt-exp-pl-001`.

An accepted prediction stores the model id that actually answered. A fallback answer stays in the experiment and is marked with that fallback id, so the primary comparison can keep `gemini-3.5-flash` on its own. Changing `GEMINI_MODEL` later is a new configuration version. It does not rewrite these rows.

`DATABASE_PATH` in `.env` is `data/predictions.db`, the bot's operational file. The laboratory store decided at checkpoint C is `data/laboratory.db`.

## Gemini schema `gemini-schema-exp-pl-001`

Prompt version `prompt-exp-pl-001` asks for this JSON object and for nothing else. The model id for this experiment is `gemini-3.5-flash`, as recorded above.

Probabilities are fractions from 0 to 1, not percentages.

```json
{
  "full_time_1x2": {"home": 0.0, "draw": 0.0, "away": 0.0},
  "full_time_most_likely": "home",
  "exact_scores": [{"home_goals": 0, "away_goals": 0, "probability": 0.0}],
  "exact_score_most_likely": {"home_goals": 0, "away_goals": 0},
  "half_time_1x2": {"home": 0.0, "draw": 0.0, "away": 0.0},
  "half_time_most_likely": "draw",
  "half_time_withheld": false,
  "half_time_withheld_reason": "",
  "over_under_2_5": {"over": 0.0, "under": 0.0},
  "over_under_2_5_most_likely": "under",
  "over_under_3_5": {"over": 0.0, "under": 0.0},
  "over_under_3_5_most_likely": "under",
  "analysis": ""
}
```

Validation for this schema version:

- `exact_scores` contains every pair with home goals and away goals each from 0 through 10 inclusive. Further pairs are allowed. Listed probabilities are non-negative and sum to 1 within `0.01`. A score that is absent from the list has probability 0.
- Each 1X2 triple and each over/under pair is non-negative and sums to 1 within `0.01`.
- `full_time_most_likely` is `home`, `draw`, or `away`, and it is the argmax. A tie breaks in the order home, then draw, then away.
- `exact_score_most_likely` is the listed cell with the highest probability. A tie breaks toward the lower total, then the lower home-goal count.
- Over/under labels are `over` or `under` and match the larger probability. A tie breaks toward `under`.
- When `half_time_withheld` is false, the half-time triple and label follow the same rules as full time. `half_time_withheld_reason` is empty.
- When `half_time_withheld` is true, the half-time probabilities are ignored, the reason is non-empty, and the snapshot must contain no usable prior half-time match. The reverse also fails validation: a usable prior sample requires `half_time_withheld` false.
- `analysis` is a non-empty string. It does not supply numbers the structured fields lack.
- The payload contains no target score and no Poisson field.

This rectangle through 10 goals is the Gemini support for schema v1. Poisson still grows its own grid until the 0.1% tail. Evaluation uses each engine's stored distribution. An actual score outside Gemini's list is scored with the probability floor.

The prompt text that surrounds this schema is part of `prompt-exp-pl-001` and is not reproduced here as a finished prompt. The schema fields and the validation rules are the locked contract. Wording changes that keep this contract are still a new prompt version when the body changes.

## What this lock does not do

- The import command `python -m laboratory import-pl` can load Football-Data seasons into the laboratory store. Loading them does not close D8.
- It does not change `/predict`.
- It does not mark `k = 6`, the Premier League, the 0.1% tail, or `HT-SHARE-1` as winners. They are the settings of one experiment.
- It pins `gemini-3.5-flash` from `.env`. The bot's API keys stay out of this document.
