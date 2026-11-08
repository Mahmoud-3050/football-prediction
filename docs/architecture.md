# Architecture

The bot answers one Telegram command. The user names the home club, the away club, and the competition. That message is the only thing that starts a lookup, a snapshot, or a model call.

Long polling stays open so Telegram can deliver the message. It does not scan competitions, prefetch fixtures, or run a second match in the background. There is no fixture list, no inline match picker, and no periodic job.

## Command

```text
/predict Arsenal vs Chelsea | Premier League
```

Empty home, away, or competition text is rejected before any provider call. The same club on both sides is rejected the same way. Names may contain spaces. `vs` splits the clubs. `|` splits the competition.

Competition labels resolve only to the free tier: FIFA World Cup (`WC`), UEFA Champions League (`CL`), European Championship (`EC`), Premier League (`PL`), Championship (`ELC`), Bundesliga (`BL1`), Serie A (`SA`), Primera Division (`PD`), Ligue 1 (`FL1`), Primeira Liga (`PPL`), Eredivisie (`DED`), and Campeonato Brasileiro Série A (`BSA`). An unknown name returns that list. `Serie A` is the Italian league. The Brazilian league is requested by its own name or code.

Inside the named competition, a club resolves by its name, short name, three-letter code, or a known alias such as `Man City`, `Barca`, or `Inter`. A typo returns the closest names from that competition. An ambiguous name asks for a more specific one. Home and away are never swapped to force a hit.

## Workflow

```mermaid
flowchart TD
  user[User message] --> parse[Validate home away competition]
  parse --> identify[Resolve competition and exact fixture]
  identify --> fetch[Fetch standings form H2H]
  fetch --> check[Validate quality consistency freshness]
  check --> snap[Save point-in-time snapshot]
  snap --> eligible{Eligible}
  eligible -->|no| abstain[Abstain and explain the stage]
  eligible -->|yes| gemini[Gemini score prediction]
  gemini --> markets[Derive markets from the validated score]
  markets --> version[Save prediction version]
  abstain --> reply[Telegram report]
  version --> reply
```

`/predict` acknowledges the request, then awaits this path. The await covers only the match in that message.

### Identify

Teams are loaded for the one competition. The fixture search is `GET /v4/teams/{homeId}/matches` filtered to that competition, that opponent, and a home venue.

- The earliest `SCHEDULED` or `TIMED` fixture is the one that can be predicted.
- If the only meeting is the opposite venue, the reply names that fixture and stops.
- A `FINISHED` match reports the recorded score and stops. A match that has already been played is not predicted.
- `IN_PLAY`, `PAUSED`, `POSTPONED`, `SUSPENDED`, and `CANCELLED` stop with that status. They are not treated as a future match.
- If nothing matches, the reply says the competition and both clubs were checked.

### Retrieve

Only after that fixture is identified:

- `GET /v4/competitions/{code}/standings` for position, points, goals for, goals against, and goal difference.
- `GET /v4/teams/{id}/matches?status=FINISHED&limit=30` for each club. The analysis window is the latest five with a full-time score.
- Head-to-head meetings are taken from that history. If none are there, one `GET /v4/matches/{id}/head2head?limit=5` is used.

Code, not the model, computes last-five W-D-L, half-time goals scored and conceded, and second-half goals (`full time` minus `half time`) when both scores exist and the half-time score does not exceed the full-time score.

Football-Data is called with `X-Auth-Token`. The client keeps about 8 calls per minute in process. HTTP 429 waits once for this request and then stops. HTTP 401 or 403 names the rejected key or competition. Team and standings bodies are cached on disk only while a request reads or writes them. There is no refresh loop.

### Validate

Every check is kept as pass, fail, or limitation.

| Check | What it requires |
| --- | --- |
| Consistency | Fixture home, away, and competition match the request, and both clubs appear in that standings table. |
| Availability | Each club has a standings row with goals and points, and the matches used have full-time scores. |
| Quality | Missing scores and missing table numbers stay missing. They are not written as zero. |
| Freshness | Each payload's `lastUpdated` is stored. A missing timestamp is freshness unknown, not current. The free-tier delay is always a warning. |

### Snapshot

The snapshot is the only Gemini input: request labels, resolved ids, kickoff, raw standings rows, last five, head-to-head, derived facts, check results, limitations, and fetch time. The stored snapshot keeps half-time samples so the trace shows why that market was skipped. The prompt copy drops those fields when the market is ineligible.

