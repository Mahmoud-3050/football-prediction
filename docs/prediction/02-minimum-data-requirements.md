# Minimum Data Requirements

Status of this document: these fields are the documented minimum for the baseline in [the prediction philosophy](01-mvp-prediction-philosophy.md). Which league is first, and how many seasons to keep, stay unapproved. See the [decision register](06-open-questions-and-decision-register.md). This document does not specify a database, an API, or a data provider.

## Required match record

Every prior match used in a rate needs:

- The date the match was played, so later matches can be excluded.
- The home club and the away club, in that order.
- Full-time goals for the home club and the away club.
- A stable league label, so a cup match is not mixed into the league rates.

The half-time market also needs half-time goals for the home club and the away club. If a match has a full-time score and no half-time score, it still counts for full-time rates. It does not count for the half-time share. A missing half-time score is left missing. It is not stored as 0-0.

Nothing else is required for the baseline. Shots, expected goals from an event feed, lineups, injuries, odds, and weather are out of scope.

## What each feature does

Goals scored and goals conceded, split by home and away, are the attack and defence ratios. The recommended sample is the current season up to, and not including, the match being priced. Last five and last ten are competing samples. A short window is noisier. It is not the default.

Win, draw, and loss are not inputs. They discard the score. An explanation may restate them only as a reading of scores that were already used.

Home advantage is the league home-goal average set against the league away-goal average, estimated from the same prior matches. It is not a universal constant.

Opponent strength is not a separate input in the baseline. The simple ratios are biased by who a club has already played. That bias is the reason a later model re-estimates attack and defence together. Adding an opponent multiplier on top of ratios that were not adjusted for schedule would double-count or, if the ratios are raw, leave the schedule bias in place until the joint model exists.

Recency is not a separate input in the baseline. A decay weight changes the same ratios from the inside. It has to beat the unweighted season sample on matches that were not used to pick the weight.

## Coverage

Still produce `λ_home` and `λ_away` when a club has few matches. Shrinkage pulls the ratio toward 1, which is the league average. Say how many matches entered each ratio and how close the shrunk ratio sits to 1.

A club with no prior league matches, including a newly promoted club, keeps the ratio at 1. Both expected-goal rates then fall back to the league averages. There is no separate new-club rule.

Withhold the half-time market when the league sample does not have a usable set of half-time scores. Do not invent the share.

## Leakage

A rate for a match may use only matches that had already been played. A cup match, a friendly, or a match from another league stays out of the league averages unless a later decision explicitly admits it. Using the match being priced, or any later match, makes the evaluation meaningless.

The first test league is still unapproved. It should have stable membership and half-time scores, so the half-time share can be estimated. That requirement is a selection rule, not a choice of league.
