---
description: Governs ALL dashboard UI/UX work in the Football Predictor lab — redesigns, CSS/HTML polish, layout/spacing/typography, new analytical views (experiment leaderboards, calibration, promotions), routes, auth/CSRF, team display names, and dashboard tests. Apply when editing laboratory/dashboard.py, laboratory/auth.py, laboratory/team_display.py, or any tests/test_dashboard_*.py. The dashboard is server-rendered Python HTML (no React/Vite/SPA).
trigger: glob
globs: "**/laboratory/dashboard.py, **/laboratory/auth.py, **/laboratory/team_display.py, **/tests/test_dashboard_states.py, **/tests/test_dashboard_auth.py, **/tests/test_match_predict_button.py, **/tests/test_product_ops.py"
---

# 05 — Dashboard UI/UX Rule

Source: `docs/DASHBOARD_BLUEPRINT_FOR_SKILL.md` (derived from source inspection). If `laboratory/dashboard.py` disagrees with this rule, the source wins: tell the user, do not silently "fix" either side. Related: `.agents/rules/03-laboratory-backtest.md` (same prohibitions on renaming `match-card` / `market-strip`).

## 1. Dashboard Architecture & SQLite Safety

### Architecture (REQUIRED to preserve)
- Entry: `python -m laboratory serve` → `laboratory/__main__.py: serve(path, host, port)` → `build_dashboard_server(path, host, port, *, auth=None)` → `ThreadingHTTPServer((host, port), Handler)`. Handler is an inner `BaseHTTPRequestHandler` with `do_GET` / `do_POST`. `log_message` is a no-op.
- Defaults: host `DASHBOARD_HOST` or `127.0.0.1`; port `DASHBOARD_PORT` or `8765`. DB path via `database_path()` → `resolve_database_path(prefer_lab_override=True)` (normally `data/football.db`).
- **All markup, CSS, and JS live in Python string builders inside `laboratory/dashboard.py`.** There are no `.css`, `.js`, or template files, and no static asset pipeline. Only crest images are served from disk (`GET /crests/<filename>` from `crest_cache_dir()`, extensions `.png/.svg/.jpg/.jpeg/.webp`, traversal rejected, `Cache-Control: public, max-age=86400`).
- **FORBIDDEN:** React/Next/Vite/Tailwind build/npm/CSS bundler/template engine, Chart.js or any charting dependency, client-side routing, `fetch`-driven SPA behavior. JS stays limited to the tiny `[data-copy]` clipboard helper unless the user explicitly approves more; if added, it is inline, framework-free, and degrades without JS.
- **FORBIDDEN:** new external network assets beyond the existing Google Fonts (IBM Plex Sans + IBM Plex Mono) already loaded in `_page`.
- **REQUIRED:** page functions are pure `(connection, query) -> str` builders using existing helpers: `_page(title, body)`, `_masthead(..., active=...)`, `_filters`/`_filter_form`, `_state`. Missing resource → page function returns `None` and the handler maps it to 404 (as `match_detail` / `experiment_detail` do).
- **REQUIRED:** every dynamic value interpolated into HTML (team names, league names, flash params, query values, error detail) is escaped. Read how `dashboard.py` already escapes before writing new f-strings and reuse that exact helper; do not invent a helper name.
- **REQUIRED:** keep `_html` response headers exactly: `Content-Type: text/html; charset=utf-8`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. New HTML responses go through `_html`.
- **REQUIRED:** exceptions in handlers still produce `server_error_page(detail=...)` (HTML 500, "Something went wrong", `role="alert"`).
- Team labels go through `laboratory/team_display.py` (`DISPLAY_NAME_BY_OFFICIAL`, `team_display_map`, `_club_label`). **FORBIDDEN:** showing legal/official club names on cards.

