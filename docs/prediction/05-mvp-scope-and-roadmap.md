# MVP Scope and Future Roadmap

Status of this document: P0 is the documented baseline. P1 items are the challengers named in the [decision register](06-open-questions-and-decision-register.md). None of the unapproved parameters become approved by being listed here. This document does not specify a database, an API, or a data provider.

A later item is added only when it improves a primary metric on walk-forward matches. A feature that is easy to compute is not therefore an MVP output.

## P0 — Required for the first engine

- One league, chosen later.
- Prior matches only: date, home club, away club, full-time goals, and a stable league label.
- Half-time goals where the half-time market is priced.
- League home-goal average and league away-goal average.
- Venue attack and venue defence ratios, as specified in the [mathematical model](03-mathematical-prediction-model.md).
- Shrinkage of those ratios toward 1. The formula is in scope. The prior strength `k` is not approved.
- Multiplicative `λ_home` and `λ_away`, with home advantage carried by the two league averages.
- An independent Poisson score matrix. The grid runs until the raw tail is under 0.1%, then the cells are renormalized.
- Ranked exact scores, including the mode and alternatives, each with a probability.
- Full-time home, draw, and away as sums of cells.
- Over/under 2.5 and over/under 3.5 as sums of cells.
- Half-time home, draw, and away from `λ_FT × share`, with the share estimated and shrunk. The numeric share is not approved.
- A coverage statement: sample size behind each ratio, and how far shrinkage moved it. This is not a second confidence score.
- An explanation that cites the ratios and the rates.
- Immutable predictions, scored later with log loss and Brier score as specified in the [evaluation strategy](04-model-evaluation-and-validation.md).

## P1 — Next, only if a walk-forward test wins

- A grid of shrinkage priors, including no shrinkage.
- Exponential decay or a longer sample, against season-to-date.
- Last season's shrunk attack and defence as the prior mean, against a prior mean of 1.
- A joint attack-defence solution that removes the schedule bias of one-pass ratios.
- A Dixon-Coles low-score factor estimated before the test window, against independence.
- Club-specific half-time shares beyond the shrunk baseline, if they improve half-time log loss.
- Ranked probability score as an extra report on the three-way result.
- Showing both-teams-to-score, or the modal half-time score. Both fall out of grids that P0 already builds. Showing them is a product choice after the probabilities exist.

## P2 — Later, and still evidence-gated

- More than one league, each with its own averages. A pooled model is a new decision, not a copy of the first league's ratios.
- A schedule-strength report as a standing check, once the joint model has been tried.
- Calibration plots by month and by probability bucket as a regular report.
- A stated bound on `λ` if a product needs one. It would be printed as a constraint. It is not part of the probability model.

## P3 — Out of the first engine

- Player ratings, lineups, injuries, news, sentiment, weather, and live state.
- Betting odds as a feature or as a displayed price.
- Deep learning and ensembles.
- Head-to-head as its own feature.
- A universal home factor of 1.25 and 0.85, a universal half-time share of 0.45 or 0.50, a copied Dixon-Coles `ρ`, and a hard cutoff at 6 goals. These may be logged as challengers. They are not promoted by having been used in the current engine.

## What P0 deliberately leaves biased

One-pass ratios reward a club that has played a weak schedule. Early-season samples are the worst case. The baseline keeps that bias so the joint model has something to beat.

Independence can understate 0-0 and 1-1. The baseline keeps independence so a low-score correction has something to beat.

A prior at 1 pulls a promoted or depleted club toward an average side. That is the honest fallback when no other prior has been shown to help. It is wrong when the club is not average, and the model has no extra signal with which to say so.
