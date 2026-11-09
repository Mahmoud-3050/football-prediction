# Football Prediction Bot

Telegram bot that estimates one named match and abstains when the data is not good enough. It does not browse fixtures, and it does not turn a guess into a betting tip.

Commands:

```text
/today
/matches 24-05-2026
/predict Arsenal vs Chelsea | Premier League
/predict #560638
```

`/today` and `/matches DD-MM-YYYY` list fixtures in Premier League, UEFA Champions League, Primera Division, Serie A, Bundesliga, and Ligue 1. They do not predict by themselves. Each match also gets a tap button that runs the same analysis as `/predict <id>`. Kickoff times and the `/matches` day use Cairo time.

`vs` separates the clubs. `|` separates the competition. Home and away stay in the order you wrote them.

## Run

1. Use Python 3.10 or newer.
2. Create a BotFather token, a free token from [football-data.org](https://www.football-data.org/client/register), and a Google AI Studio key.
3. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
4. `cp .env.example .env` and fill the three keys.
5. `python main.py`
6. `/today` then `/predict #<id>`, or `/predict Arsenal vs Chelsea | Premier League`

The process only listens for commands. `/today` loads fixtures when you ask. Long polling waits for a Telegram message. It does not schedule predictions.

`GEMINI_MODEL` defaults to `gemini-3.5-flash`. A display name is converted to an API id. On 503/429 the bot retries, then tries `GEMINI_FALLBACK_MODELS`. The model package is `google-genai`.

## Tests

```bash
pytest
```

Tests use local fixtures. They do not call Football-Data or Gemini.