### SQLite read-safety (CRITICAL)
- `build_dashboard_server` opens **one long-lived** connection (`laboratory.store.connect(path)`: `timeout=60`, `busy_timeout=60000`, WAL, `synchronous=NORMAL`, `wal_autocheckpoint=1000`, `temp_store=MEMORY`, `foreign_keys=ON`, then `ensure_shared_schemas`) shared by all request threads (`check_same_thread=False`).
- **FORBIDDEN:** changing those PRAGMAs, disabling WAL, or opening ad-hoc `sqlite3.connect` with different settings. Use `laboratory.store.connect`.
- **REQUIRED:** because the connection is shared across threads, finish each query inside the handler (`fetchall()` before returning), never leave a transaction or cursor open past the end of a handler, and never keep one open across a sleep/poll/long computation.
- **REQUIRED:** dashboard reads touch `matches`, `result_revisions`, `predictions`, `evaluations`, `engine_runs`, `snapshots`, and job-run tables. The only existing writes are POST predict (`price_one_score_model(connection, match_id, replace=True)`) and POST experiment run. **Do not add new write paths** without explicit user approval; if approved, commit immediately and keep the write tiny.
- **REQUIRED:** new queries are bounded: `LIMIT`/pagination (`PAGE_SIZE=40` for match lists), indexed filters, no unbounded joins. Do not copy `_load`'s load-then-filter-in-memory pattern for new, larger tables.
- **REQUIRED:** lab-only tables (`lab_experiments`, `lab_experiment_runs`) are read through a **short-lived** connection (open → query → close), the way `experiments_list` / `experiment_detail` do. Read those functions to see exactly how the lab DB path is resolved; do not hard-code a path. Never hold lab and live connections together across a sleep or await.
- **FORBIDDEN:** running `lab-run`, `tune-spl`, `price-finished`, `import-*`, or any multi-minute work inside a request thread. The dashboard **reads** results; heavy jobs stay CLI/background (`python -m laboratory …`, `python -m jobs`). A "Run" button may only run tiny work, or become enqueue-and-poll via a job table (needs user approval).
- If a page fails with `database is locked`: suspect WAL growth after bulk pricing (`checkpoint_wal(..., mode="TRUNCATE")` exists for this) and `make dash-restart`; do not "fix" it by loosening the connection settings.

## 2. UI/UX Design System & Skill Orchestration

### Design tokens (extend `:root` in `_page`; do not fork)
| Token | Value |
| --- | --- |
| `--bg` / `--bg-2` / `--panel` / `--line` | `#0b1220` / `#111a2b` / `#152238` / `#243552` |
| `--text` / `--muted` | `#e8eef8` / `#93a4bd` |
| `--accent` / `--accent-2` / `--focus` | `#34d399` / `#10b981` / `#6ee7b7` |
| `--good` / `--bad` / `--wait` | emerald / rose / amber |
| `--space-1…6` | 4 / 8 / 12 / 16 / 24 / 32 px |
| radius | sm 8 / md 12 / lg 16 |
| ease | `cubic-bezier(0.2, 0, 0, 1)` |

- **REQUIRED:** new colors/spacing/radii are added as tokens in `:root` and consumed via `var(--…)`; no stray hex or magic px in component CSS.
- **REQUIRED:** body font 16px IBM Plex Sans; numbers, ids, and probabilities use IBM Plex Mono (`.mono`, `.num`). Keep ≥44px hit targets on nav, filters, and buttons; keep 2px `:focus-visible` rings; keep `prefers-reduced-motion` disabling animations/transitions.
- **REQUIRED:** this is an analytical dashboard. Preserve data density; do not hide metrics behind decorative whitespace or hero sections.

### Skill locations (CRITICAL — path correction)
The local skills are in **`.cursor/skills/<name>/SKILL.md`**, not a top-level `skills/` directory. Read the skill's SKILL.md before applying it; do not invent its script paths or flags.

