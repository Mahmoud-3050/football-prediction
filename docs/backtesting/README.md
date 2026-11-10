# Historical testing laboratory

These notes specify a historical backtesting laboratory. The laboratory compares two independent engines, Gemini and Poisson, on the same pre-match information.

The laboratory is a new logical architecture. It does not replace the Telegram bot in [the operational architecture](../architecture.md). The modeling baseline remains [the prediction series](../prediction/README.md). This series does not choose a database, a cloud platform, or a physical schema, and it does not approve the open parameters D1–D10. The operator accepted BT-00 through BT-10. The first locked experiment is [EXP-PL-001](BT-11-experiment-exp-pl-001.md).

Read [BT-00](BT-00-decision-addendum.md) before the other notes. A row there is closed only for the laboratory. It does not rewrite docs 01–06.

1. [Decision addendum](BT-00-decision-addendum.md)
2. [Independent engines](BT-01-independent-engines.md)
3. [Temporal reconstruction](BT-02-temporal-reconstruction.md)
4. [Poisson specification](BT-03-poisson-specification.md)
5. [Gemini scheduling](BT-04-gemini-scheduling.md)
6. [Evaluation metrics](BT-05-evaluation-metrics.md)
7. [Business requirements](BT-06-backtesting-requirements.md)
8. [Testing dashboard](BT-07-testing-dashboard.md)
9. [Experiment versioning](BT-08-experiment-versioning.md)
10. [Roadmap](BT-09-roadmap.md)
11. [Gap analysis](BT-10-gap-analysis.md)
12. [Experiment EXP-PL-001](BT-11-experiment-exp-pl-001.md)
