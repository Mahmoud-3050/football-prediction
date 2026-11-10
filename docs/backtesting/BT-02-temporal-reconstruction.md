# BT-02 — Historical Data and Temporal Reconstruction

Status of this document: laboratory data rules under [BT-00](BT-00-decision-addendum.md). L5, L6, and L7 are binding. This note does not choose a provider, a database, or the first league (D8).

The laboratory rebuilds the information that existed before a cutoff. It then asks each engine to price the match from that view. Played scores of the target match stay in a result store that prediction engines do not read.

## Dataset

A historical dataset is a named, versioned collection of matches for one or more competitions. The first paired experiment uses one competition, named on the experiment. Other competitions may sit in the dataset so later experiments can name them. A pooled model across competitions is a later decision, already deferred in the prediction roadmap.

Each match record distinguishes:

- Identity: competition, season, home club, away club, kickoff instant, and the timezone used to interpret that instant.
- Status of the fixture at import: scheduled, finished, or another provider status stored as reported.
- Played scores, held on the result side: half-time goals and full-time goals when the provider supplied them.
- Provenance: provider name, provider match identifier, fetch time, and a hash of the raw payload.
- Quality: which of the minimum fields are present, which are missing, and which sources conflict.

Raw provider payloads and parsed records are different objects. One raw payload yields one parse. A later parse of the same payload, or a corrected payload, appends a new parse. Older parses remain. The snapshot builder reads a chosen parse version. It does not read an overwritten blob.

Clubs and competitions in the laboratory have a stable internal identity for the duration of a dataset version. Provider identifiers attach to that identity as mappings. The bot's direct use of Football-Data.org identifiers is running code. The laboratory mapping is a logical requirement. It is not an implementation of a DA identity model that this workspace does not contain.

## Minimum fields

From [the minimum data note](../prediction/02-minimum-data-requirements.md), a prior match that enters a rate or a Gemini history list has:

- A kickoff instant before the cutoff.
- Home club, away club, and the league label of the experiment.
- Full-time home goals and full-time away goals.

Half-time goals are required only for matches that support the half-time share or the half-time history shown to Gemini. A missing half-time score stays missing. It is not stored as 0–0. A missing full-time score excludes that prior match from the rates.

The target match needs identity, kickoff, competition, season, and both clubs before a snapshot can be built. Its own goals are not inputs.

## Cutoff

P0 uses one stage, `pre-kickoff`. The cutoff instant is the scheduled kickoff. Information with a known time at or after that instant is excluded. The cutoff is stored on the snapshot. It is not assumed to equal "whatever the engine can see today."

Later stages, if an experiment adds them, each get their own cutoff instant and their own snapshot. They do not reuse the pre-kickoff snapshot under a new name.

Examples that are specified as future stages, not as P0 clocks:

- A clock 48 hours before kickoff.
- A clock 24 hours before kickoff.
- A clock when lineups are treated as known.
- A last clock still strictly before kickoff if it differs from scheduled kickoff because of a delay known at that time.

Lineups are outside the first engine. Adding them is a roadmap item, not a P0 snapshot field.

## Chronological replay

Matches in an experiment are ordered by kickoff. For match *t*:

1. Build the snapshot from matches completed before the cutoff of *t*.
2. Run the engines that the job still owes for *t*.
3. Store their predictions.
4. Only then may evaluation read the played score of *t*.
5. Match *t*, once completed and verified, may enter the snapshot of a later match.

A random split of the season is not a laboratory test. A single train-then-test cut is not the comparison that the evaluation strategy accepts. Early matches in a season are still priced. Shrinkage, when the experiment declared a `k`, does the work. Dropping the first month hides the period the prior is there to cover.

Season-to-date means matches in the same competition and season with kickoff before the cutoff. It does not mean the full season table as published after the season ends. A table that includes future matches is leakage.

## Leakage checks

The snapshot builder rejects a snapshot when any of the following are true:

- The target full-time or half-time score appears in the engine payload.
- A prior match has kickoff at or after the cutoff.
- A feature uses a season aggregate that includes matches after the cutoff.
- The payload includes a derived rate that was computed with the target match or a later match.
- Gemini's payload includes Poisson output, or Poisson's payload includes Gemini output.

The check result is stored with the snapshot. A failed check excludes the match for both engines. The engines are not called "to see what they would do."

Derived features are computed inside the engine from the allowed match list, or they are computed by the snapshot builder from that same list and stored with the formula version. Either way, the formula version is part of the experiment. A feature whose formula is undocumented does not enter the snapshot.

## Completeness

The snapshot records:

- Count of prior league matches used for each club, split by home and away where the rate needs venue.
- Whether half-time scores exist on those matches, and how many.
- Fields that were missing and were left empty.
- Conflicts between two provider parses, and which parse the experiment selected.

Insufficient history produces an exclusion or a withheld market, under the engine rules in [BT-01](BT-01-independent-engines.md). The builder does not fill gaps with league averages silently inside the snapshot. Poisson's shrinkage toward the prior mean is a model step with a declared `k`. It is visible on the Poisson prediction as the sample size and the distance the ratio moved. It is not a hidden replacement of missing scores.

## Provenance

Each snapshot stores dataset version, parse version, cutoff, stage, the list of prior match identities, the coverage counts, the leakage-check result, and a hash of the canonical payload. Predictions point at that snapshot id and hash. Rebuilding a snapshot with a different parse or a different cutoff creates a new snapshot. Predictions already pointed at the old hash stay pointed at it.

## Result isolation

Actual results live in a result record: full-time goals, half-time goals when known, the derived 1X2 labels, total goals, source, verification status, and revision history. Prediction components receive a snapshot id. They do not receive a result id. The evaluation component is allowed to read a prediction id and a result id together after the prediction row exists.

A result correction writes a new revision, names the previous revision, and causes a new evaluation. The previous evaluation remains attached to the previous revision. Historical probabilities stay as first written.

## Walk-forward availability

After a match is evaluated, its verified full-time score may be included in later snapshots. Its inclusion is by kickoff order, not by import order. A match imported late, with a kickoff that is already in the past relative to a snapshot already stored, does not rewrite that snapshot. A new experiment, or an explicit new snapshot version, is required before later predictions see the late import. Predictions already stored keep the snapshot they used.
