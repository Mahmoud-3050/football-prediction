# Mathematical Prediction Model

Status of this document: independent Poisson on a multiplicative rate is the documented baseline. The prior strength `k`, the sample window, the cross-season prior, opponent adjustment, the Dixon-Coles parameter, and the half-time share are recommendations with unapproved numbers. See the [decision register](06-open-questions-and-decision-register.md). Worked numbers below are arithmetic on assumed inputs. They are not measured league rates and they are not model scores.

## Expected goals

All counts use matches already played in the one league.

- `league_home_avg`: home goals per match in the league sample.
- `league_away_avg`: away goals per match in the same sample.
- `attack_home`: home goals per home game for the home club, divided by `league_home_avg`.
- `defence_away`: away goals conceded per away game for the away club, divided by `league_home_avg`. Away goals conceded are the goals home sides scored against them.
- `attack_away`: away goals per away game for the away club, divided by `league_away_avg`.
- `defence_home`: home goals conceded per home game for the home club, divided by `league_away_avg`.

Unshrunk rates:

```text
λ_home = league_home_avg × attack_home × defence_away
λ_away = league_away_avg × attack_away × defence_home
```

Home advantage is the gap between `league_home_avg` and `league_away_avg`. A further constant such as 1.25 on `λ_home` and 0.85 on `λ_away` double-counts that gap when the rates already use home scoring and away scoring. Those factors in the current engine are candidates, not part of this baseline.

Why multiplicative rather than a weighted average of the two clubs' past scores: if the home side scores 20% more than the league at home, and the away side concedes 10% less than a home side usually scores, those two facts scale the league home average. A blend of the two clubs' past scores drops the opponent structure.

Worked example with assumed inputs. League home average 1.55. League away average 1.20. The home club scores 1.86 per home game, so attack is `1.86 / 1.55 = 1.20`. The away club concedes 1.40 per away game, so defensive weakness is `1.40 / 1.55 = 0.90`. Then `λ_home = 1.55 × 1.20 × 0.90 = 1.67`. The away club scores 0.96 per away game, so attack is 0.80. The home club concedes 1.08 per home game, so defensive weakness is 0.90. Then `λ_away = 1.20 × 0.80 × 0.90 = 0.86`.

Normalization check: if every ratio is 1, `λ_home` equals `league_home_avg` and `λ_away` equals `league_away_avg`. Across fixtures, the average of `λ_home` should stay near the league home average. A large drift means the ratios were not centered.

The one-pass ratios already depend on who each club has played. Multiplying by the next opponent's defence is the predictive step this baseline uses. The ratio itself stays biased until attack and defence are solved together. That bias is accepted here and tested later. It is not removed by a second form multiplier.

## Shrinkage

For a ratio `r` from `n` matches, with prior strength `k` and prior mean 1:

```text
r_shrunk = (n × r + k × 1) / (n + k)
```

`k` is unapproved. The formula is the recommendation. The number 6 below is only an illustration.

If `n = 2` and `r = 2.0`, a prior of `k = 6` moves the ratio from 2.0 to `(2 × 2 + 6 × 1) / 8 = 1.25`. If `n = 20` and `r = 2.0`, the same prior leaves it at `(20 × 2 + 6) / 26 = 1.77`. A club with `n = 0` stays at 1, so both `λ` values fall back to the league averages. A promoted club uses that same prior. There is no extra rule.

Shrinkage is what restrains a short sample. A large rate from a long sample is evidence. This baseline does not cap `λ`. A hard cap would be a product constraint and would have to be printed as a constraint.

## Full-time score matrix

Independent Poisson is the baseline because goals are non-negative counts and the mean is the expected-goal rate. Independence is a declared assumption. It is the thing a later low-score correction has to beat.

```text
P(K = k) = exp(-λ) × λ^k / k!
P(home = i, away = j) = P(i | λ_home) × P(j | λ_away)
```

Assumptions and limits:

- Variance equals the mean. Football is often slightly overdispersed. That shows up first in the tails, especially over 3.5.
- The two sides do not depend on each other. Low scores in many leagues cluster a bit more than independence allows. That is the Dixon-Coles observation. It is not in this baseline.
- The grid is finite. Cells outside it are omitted, then the remaining cells are divided by their total so the reported probabilities sum to 1.

The grid runs from 0 until the raw probability outside the grid is under 0.1%. That cutoff is a numerical tolerance, not a football parameter. At assumed rates `λ = 1.5` and `0.9`, cutting at 6 drops about 0.1% of the raw mass. Cutting at 10 drops well under 0.001%. A fixed cutoff at 6 is not the rule.

Dixon-Coles multiplies the 0-0, 1-0, 0-1, and 1-1 cells by a low-score factor and renormalizes. It is the first challenger, not the baseline. It is accepted only if walk-forward log loss on unseen matches beats independent Poisson. A fixed `ρ`, including the current engine's `-0.13`, is not an estimate for this league.

## Markets

Every market is a sum of cells on the normalized full-time grid.

Exact score: rank cells by probability. Report the mode, the next alternatives, and each cell's probability. The mode is one cell.

Full-time result:

```text
P(home win) = sum of cells where home goals > away goals
P(draw)     = sum of cells where home goals = away goals
P(away win) = sum of cells where home goals < away goals
```

Those three sum to 100% on the normalized grid.

Over/under 2.5: over is the sum of cells whose total is at least 3. Under is the sum of cells whose total is at most 2.

Over/under 3.5: over is the sum of cells whose total is at least 4. Under is the sum of cells whose total is at most 3.

Each over/under pair sums to 100% on the normalized grid.

Worked example, independent Poisson, goals 0 through 10, assumed `λ_home = 1.5` and `λ_away = 1.1`:

- Mode: 1-1, about 12.3%. That cell is a draw.
- Result: home 46.4%, draw 25.8%, away 27.8%.
- The most probable score is a draw. The most probable result is a home win.

Assumed `λ_home = 1.5` and `λ_away = 0.9`:

- Over 2.5 about 43.0%, under 2.5 about 57.0%.
- Over 3.5 about 22.1%, under 3.5 about 77.9%.

Exact-score hit rate will stay low while the mode sits near 10–15% in this range. Chasing that hit rate pushes the model toward common scores such as 1-0 and hides bad probabilities.

## Half-time result

The half-time model is linked to the full-time rates. It is not a second, unrelated guess, and it is not half of `λ`.

Estimate the league's first-half share of goals from prior matches that have a half-time score. Shrink a club's own share toward that league share with the same style of prior used for attack and defence. Then:

```text
λ_home_HT = λ_home × share_home
λ_away_HT = λ_away × share_away
```

Apply the same Poisson grid and sum it to home, draw, and away. The required output is those three probabilities. The modal half-time score falls out of the grid. Showing it is an open product choice, not a separate model.

A universal share of 0.50, and the current engine's default of 0.45, are not accepted. With the assumed full-time rates 1.5 and 0.9 and an assumed share of 0.45, the half-time rates are 0.675 and 0.405. The half-time mode is 0-0 at about 34%, and the half-time result is about 37% home, 44% draw, and 19% away. Changing the share to 0.50 moves those result probabilities. The share has to be estimated.

This link cannot represent a club that starts fast and fades until the share itself is club-specific. A club share from a handful of matches will overfit, which is why it is shrunk toward the league share. The league share and the prior on the club share are unapproved until half-time result log loss says otherwise.

## What the explanation is allowed to say

Name the four ratios, the two league averages, the two full-time rates, the half-time shares, and the market sums. Say which ratio raised or lowered a rate relative to the league average. Do not say a side will win because it is in form.