### Skill orchestration
| Situation | Use | How to apply on THIS dashboard |
| --- | --- | --- |
| New page, palette/type/chart decisions, data-dense layouts | `.cursor/skills/ui-ux-pro-max` | Follow its priority order: accessibility → touch → performance → layout → type/color → animation → style → charts. Use it for guidance only: **do not adopt a stack it suggests (e.g. Tailwind) or any asset that violates §1**. Existing tokens win over generated palettes. |
| Hover/press states, radii, shadows, borders, icons, transitions, "make it feel better" | `.cursor/skills/better-ui` | Concentric radii, optical alignment, interruptible CSS transitions, scale(0.96) press on buttons only, **no `transition: all`**, icon stroke matching text weight. Pure CSS inside `_page`. |
| Review/critique of a rendered page | `.cursor/skills/better-interface` | It is Figma-oriented. Borrow only its review order (accessibility → layout → writing → typography → UI polish), keyboard-first checks, evidence with `file:line`, and modes `quick` (cap 5) / `full` (cap 15). Output findings as a text list. **Read-only unless the user asks to implement.** Skip any Figma/card-pinning steps. |
| Spacing inconsistency | `.cursor/skills/spacing-audit` | Figma-oriented. Use its scale only: 0, 4, 8, 12, 16, 24, 32, 40, 48, 56, 64, 80, 96. Prefer `--space-*` tokens (≤32) and only add larger steps from that scale. Change spacing only; preserve tokens, hairlines, and optical adjustments; summarize before applying. |
| UX/BRD coverage audit | `.cursor/skills/ux-ui-auditor` | **Only if** the EasyCity BRD/screen-inventory CSVs it requires exist under `docs/`. This repo does not ship them; if absent, say so and do not run it or fabricate inputs. It writes only under `docs/quality-assurance/` and does not approve other skills' work. For a generic dashboard audit use `better-interface`'s checklist instead. |
| Craft/aesthetic principles | `.cursor/skills/taste` (`design-taste-frontend`) | Adapted; see below. |

### Adapted `design-taste-frontend` (`.cursor/skills/taste`)
**Borrow:** anti-slop typography (no default-looking system stacks beyond the existing IBM Plex pair); equal content edges and consistent gutters; crisp visual hierarchy (one clear primary per panel); subtle CSS-only hover/press micro-interactions; **no layout shift** (reserve space for crests/charts/badges; animate only `transform`/`opacity`); no violet/lavender unless the user asks (the accent is emerald).
**EXCLUDE (do not enforce, do not run):** one-section-per-screen layouts, word-count-per-screen limits, stock-photography hunts, and any landing-page/marketing gate or browser-gate hook the skill ships. None of that applies to an analytical prediction dashboard.

### Precedence
This rule > §3 protected contracts > skills. If a skill's advice conflicts with §1 or §3, follow this rule and say so.

## 3. Protected DOM & Auth Invariants (NEVER BREAK)

**FORBIDDEN:** renaming or removing any selector, attribute, visible string, route, or status code below unless the matching tests are updated **in the same commit**: `tests/test_dashboard_states.py`, `tests/test_dashboard_auth.py`, `tests/test_match_predict_button.py`, `tests/test_product_ops.py`, and the match-list assertions in `tests/test_laboratory.py`. Adding new classes is fine; changing these is not.

### CSS classes / DOM
- `market-strip` inside match-card HTML.
- `class="match-card"` (the card is an `<a>` to the detail page), `class="match-list"`, `class="match-row"`, `score-label">P<`.
- Overview charts: `charts-panel`, `hbar-chart`, `vbar-chart`, `cal-chart`.
- `class="state state-empty"`, `class="state state-error"`; `role="status"` on the empty overview, `role="alert"` on the error page.
- `aria-label="Evaluation charts"` must be **absent** on the empty overview.
- Predict flow: `predict-panel`, `action="/matches/{id}/predict"`, `name="csrf" value="…"`.
- Jobs panel: `job-ok`, `job-failed`.
- Filters: `name="confidence"` with option text `≥ 65%`; `name="league"` (selected option preserved, e.g. La Liga); hidden `engine` input forced to `score_model`.
- Login HTML contains `name="csrf"`.

