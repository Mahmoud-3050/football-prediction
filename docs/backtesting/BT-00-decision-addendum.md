# BT-00 — Decision Addendum

Status of this document: closed rows apply to the historical testing laboratory only. They do not amend [docs/prediction/01](../prediction/01-mvp-prediction-philosophy.md) through [06](../prediction/06-open-questions-and-decision-register.md). They do not approve D1–D10. They do not change the Telegram bot. They do not select a database or authorize implementation.

The discovery note that preceded this addendum is evidence of the review. It is not itself an approval of model parameters.

## Source of truth

The operator confirmed this split:

- The Telegram bot remains a separate operational application.
- The historical testing platform is a new logical architecture.
- The only modeling baseline to preserve is the prediction series.
- BRD, ERD, and DA-01 through DA-12C are not in this workspace. This series does not invent them and does not assign DA identifiers.

Running code in `score_model.py` and `predictor.py` is an operational implementation. Where it disagrees with the prediction series, the laboratory follows the prediction series.

## How a row is labeled

- **Closed lab decision.** Binding for laboratory documents BT-01 through BT-11.
- **Documented baseline.** Already stated in the prediction series and left unchanged.
- **Open.** Still waiting on a walk-forward result or an explicit later decision. D1–D10 stay in this class.
- **Running code.** Describes the bot. It is not a laboratory input.

## Closed lab decisions

### L1. Corpus

Closed lab decision.

The prediction series is the modeling authority for the laboratory. The operational architecture describes the bot and is not a second model specification. Missing DA documents stay missing. The gap register in [BT-10](BT-10-gap-analysis.md) compares the laboratory to the prediction series and to the bot's stored run, snapshot, and version. It does not pretend to extend DA-12C.

### L2. Boundary

Closed lab decision.

The laboratory replays historical matches and stores engine outputs for later evaluation. The bot continues to answer on-demand Telegram commands. Laboratory work does not change `/predict`, does not copy the bot's Dixon-Coles constants into the Poisson engine, and does not promote a laboratory winner into the bot.

### L3. Independence

Closed lab decision.

One pre-match snapshot is built for a match and a cutoff. Gemini and Poisson both become eligible from that snapshot. Gemini's request excludes Poisson rates, Poisson probabilities, Poisson scores, and the target match result. Poisson's calculation excludes Gemini text and Gemini probabilities. Neither engine's output is an input to the other. Each engine has its own execution record, configuration version, and stored prediction. There is no ensemble in this laboratory.

### L4. Poisson lock

Closed lab decision, limited to the shape of the engine.

The laboratory Poisson engine is independent Poisson on the multiplicative rates in [the mathematical model](../prediction/03-mathematical-prediction-model.md). Markets are sums of one score grid. The engine does not port `RHO`, `MAX_GOALS = 6`, `DEFAULT_HALF_TIME_SHARE`, `HOME_FACTOR`, or `AWAY_FACTOR` from `score_model.py`.

D1, D2, D3, D4, D5, D6, D8, D9, and D10 remain open. A laboratory run records every parameter it used. Recording a value tests that value. It does not approve it. The comparison configuration is the one named in [the evaluation strategy](../prediction/04-model-evaluation-and-validation.md): independent Poisson, unweighted season-to-date ratios, prior mean 1, and no Dixon-Coles factor. Shrinkage uses the documented formula. The prior strength `k` has no approved number, so a run without an explicit declared `k` does not start. The illustration `k = 6` is not a silent default.

D7 is a numeric tolerance, not a football parameter. The laboratory grid grows until the omitted raw tail is under the tolerance recorded on the experiment. A fixed cutoff at 6 is not the rule. The candidate tolerance cited in the prediction roadmap is 0.1%. That candidate stays a candidate until an experiment records it and a numeric check confirms the renormalized cells sum to 1. The check is arithmetic, not a football backtest.

### L5. P0 cutoff

Closed lab decision.

The first laboratory stage is one cutoff: information available strictly before kickoff, using matches completed before that cutoff. Stages at 48 hours, 24 hours, official lineups, and final pre-kickoff as separate clocks are later experiments. Lineups stay outside the first engine, as already stated in the prediction roadmap.

### L6. Feature fence

Closed lab decision.

The shared snapshot carries competition, season, both clubs, kickoff, and prior completed matches in that same league: date, home club, away club, full-time goals, and half-time goals when both half-time scores exist. It carries sample size and missingness. It does not carry the target match result, either engine's output, head-to-head as its own feature, a second form multiplier, lineups, odds, news, weather, or live state.

Poisson reads goals, venue, and league membership from that snapshot. Gemini reads the same snapshot and may receive a coverage statement. Gemini does not receive expected goals computed by Poisson.

### L7. Immutability and evaluation order

Closed lab decision.

A prediction payload is written once. A different model, prompt, statistical configuration, parameter set, snapshot, or stage creates a new prediction record. Evaluation attaches beside the prediction and does not edit probabilities, selections, or analysis. A correction to an actual result appends a result revision and a new evaluation, and it retains the previous evaluation.

