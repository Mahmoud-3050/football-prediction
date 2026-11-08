# Football Prediction Bot

Telegram bot that estimates one named match and abstains when the data is not good enough. It does not browse fixtures, and it does not turn a guess into a betting tip.

The command is:

```text
/predict Arsenal vs Chelsea | Premier League
```

`vs` separates the clubs. `|` separates the competition. Home and away stay in the order you wrote them.

## Run

1. Use Python 3.10 or newer.
2. Create a BotFather token, a free token from [football-data.org](https://www.football-data.org/client/register), and a Google AI Studio key.
3. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
4. `cp .env.example .env` and fill the three keys.
5. `python main.py`
6. `/predict Arsenal vs Chelsea | Premier League`

The process only listens for commands and never scans the fixture list on its own. Long polling waits for a Telegram message. It does not schedule predictions.

`GEMINI_MODEL` defaults to `gemini-2.5-flash`. The model package is `google-genai`.

## Tests

```bash
pytest
```

Tests use local fixtures. They do not call Football-Data or Gemini.