### Visible copy
- Market strip labels: `FT 1X2`, `HT`, `Score`, `Goals 2.5`, `Goals 3.5` (from `_prediction_market_confidences`) and percent strings such as `62%`/`56%`/`72%`; `1X2` on priced cards.
- Empty/404/500: `No matches in the laboratory yet`, `Open Matches`, `Back to Overview`, `Match not found`, `Browse matches`, `Something went wrong`.
- Jobs panel: `Server jobs`, `Tomorrow predictions`, `Standings refresh`, `priced 3`; times shown in Cairo, **never a bare ` UTC`**.
- Charts: `Market accuracy`, `Full-time accuracy by month`, `FT selection calibration`.
- Standings empty: `No standings stored for this league yet` plus (`FOOTBALL_API_KEY` **or** `import-standings`).
- Auth: `Sign in`, `Wrong username or password`.
- Predict: `Run prediction` / `Re-run prediction` / `Prediction stored` / `Bot score model`.

### Routes, status codes, auth
- `GET /healthz` → 200, body exactly `ok\n`, no auth (readiness probe for tests).
- `GET /matches/<id>/predict` → 404; predict is **POST-only**. `POST /matches/<id>/predict` (authed) → 303 back to `/matches/<id>…` with `predict_status` / `predict_detail` / `predicted`, and stores the prediction.
- Auth enabled + no cookie: protected GETs → 303 to `/login?next=…`; POST predict/experiment → login redirect. `GET /login` → 200 with Sign in + csrf. Bad password → **401** with the wrong-password message. Good login → 303 + Set-Cookie. `GET /logout` → 303 `/login` + cleared cookie.
- Cookie `pitch_lab_session`: `Path=/; HttpOnly; SameSite=Lax; Max-Age=…` (+ `Secure` when `DASHBOARD_COOKIE_SECURE`); TTL 7 days; token `username:expires:hmac_sha256(secret, payload)`.
- **CSRF REQUIRED** (HMAC `csrf_token()`, `secrets.compare_digest`) on every POST: login, predict, experiment run, and any new POST. **FORBIDDEN:** a mutating GET.
- `require_auth_for_host`: non-loopback bind (`127.0.0.1` / `localhost` / `::1` are loopback) without `DASHBOARD_PASSWORD` → `SystemExit` mentioning `DASHBOARD_PASSWORD`. Never weaken.
- `GET /healthz` and `GET /crests/<file>` are the only unauthenticated paths besides `/login`; do not add others.

### Helper behavior tied to tests
- `_confidence_threshold("all")→None`, `"65"→0.65`, `"0.55"→0.55`; the confidence filter in `_apply` keeps only high-probability FT picks; `_ft_1x2_from_body` / `_prediction_market_confidences` return the existing selection/confidence values for FT/HT/score/OU.
- `_masthead` nav: Overview `/`, Matches `/matches` (preserves filter query), Standings `/standings`, Experiments `/experiments`, Log out only when auth is enabled; active tab via `class="active"`.
- Shared filter params (`_filters`): `engine`, `league`, `season`, `team`, `date_from`, `date_to`, `fixture` (`all|finished|scheduled`), `status` (`all|completed|excluded|pending|failed`), `verdict` (`all|correct|incorrect|unscored`), `confidence` (`all|45|55|65`), `order` (`newest|oldest`), `page`. Do not rename values.

### Known defect (do not build on it)
`POST /experiments/run`: the form builder creates `config_data`, but the call site references an undefined `exp_config`. Treat experiment-from-UI as unfinished. If asked to fix it, make that a separate, explicit change and keep CSRF and redirect-flash behavior; do not depend on it for new views.

## 4. Adding New Analytical Views (Test Lab Readiness)