Job and execution status may move from pending to completed or failed. That status is operational. It is not a license to rewrite a stored prediction. The bot's in-place update of a prediction-run row is running code. The laboratory does not copy that pattern onto prediction values.

Actual results become available to evaluation only after the prediction that will be scored has been stored. A missing result leaves evaluation pending. It does not delete the prediction.

### L8. Metric card

Closed lab decision.

Each required market reports selection accuracy, log loss, and Brier score according to the roles in [BT-05](BT-05-evaluation-metrics.md) and the prediction evaluation strategy. Exact-score hit rate is secondary. The dashboard shows two populations: each engine on the matches it completed, and the paired set where both engines have a valid prediction. Every displayed rate names its denominator.

Over/under 2.5 is one binary market. Over/under 3.5 is another. Each market has one selection accuracy. Separate "over accuracy" and "under accuracy" headlines are not added, because they split one selection into two rates that share the same matches. The probability of the observed side is still stored.

P0 has no weighted composite score across markets.

### L9. Experiment identity

Closed lab decision.

A stored prediction is evidence only inside an experiment that records dataset version, league, date range, cutoff policy, snapshot policy, Gemini model and prompt versions, Poisson configuration version, parameter set, evaluation methodology, and execution settings. Two runs that differ in any of those are two experiments. Older predictions stay.

### L10. Dashboard cut

Closed lab decision.

P0 dashboard surfaces are the overall comparison, the match list, and the match detail page, with the filters named in [BT-07](BT-07-testing-dashboard.md). Calibration curves, confidence intervals, significance tests, and by-team charts wait. Opening a dashboard page reads stored records. It does not generate predictions and it does not change results.

## Open parameters carried forward

These remain unapproved. [BT-03](BT-03-poisson-specification.md) repeats them so a later experiment cannot treat this addendum as a fit.

| Id | Subject | Laboratory rule until a walk-forward test closes it |
| --- | --- | --- |
| D1 | Shrinkage `k` | Formula stands. No numeric default. A run names its `k` or does not start. |
| D2 | Sample window | Comparison configuration uses unweighted season-to-date. Decay and last-five are challengers, not the default. |
| D3 | Cross-season prior | Comparison configuration uses prior mean 1. |
| D4 | Opponent adjustment | Comparison configuration uses one-pass ratios and states the schedule bias. |
| D5 | Dixon-Coles | Out of the comparison configuration. A later experiment may estimate `ρ` on earlier matches only. |
| D6 | Half-time share | No universal 0.45 or 0.50. Withhold the half-time market when the prior half-time sample is unusable. |
| D7 | Tail tolerance | Numeric tolerance recorded on the experiment. Candidate 0.1%. Not a fixed 6. |
| D8 | First league | Named on the experiment. This addendum does not choose the league. |
| D9 | Extra displays | Required laboratory markets are the five in L8. Both-teams-to-score is not required. |
| D10 | Cap on `λ` | No cap in the comparison configuration. |

## Conflicts left visible

- **Gemini's job differs by system.** In the bot, Gemini writes reasoning for a score the statistical engine already chose, and the prompt includes that score. In the laboratory, Gemini is an independent predictor and receives neither that score nor Poisson's rates. L2 keeps both behaviors in their own system.
- **Rate formula.** The bot uses scored-per-game times conceded-per-game divided by one league rate, with 1.25 and 0.85 on a season-table fallback. The laboratory uses the multiplicative home and away averages from the mathematical model. L4 records that split.
- **Low-score correction and grid.** The bot uses `ρ = -0.13` and goals 0 through 6. The laboratory comparison configuration uses independence and a recorded tail tolerance. L4 records that split.
- **Competitions.** The bot requests several leagues on demand. The laboratory dataset may store more than one competition. The first paired experiment scores one league, named on the experiment. Pooling leagues is not authorized here.
- **Storage.** The bot stores runs in SQLite and updates run status in place. That is operational storage. It is not the laboratory's logical model and it is not a database selection for the laboratory.

## Document map

| Document | Subject |
| --- | --- |
| [BT-01](BT-01-independent-engines.md) | Engine boundaries and independence. |
| [BT-02](BT-02-temporal-reconstruction.md) | Snapshots, cutoff, leakage. |
| [BT-03](BT-03-poisson-specification.md) | Poisson specification and open parameters. |
| [BT-04](BT-04-gemini-scheduling.md) | Gemini requests, schema, sequential scheduling. |
| [BT-05](BT-05-evaluation-metrics.md) | Metrics and paired comparison. |
| [BT-06](BT-06-backtesting-requirements.md) | Business requirements and acceptance. |
| [BT-07](BT-07-testing-dashboard.md) | Dashboard behavior. |
| [BT-08](BT-08-experiment-versioning.md) | Experiments and versions. |
| [BT-09](BT-09-roadmap.md) | P0–P3 and readiness. |
| [BT-10](BT-10-gap-analysis.md) | Logical gaps against the current workspace. |
| [BT-11](BT-11-experiment-exp-pl-001.md) | First locked experiment, EXP-PL-001. Values there do not close D1–D10. |
