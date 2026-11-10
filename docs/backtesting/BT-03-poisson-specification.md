# BT-03 — Poisson Statistical Specification

Status of this document: the comparison configuration for the laboratory. It restates the documented baseline in [the mathematical model](../prediction/03-mathematical-prediction-model.md). Open rows stay open. Worked numbers in the mathematical model are arithmetic on assumed inputs. They are not measured league rates, and they are not copied here as results.

The bot's `score_model.py` is running code. This specification does not adopt its `ρ`, its goal cap of 6, its half-time share of 0.45, or its home and away factors of 1.25 and 0.85.

## Role

The Poisson engine estimates two expected-goal rates from prior matches, builds one full-time score distribution, and reads every full-time market from that distribution. Half-time uses a second rate linked by a share, then the same distribution family. The engine does not see Gemini output and does not see the target score. Independence from Gemini is defined in [BT-01](BT-01-independent-engines.md). The match list is the snapshot in [BT-02](BT-02-temporal-reconstruction.md).

## Comparison configuration

Until a walk-forward test closes a row, the configuration used for the first paired comparison is:

- Independent Poisson. Home goals and away goals are independent given the two rates.
- Multiplicative rates below.
- Unweighted season-to-date matches in the one league named on the experiment.
- Prior mean 1 for shrinkage.
- One-pass attack and defence ratios.
- No Dixon-Coles factor.
- No extra home multiplier on top of `league_home_avg` and `league_away_avg`.
- No cap on `λ`.
- A tail tolerance recorded on the experiment. [EXP-PL-001](BT-11-experiment-exp-pl-001.md) records 0.1% (`0.001`). The grid is not fixed at 6 goals.
- Half-time priced when a prior sample can support estimator `HT-SHARE-1` in EXP-PL-001. A match with no usable prior half-time sample withholds that market only. No universal share of 0.45 or 0.50.

The shrinkage formula is in the configuration. A run whose parameter record omits `k` does not start. [EXP-PL-001](BT-11-experiment-exp-pl-001.md) declares `k = 6`. Choosing `k` for an experiment does not close D1.

## Rates

Counts use matches already played in the experiment's league, with kickoff before the cutoff.

- `league_home_avg`: home goals per match in that sample.
- `league_away_avg`: away goals per match in that sample.
- `attack_home`: home goals per home game for the home club, divided by `league_home_avg`.
- `defence_away`: away goals conceded per away game for the away club, divided by `league_home_avg`.
- `attack_away`: away goals per away game for the away club, divided by `league_away_avg`.
- `defence_home`: home goals conceded per home game for the home club, divided by `league_away_avg`.

```text
λ_home = league_home_avg × attack_home_shrunk × defence_away_shrunk
λ_away = league_away_avg × attack_away_shrunk × defence_home_shrunk
```

Home advantage is the gap between the two league averages. A club with no prior matches in the sample keeps the shrunk ratio at the prior mean, so both rates fall back to the league averages when every ratio is 1.

The one-pass ratios depend on who each club has played. That schedule bias stays in the comparison configuration so a later joint model has a baseline to beat. The prediction record states the bias as a limitation. It does not add a second form multiplier to hide it.

## Shrinkage

For a ratio `r` from `n` matches, prior strength `k`, and prior mean 1:

```text
r_shrunk = (n × r + k × 1) / (n + k)
```

`k` is D1 and is unapproved. The prediction stores `n`, `r`, `k`, and `r_shrunk` for each ratio. Coverage is that sample size and the distance shrinkage moved the ratio. Coverage is not a second confidence percentage.

## Full-time distribution

```text
P(K = k) = exp(-λ) × λ^k / k!
P(home = i, away = j) = P(i | λ_home) × P(j | λ_away)
```

The grid starts at 0 goals on each side and extends until the raw probability outside the grid is under the experiment's tail tolerance. Cells inside the grid are then divided by their total so the stored cells sum to 1. The tolerance is D7: a numeric setting recorded on the experiment, with candidate 0.1%. A fixed maximum of 6 is not this rule.

