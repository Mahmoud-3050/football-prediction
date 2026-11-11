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

1. Use **Python 3.9 or newer** (3.10+ recommended; 3.9 is end-of-life).
2. Create a BotFather token, a free token from [football-data.org](https://www.football-data.org/client/register), and a Google AI Studio key.
3. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
4. `cp .env.example .env` and fill the three keys. Never commit `.env`.
5. Set a strong `DASHBOARD_PASSWORD` (and `DASHBOARD_SESSION_SECRET`) before any non-loopback dashboard bind.
6. Start everything on a Mac with one shortcut:
   ```bash
   chmod +x start.sh start.command   # once
   ./start.sh
   ```
   Or double-click `start.command` in Finder. This runs the Telegram bot, jobs, and dashboard together (Ctrl+C stops all). Logs go to `data/logs/`.
7. Optional one-word alias (add to `~/.zshrc`):
   ```bash
   alias fp='cd ~/Desktop/Projects/Football-Predictor/code && ./start.sh'
   ```
8. Or run processes yourself:
   - `python main.py` — Telegram bot (idle until `/start` in a chat)
   - `python -m jobs` — server jobs (tomorrow predictions + standings refresh)
   - `python -m laboratory serve` — dashboard at http://127.0.0.1:8765
9. In Telegram: `/start`, then `/today` / `/predict …`

Production publish steps (HTTPS proxy, process managers, DB backup): [docs/deploy-checklist.md](docs/deploy-checklist.md).

Long polling waits for Telegram commands. Other commands stay blocked until that chat sends `/start`.

Server jobs (separate process) every `JOBS_INTERVAL_SECONDS` (default 3600):

- **Tomorrow predictions** — price predictable Cairo-tomorrow fixtures in PL, CL, PD, SA, BL1, FL1, PPL, DED
- **Standings refresh** — when a league finishes fixtures today, re-fetch standings into the DB

Legacy in-bot pre-round remains available with `PRE_ROUND_ENABLED=1`, but `python -m jobs` is preferred.

`GEMINI_MODEL` defaults to `gemini-3.5-flash`. A display name is converted to an API id. On 503/429 the bot retries, then tries `GEMINI_FALLBACK_MODELS`. The model package is `google-genai`.

Bot runs and laboratory data share one SQLite file: `DATABASE_PATH` (default `data/football.db`). Optional `LAB_DATABASE_PATH` overrides that path for laboratory CLI only. To merge older `data/predictions.db` + `data/laboratory.db` files once:

```bash
python -m laboratory migrate-db
```

## Tests

```bash
pytest
```

Tests use local fixtures. They do not call Football-Data or Gemini.

## Laboratory

The historical laboratory uses the same SQLite file as the bot (`DATABASE_PATH` / `data/football.db`). League history, experiment predictions, evaluations, and Telegram bot run traces all live there in separate tables.

**Prediction rule:** production predictions are created **only** by the Telegram bot system — `/predict` (on demand) or the 1-day pre-round job — and stored as `prediction_run` / `prediction_snapshot` / `prediction_version` rows. Imported league history in the `matches` tables is fixture data for research/imports; it is not batch-priced into bot predictions. Finished and in-progress fixtures are never predicted by the live bot.

### Bot scorer backtest (no Gemini)

Prices Premier League history with the same `score_model` used by `/predict`:

```bash
python -m laboratory backtest-bot
python -m laboratory serve
```

Defaults to seasons **2024, 2025, and 2026**. Override with repeated `--season` flags. Results are stored under experiment `BOT-PL-001`. Open `http://127.0.0.1:8765` for the dashboard (overview, match list, match detail, standings).

League tables: fetch once from Football-Data.org into the shared DB, then the dashboard reads SQLite only:

```bash
python -m laboratory import-standings
```

Opening `/standings` with `FOOTBALL_API_KEY` set also fills a missing league on first view.

Dashboard auth: set `DASHBOARD_PASSWORD` (and optional `DASHBOARD_USER`, default `admin`). The dashboard then shows a **Sign in** page at `/login` and uses an HttpOnly session cookie. Binding outside loopback without a password fails. `/healthz` stays open for probes. Pre-production steps: [docs/deploy-checklist.md](docs/deploy-checklist.md).

### Lab Poisson experiment

```bash
python -m laboratory import-pl
python -m laboratory serve
```

`import-pl` loads Premier League seasons 2023–2026 and prices them with the laboratory Poisson engine (`EXP-PL-001`). Gemini is not called.

`import-pd` stores Primera Division (La Liga) seasons **2024, 2025, and 2026** only — no pricing yet:

```bash
python -m laboratory import-pd
```

`import-leagues` stores the same three seasons for Ligue 1 (`FL1`), Bundesliga (`BL1`), Serie A (`SA`), Primeira Liga (`PPL`), and Eredivisie (`DED`) — no pricing:

```bash
python -m laboratory import-leagues
```

Override with `--competition FL1 --competition SA` and/or `--season 2025`.

`price-finished` walk-forward prices **finished** stored matches with the Telegram bot `score_model` (live stack: `k=6`, last-season priors, fixed HT 0.45), one experiment per competition (`BOT-{code}-live`):

```bash
python -m laboratory price-finished --exclude-competition PL
```

`demo` loads four fictional matches and prices them with Poisson only.