Follow this checklist in order:
1. **Page function:** add `def <view>(connection, query) -> str` in `laboratory/dashboard.py` (e.g. `promotions_list`), built from `_masthead(..., active="<key>")`, `_filter_form` (or a dedicated GET form), `_state`, `_page`.
2. **Route:** add one branch in `Handler.do_GET` next to `/experiments`; add `do_POST` only for an approved mutation. Detail pages return `None` → 404.
3. **Nav:** add the link in `_masthead` with its new `active` key; do not reorder or rename existing links.
4. **Data:** football data via the serve-time connection; lab tables (`lab_experiments`, `lab_experiment_runs`) via a short-lived connection as in `experiments_list`/`experiment_detail`. Known columns: `lab_experiments(experiment_id, competition, config_json, status, timestamps, best_config_json, best_metrics_json)`, `lab_experiment_runs(run_id, experiment_id, model_id, config_json, status, metrics_json, priced/evaluated counts, error, timestamps)`. Read `laboratory/store.py` for exact column names before writing SQL.
5. **Leaderboards:** read `lab_experiment_runs.metrics_json` (parse defensively; a run may be missing or failed) and show `status`, priced/evaluated counts, and the primary metric (default `log_loss`). Lower log-loss/Brier is better; show **n** next to every metric; never display a metric without its experiment_id and n.
6. **Calibration/comparison views:** reuse `_experiment_comparisons` and the `_overview_charts` primitives (`.cal-chart`, `.hbar-*`, `.vbar-*`, `.chart-axis`). Charts are inline SVG/CSS bars only.
7. **Promotions view:** do not invent a promotion-history table. Read `laboratory/lab_promotion.py` and `laboratory/store.py` to find where promotion/rollback history is actually recorded (rollback files vs DB). If it is not in the DB, say so and ask before adding storage. The dashboard must **never write** `configs/live/*` or `data/spl_stack.json`; promotion stays CLI (`lab-promote`, `lab-rollback`).
8. **Mutating POSTs:** reuse `_authenticated` + CSRF + 303 redirect with `*_status` / `*_detail` flash params (same as predict and experiment run), and keep the retained filter query via `next`.
9. **Empty/error states:** use `_state` (`state-empty|state-error|state-info|state-loading`) with `role="status"`/`role="alert"`; copy states what to do next (CLI command or env key), like the standings empty state.
10. **States, a11y, responsiveness:** every new view has empty, error, and populated states; semantic `<table>` inside `.table-wrap`; labels on all form controls; usable at ~375px width with no horizontal page scroll (tables scroll inside `.table-wrap`).
11. **Tests:** add tests alongside the existing dashboard tests for the new route (200 with auth off; redirect to `/login` with auth on; empty state; 404 for unknown id). Tests must pass offline.

## 5. Post-Edit Verification Protocol

### Tests (run, or instruct the user to run)
```bash
.venv/bin/python -m pytest tests/test_dashboard_states.py tests/test_dashboard_auth.py tests/test_match_predict_button.py tests/test_product_ops.py -q
.venv/bin/python -m pytest tests/test_laboratory.py -q      # match-list UI assertions
.venv/bin/python -m pytest -q                                # or: make test
```
If markup assertions fail, inspect `laboratory/dashboard.py` first; update tests only when you intentionally changed a protected contract (§3), in the same commit.

### Run and look (visual / responsive)
```bash
make dash-restart          # kills :8765 and restarts the dashboard
curl -s http://127.0.0.1:8765/healthz      # expect: ok
```
Open `http://127.0.0.1:8765` and check, for every page you touched:
1. **Auth off (loopback):** page loads; **auth on:** start with `DASHBOARD_PASSWORD` set, confirm unauthenticated GET redirects to `/login`, wrong password → 401, login works, predict/new POSTs reject a missing/invalid csrf.
2. **Widths:** ~375px, ~768px, ~1280px. No horizontal page scroll; cards/tables reflow; nav usable; touch targets ≥44px.
3. **Keyboard:** tab through nav, filters, buttons; visible focus ring on each; `match-card` links reachable.
4. **Contrast & states:** muted text legible on `--panel`; empty, error, populated, and long-name (team display) states all render; no layout shift when crests/charts load.
5. **Motion:** hover/press are CSS-only and subtle; with reduced-motion enabled, no animation remains.
6. **Concurrency sanity:** with the bot/jobs running, page loads stay fast and nothing logs `database is locked`; no new write path was added.
7. **Escaping:** a team/league name containing `<` or `&` renders as text.

### Report
State which pages/routes changed, which protected contracts (§3) were touched (ideally none), the exact test commands run with results, and anything not verified (for example no browser available).