Assumptions stored as limitations on every prediction:

- Variance equals the mean. Extra variation in football shows up first in the tails, including over 3.5.
- Independence given the rates. Low scores in many leagues cluster more than independence allows. That dependence is the Dixon-Coles observation. It is a challenger, not this configuration.
- The finite grid omits a raw tail no larger than the recorded tolerance, then renormalizes.

Dixon-Coles multiplies the 0–0, 1–0, 0–1, and 1–1 cells by a low-score factor and renormalizes. It may appear only as a named challenger experiment. `ρ` for that experiment is estimated on matches before the test window. A copied constant, including the bot's `-0.13`, is not that estimate.

## Markets

Every full-time market is a sum of cells on the normalized grid.

- Exact score: rank cells by probability. Store the mode, the ranked alternatives, and each retained cell's probability.
- Full-time result: home, draw, and away as sums of cells. The most likely result is the largest of the three sums.
- Over/under 2.5: sum of cells with total goals at least 3, and the complement.
- Over/under 3.5: sum of cells with total goals at least 4, and the complement.

The mode and the most likely result may disagree. Both are stored. The mode is not reported as the likely result.

## Half-time

Half-time rates are `λ_FT × share` for each side. The share method is D6 and is unapproved as a universal result. The comparison configuration does not install 0.45 or 0.50. [EXP-PL-001](BT-11-experiment-exp-pl-001.md) prices half-time with estimator `HT-SHARE-1`: a league share from prior half-time goals, and a club share shrunk toward that league share with the experiment's `k`. When the league sample has no usable half-time scores before the cutoff, the engine withholds half-time 1X2 for that match and stores the reason. A later experiment may compare that estimator with a single league share, or with withholding.

The half-time grid uses the same Poisson family and the same tail rule. Half-time 1X2 is the sum of that grid. The modal half-time score is not a required output (D9).

## Parameter record

Every Poisson prediction stores the configuration version and at least:

- `k`, `n` per ratio, prior mean, window rule, and whether ratios are one-pass or joint.
- Dependence mode: `independent` or a Dixon-Coles experiment id.
- Tail tolerance and the highest goal count the grid actually reached.
- Half-time policy: withheld, or the share estimator version and the share used.
- `λ_home`, `λ_away`, and the normalized matrix.
- Limitations: schedule bias, independence, missing half-time, and any ratio that fell back to the prior because `n = 0`.

## Validation before a prediction is accepted

- Both rates are finite and non-negative.
- Declared `k` is present and finite. `k = 0` is allowed only when the experiment explicitly chose it as part of the D1 grid. It is not implied by a blank field.
- Stored cells sum to 1 within an arithmetic tolerance recorded on the experiment.
- Each 1X2 triple sums to 1 within that same tolerance.
- Each over/under pair sums to 1 within that same tolerance.
- No cell was computed from a match at or after the cutoff. The snapshot leakage check is a precondition.

A failed check stores a failed execution and no accepted prediction.

## Challengers

These are experiments, not switches that turn themselves on. Each one changes a single row, as required by the evaluation strategy.

- A grid of `k`, including `k = 0`.
- Decay or a multi-season window, against season-to-date.
- A prior mean taken from last season's shrunk ratios, against prior mean 1.
- A joint attack-defence solution, against one-pass ratios.
- A Dixon-Coles `ρ` estimated before the test window, against independence.
- A half-time share estimated from prior half-time scores, against withholding or against a single league share once that share exists as an explicit estimator.

A stack of these in one run does not identify which change helped. The laboratory may still store such a stack as its own experiment, labeled as a stack, and it does not call that stack the comparison configuration.

## Known limits

The comparison configuration leaves schedule bias in place. It leaves low-score dependence unmodeled. A prior mean of 1 treats a promoted or unfamiliar club as average until a different prior wins on later matches. Overdispersion is unmodeled, so over 3.5 is the market where that limit matters first. None of these sentences is a measured error rate. Measured error rates come from evaluation of stored predictions, in [BT-05](BT-05-evaluation-metrics.md).