### Predict or abstain

No score is requested when any of these are true:

- The competition, club, or fixture was not identified.
- Standings are missing for either club.
- Either club has fewer than 3 finished matches with a full-time score.
- The provider rejected the key, hit the free-tier limit after one wait, or could not be reached.
- Gemini returns invalid JSON twice, or a half-time score that disagrees with its Team A / Team B / Draw label.

These limitations still allow a full-time estimate, and they are printed:

- Fewer than 3 of the last 5 scored matches are in the named competition. Matches from other competitions are used and named.
- Half-time scores exist on fewer than 3 of either club's last 5. Half-time result and half-time score are left out of the prompt and the report.
- No head-to-head meetings were found.
- A required `lastUpdated` value is missing, so freshness is unknown.

The free-tier delay warning is printed on every report that used provider data. By itself it does not change a completed run into a partial one.

Gemini is `google-genai` with `gemini-2.5-flash` unless `GEMINI_MODEL` is set. The call asks for JSON, with thinking budget 0. The accepted object is:

```json
{
  "half_time_prediction": "Team A | Team B | Draw",
  "half_time_score": "1-0",
  "full_time_score": "2-1",
  "confidence_percentage": 75,
  "tactical_reasoning": "..."
}
```

Half-time fields are omitted from the prompt and the schema when that market is ineligible. Team A is the requested home club and Team B is the requested away club. A response is validated for the enum, `N-N` scores, and confidence from 0 to 100, then retried once.

Markets are computed from the validated full-time score:

- 1X2 from which side has more goals.
- Both teams to score, yes or no.
- Total goals over or under 2.5.

The report labels those three as derived from the scoreline. Confidence belongs to that score.

### Trace

SQLite at `DATABASE_PATH` (default `data/predictions.db`) stores three rows:

- **Prediction Run:** chat, user, raw home, away, and competition text, time, status `completed | partial | abstained | failed`, stage, and the user-facing reason. A run is inserted when the command is accepted and updated when the path stops. A crash before that update stays `failed`, not `completed`.
- **Prediction Snapshot:** one row once a fetch has produced data, including abstentions after a fetch. It holds the capture time, competition code, match id, team ids, kickoff, payload, freshness, and validation checks.
- **Prediction Version:** written only when a prediction was produced. It stores the model id, prompt version, schema version, output JSON, and limitations. A rejected first response is not stored. If the retry is the response that passes, it is version 2 of the same run.

### Reply

Success names the competition, fixture, kickoff, half-time market when it is eligible, full-time score, derived 1X2, BTTS, over/under 2.5, confidence, reasoning, limitations, warnings, and the run id. The footer says the result is a statistical estimate, not a betting tip.

Failure names the stage (`input`, `competition`, `teams`, `fixture`, `standings`, `form`, `provider`, `model`) and the reason. A known cause is not replaced with a generic retry message. One failed update does not stop the process.

## Modules

Shared dataclasses live in `models.py`. The other modules are the request path:

| Module | Role |
| --- | --- |
| `config.py` | Required keys and optional model, log level, and database path. Import and startup do not call external APIs. |
| `data_fetcher.py` | Async Football-Data client, competition and club resolution, fixture orientation. |
| `eligibility.py` | Checks, derived facts, and the abstain or partial decision. |
| `predictor.py` | Prompt, Gemini call, validation, retry, and derived markets. |
| `storage.py` | Run, snapshot, and version tables. |
| `pipeline.py` | The single orchestration path. |
| `bot.py` | `/start`, `/predict`, and the Markdown report. |
| `main.py` | Process entry. Long polling only. |

## Data relationships

```mermaid
erDiagram
  PREDICTION_RUN ||--o| PREDICTION_SNAPSHOT : captures
  PREDICTION_SNAPSHOT ||--o| PREDICTION_VERSION : produces
  PREDICTION_RUN {
    int id
    string status
    string stage
    string reason
  }
  PREDICTION_SNAPSHOT {
    int id
    int run_id
    string payload
    string freshness
    string validation_checks
  }
  PREDICTION_VERSION {
    int id
    int run_id
    int snapshot_id
    int version_number
    string output_json
  }
```

A run can stop with no snapshot when the command or the competition name fails before a fetch. A snapshot can exist with no version when the path abstains or the model does not return a valid score. A version always points at the snapshot that was shown to the model.
