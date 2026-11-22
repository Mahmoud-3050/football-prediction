# Dashboard Blueprint (for skill / UI work)

Dense technical specification of the laboratory web dashboard, auth, routes, DOM contracts, tests, and local UI/UX skills. Derived from source inspection only.

---

### 1. Dashboard Architecture & Server Lifecycle

**Entry**
- CLI: `python -m laboratory serve` (`laboratory/__main__.py` → `serve(path, host, port)`).
- Default bind: host `DASHBOARD_HOST` or `127.0.0.1`; port `DASHBOARD_PORT` or `8765`.
- DB path: `database_path()` → `resolve_database_path(prefer_lab_override=True)` (shared bot file; typically `data/football.db`).

**HTTP stack**
- `build_dashboard_server(path, host, port, *, auth=None)` opens one long-lived SQLite connection via `laboratory.store.connect(path)`, then returns `ThreadingHTTPServer((host, port), Handler)`.
- Handler is an inner `BaseHTTPRequestHandler` subclass with `do_GET` / `do_POST`.
- `serve()` prints mode (`auth on` / `auth off (loopback only)`) and calls `server.serve_forever()`.
- `log_message` is no-op (quiet console).

**Public / health**
- `GET /healthz` → plain `ok\n`, status 200, **no auth**. Used by auth/predict integration tests as readiness probe.
- Crest assets: `GET /crests/<filename>` serves files from `crest_cache_dir()` with `Cache-Control: public, max-age=86400`; path traversal rejected; extensions limited to `.png/.svg/.jpg/.jpeg/.webp`.

**Auth (`laboratory/auth.py`)**
- Cookie name: `pitch_lab_session`.
- Session TTL: 7 days (`SESSION_TTL_SECONDS`).
- Env: `DASHBOARD_USER` (default `admin`), `DASHBOARD_PASSWORD` (empty → auth disabled), `DASHBOARD_SESSION_SECRET` (else derived from password hash, or `"dev-unauthenticated"`), `DASHBOARD_COOKIE_SECURE` (`1/true/yes/on`).
- Session token: `username:expires:hmac_sha256(secret, payload)`.
- CSRF: HMAC token `csrf_token()` (32 hex chars from `login-csrf-v1`); compared with `secrets.compare_digest` on POST login / predict / experiment run when auth enabled.
- Cookie flags: `Path=/; HttpOnly; SameSite=Lax; Max-Age=…` (+ `Secure` if configured).
- Fail-closed bind: `require_auth_for_host` — non-loopback (`127.0.0.1` / `localhost` / `::1`) **requires** password or `SystemExit`.
- When auth enabled and unauthenticated: protected GETs 303 → `/login?next=…`; POST predict/experiment → login redirect.

**HTML response headers (`_html`)**
- `Content-Type: text/html; charset=utf-8`
- `Cache-Control: no-store`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`

**SQLite connection lifetime & contention**
- One connection created at server build, shared across request threads (`check_same_thread=False`).
- `store.connect`: `timeout=60`, `PRAGMA busy_timeout=60000`, `journal_mode=WAL`, `synchronous=NORMAL`, `wal_autocheckpoint=1000`, `temp_store=MEMORY`, `foreign_keys=ON`, then `ensure_shared_schemas`.
- Dashboard mostly **reads** `matches`, `result_revisions`, `predictions`, `evaluations`, `engine_runs`, `snapshots`, job-run tables; **writes** only via POST predict (`price_one_score_model`) and experiment run (lab DB).
- Contenders for the same file: Telegram bot, `python -m jobs`, `tune-spl`, `lab-run` / walk-forward pricing. Large WAL + long-lived readers can surface `database is locked`; mitigation exists as `checkpoint_wal(..., mode="TRUNCATE")` after bulk price jobs.
- Experiments UI opens a **separate short-lived** connection to `data/lab.db` inside `experiments_list` / `experiment_detail` (open → query → close), not the serve-time football connection.

**Team display**
- `laboratory/team_display.py`: official → short fan names (`DISPLAY_NAME_BY_OFFICIAL`, heuristic shorteners). Dashboard uses `team_display_map` / `_club_label` so cards show Brighton / Man City / etc., not legal names.

**No SPA / no static asset pipeline**
- Zero separate `.css` / `.js` / template files in the repo for the dashboard. All markup + CSS + a tiny copy script live inside Python string builders in `laboratory/dashboard.py`.

---

### 2. Complete Route & Function Map

| Method | Path | Handler / page fn | Auth | Query / body | SQLite / data | Response shape |
|--------|------|-------------------|------|--------------|---------------|----------------|
| GET | `/healthz` | `_plain` | No | — | — | `text/plain` `ok\n` |
| GET | `/login` | `login_page(csrf, next)` | Public; if already authed → 303 to `next` | `next` | — | Standalone HTML (Sign in form) |
| POST | `/login` | `_post_login` | Public | form: `csrf`, `username`, `password`, `next` | — | 303 + Set-Cookie; or 400 CSRF / 401 bad password HTML |
| GET | `/logout` | redirect | Cookie clear | — | — | 303 `/login` + clear cookie |
| GET | `/` | `overview(connection, query)` | Yes if enabled | filters (see below) | `_load` join matches/results/predictions/evaluations/engine_runs; `latest_job_runs` | Masthead + jobs panel + filters + stat cards + market table + charts or empty state |
| GET | `/matches` | `match_list(connection, query)` | Yes | filters; default date window last ~14d→+7d if unset | same `_load` + in-memory `_apply` + page slice `PAGE_SIZE=40` | Masthead + filters + toolbar + `.match-list` of `.match-card` + pager |
| GET | `/matches/<id>` | `match_detail(...)` | Yes | filters + predict flash params | `matches`, `result_revisions`, `snapshots`, `predictions`, `evaluations`, `engine_runs` | Scoreboard + predict panel + facts + comparison + engine panels |
| GET | `/matches/<id>/predict` | — | — | — | — | **404** (`not_found_page`) — predict is POST-only |
| POST | `/matches/<id>/predict` | `_post_predict` | Yes | form `csrf`, optional `next` (retained query) | **write** via `price_one_score_model(connection, match_id, replace=True)` | 303 back to match with `predict_status` / `predict_detail` / `predicted` |
| GET | `/standings` | `standings(connection, query, api_key=…, ninjas_api_key=…)` | Yes | `league` etc. | standings tables / optional API keys from env | League table or empty guidance mentioning `FOOTBALL_API_KEY` / `import-standings` |
| GET | `/experiments` | `experiments_list(connection, query)` | Yes | filters + `run_status`/`run_detail` flash | **lab** `lab_experiments` on `data/lab.db` | Filter form + run form + experiment cards or empty |
| GET | `/experiments/<id>` | `experiment_detail(connection, exp_id, query)` | Yes | filters | lab `lab_experiments` + `lab_experiment_runs` | Header metrics + leaderboard + comparisons + promotion section; `None` → 404 |
| POST | `/experiments/run` | `_post_run_experiment` | Yes | form fields or legacy `config_yaml` + `csrf` | intended `run_experiment(lab.db, football.db, …)` | 303 `/experiments?run_status=…` |
| GET | `/crests/<file>` | `_crest` | No | — | filesystem crest cache | image bytes or 404 plain |
| * | other | `not_found_page` / plain 404 | GET HTML 404; POST plain `Not found\n` | — | — | 404 |
| * | handler exception | `server_error_page(detail=…)` | — | — | — | HTML 500 |

**Shared GET filter query params** (from `_filters`):  
`engine` (forced `score_model` via hidden input), `league`, `season`, `team`, `date_from`, `date_to`, `fixture` (`all|finished|scheduled`), `status` (`all|completed|excluded|pending|failed`), `verdict` (`all|correct|incorrect|unscored`), `confidence` (`all|45|55|65`), `order` (`newest|oldest`), `page`.  
Flash-only on match detail: `predicted`, `predict_status`, `predict_detail`.

**`_load` / `_load_match_page` (score_model live view)**  
Joins `matches` ← latest `result_revisions` ← `engine_runs` / `predictions` / latest `evaluations` where experiment is `BOT_EXPERIMENT_ID` for PL else `BOT-{competition}-live`.  
Matches list pushes league/season/team/date/fixture/status into SQL with `LIMIT`/`OFFSET` (`PAGE_SIZE=40`); `body_json` is loaded only for the current page. Verdict/confidence still post-filter in Python. Filter dropdowns use lightweight `DISTINCT` queries (`_filter_choices`). Crest/emblem resolution on list paths uses `fetch=False` (no synchronous HTTP). Indexes: `idx_matches_kickoff`, `idx_matches_comp_season_kickoff`, `idx_predictions_match_engine_exp`, etc. (created in `ensure_laboratory_schema`).

**Nav (`_masthead`)**  
Links: Overview `/`, Matches `/matches` (preserves filter query), Standings `/standings`, Experiments `/experiments`, optional Log out `/logout` when auth enabled. Active key sets `class="active"`.

**Note on POST `/experiments/run`:** form builder constructs `config_data`, but the call site currently references `exp_config` (undefined in the inspected handler). Treat experiment-from-UI as unfinished wiring until that mapping is fixed; list/detail pages themselves read `lab.db` correctly.

---

### 3. Current UI/UX Structure, Styling & DOM Contracts

**Generation model**
- Server-rendered Python f-strings / concatenations in `dashboard.py`.
- Shell: `_page(title, body)` → full HTML document with Google Fonts (IBM Plex Sans + Mono), large inline `<style>`, body content in `<main>`, and a small inline `<script>` for `[data-copy]` clipboard buttons.
- Login uses its own minimal document (`login_page`), same palette, no shared `_page` chrome.

**Design tokens (`:root` in `_page`)**
| Token | Value |
|-------|-------|
| `--bg` | `#0b1220` |
| `--bg-2` | `#111a2b` |
| `--panel` | `#152238` |
| `--line` | `#243552` |
| `--text` | `#e8eef8` |
| `--muted` | `#93a4bd` |
| `--accent` / `--accent-2` | `#34d399` / `#10b981` |
| `--good` / `--bad` / `--wait` | emerald / rose / amber |
| `--focus` | `#6ee7b7` |
| Spacing | `--space-1…6` = 4/8/12/16/24/32px |
| Radius | sm 8 / md 12 / lg 16 |
| Ease | `cubic-bezier(0.2, 0, 0, 1)` |

Body: dark radial gradients + linear background; 16px IBM Plex Sans; focus-visible 2px rings; 44px min hit targets on nav/filters/buttons; `prefers-reduced-motion` disables animations/transitions.

**Major components / CSS classes**
| Class / pattern | Role |
|-----------------|------|
| `.bar` + `nav a.active` | Top chrome + active tab underline |
| `.title-row` / `.title-back` | Match-detail back circle button |
| `.filters` / `.filter-actions` / `a.clear` | GET filter form |
| `.cards` / `.cards article` | Overview metric cards |
| `.jobs-panel` / `.job-cards` / `.job-ok` / `.job-failed` | Server jobs strip on overview |
| `.match-list` | Grid wrapper for match cards |
| `.match-card` | **`<a>`** card linking to detail |
| `.match-row` / `.club-name` / `.club-text` / `.score-pair` / `.score.predicted|.actual` / `.score-label` | Scoreboard row (P vs FT) |
| `.market-strip` / `.market-chip` / `.market-chip-label` / `.market-chip-pick` / `.ft-conf` | Compact market confidence chips |
| `.match-sub` / `.match-meta` / `.league-chip` / `.badge` (+ `.good|.bad|.wait|.muted`) | Meta + verdict badge |
| `.pager` | Previous / Next |
| `.panel` / `.predict-panel` / `.predict-form` | Sections; predict CTA POST form |
| `.scoreboard` / `.club` / `.crest` / `.emblem` | Match detail header |
| `.market-conf-grid` / `.ft-pick` / `.market-conf-card` / `.ft-pick-label` / `.ft-pick-value` | Detail confidence panel |
| `.mls*` / `.ou-*` / `.prob` / `.meter` | Exact-score / O-U / probability meters in engine panel |
| `.charts-panel` / `.chart-grid` / `.chart` / `.hbar-*` / `.vbar-*` / `.cal-chart` / `.chart-axis` | Overview SVG/CSS charts |
| `.table-wrap` / `table` / `.mono` / `.num` | Market + standings tables |
| `.state` / `.state-empty|.state-error|.state-info|.state-loading` / `.state-lead` / `.state-link` / `.state-spinner` | Empty/error/info callouts (`role="status"` or `alert`) |
| `.login-shell` / `.login-error` / `.login-note` | Auth page |
| `.experiment-card` / `.run-experiment-panel` / `.run-experiment-form` / `.advanced-params` | Lab experiment UI |
| `.copy-btn` / `[data-copy]` / `.is-copied` | Match-id copy control |

**Market strip labels (load-bearing copy)**  
`FT 1X2`, `HT`, `Score`, `Goals 2.5`, `Goals 3.5` — from `_prediction_market_confidences`.

**JS**  
Only clipboard helper for `[data-copy]`; no framework, no fetch SPA routing.

---

### 4. Load-Bearing Test Assertions & Invariants (NEVER BREAK)

Sources: `tests/test_dashboard_states.py`, `tests/test_dashboard_auth.py`, `tests/test_match_predict_button.py`, `tests/test_product_ops.py`, `tests/test_laboratory.py` (match list UI).

#### CSS classes / DOM (rename = CI fail)
| Assertion | Test |
|-----------|------|
| `"market-strip"` in match card HTML | `test_dashboard_states` |
| `'class="match-card"'`, `'class="match-list"'`, `'class="match-row"'`, `'score-label">P<'` | `test_laboratory` match list |
| `"charts-panel"`, `"hbar-chart"`, `"vbar-chart"`, `"cal-chart"` | overview charts |
| `'class="state state-empty"'`, `'class="state state-error"'` | not_found / server_error |
| `"predict-panel"`, `action="/matches/{id}/predict"`, `'name="csrf" value="…"'` | predict button |
| `"job-ok"`, `"job-failed"` | product_ops overview jobs |
| `'role="status"'` (empty overview), `'role="alert"'` (error page) | dashboard_states |
| `'aria-label="Evaluation charts"'` **absent** on empty overview | dashboard_states |
| `'name="confidence"'`, option text `"≥ 65%"` | confidence filter form |
| `'name="csrf"'` on login HTML | auth |
| `'name="league"'` (+ La Liga selected) | laboratory list filter |

#### Visible text / copy
| Text | Context |
|------|---------|
| `FT 1X2`, `HT`, `Goals 2.5`, `Goals 3.5`, percent strings like `62%`/`56%`/`72%` | market strip |
| `No matches in the laboratory yet`, `Open Matches`, `Back to Overview` | empty states |
| `Server jobs`, `Tomorrow predictions`, `Standings refresh`, `priced 3`, Cairo clock (not bare ` UTC` in jobs panel) | jobs panel |
| `Market accuracy`, `Full-time accuracy by month`, `FT selection calibration` | charts |
| `Match not found`, `Browse matches` | 404 |
| `Something went wrong` | 500 |
| `No standings stored for this league yet` + (`FOOTBALL_API_KEY` **or** `import-standings`) | standings empty |
| `Sign in`, `Wrong username or password` | auth |
| `Run prediction` / `Re-run prediction` / `Prediction stored` / `Bot score model` | predict flow |
| `1X2` substring on priced cards | laboratory |

#### Routes / status codes / auth behavior
| Behavior | Invariant |
|----------|-----------|
| `GET /healthz` | body `b"ok\n"` |
| Auth on: `GET /` without cookie | redirect 302/303/307; `Location` contains `/login` |
| `GET /login` | 200, Sign in + csrf field |
| POST `/login` good creds | lands on HTML with overview/matches |
| POST `/login` bad password | **401** + body contains wrong-password message |
| Non-loopback without password | `require_auth_for_host` → `SystemExit` matching `DASHBOARD_PASSWORD` |
| POST `/matches/<id>/predict` (authed) | redirects to `/matches/<id>…`; stores prediction |
| `match_detail(..., missing)` | returns `None` (server maps to 404) |

#### Behavioral (non-DOM) still tied to UI helpers
- `_confidence_threshold("all")→None`, `"65"→0.65`, `"0.55"→0.55`
- Confidence filter `_apply` keeps only high-prob FT picks
- `_ft_1x2_from_body` / `_prediction_market_confidences` selection+confidence values for FT/HT/score/OU

**Cursor rule cross-check:** `.cursor/rules/03-laboratory-backtest.mdc` explicitly forbids renaming `match-card` / `market-strip` without updating tests together; non-loopback requires `DASHBOARD_PASSWORD`; server-rendered HTML only (no React/SPA).

---

### 5. Local UI/UX Skills Inventory (`skills/*`)

**Actual location:** `.cursor/skills/` (not a top-level `skills/` directory). Frontmatter + focus as shipped:

| Path | `name` (frontmatter) | Description (trigger) | Core focus |
|------|----------------------|----------------------|------------|
| `.cursor/skills/better-interface/SKILL.md` | `better-interface` | Review a Figma interface across polish, a11y, layout, typography, writing; pin findings as cards. Use in Figma Agent/Make when asked to critique/polish/audit a screen. | Holistic review order: Accessibility → Layout → Writing → Typography → UI polish; keyboard-first; evidence with file:line; modes `quick` (cap 5) / `full` (cap 15); read-only unless asked to implement |
| `.cursor/skills/better-ui/SKILL.md` | `better-ui` | Design-engineering polish: components, animations, hover, shadows, borders, icons, micro-interactions. Triggers on “make it feel better”, stagger, radius, optical alignment, etc. | Concentric radius, optical alignment, interruptible CSS transitions, stagger enters, scale(0.96) press, no `transition: all`, icon stroke matching text weight; defer typography/a11y/layout to sibling skills when named |
| `.cursor/skills/spacing-audit/SKILL.md` | `spacing-audit` | Audit Figma layout vs 4/8pt scale; flag/snap padding/gaps/margins. Use when spacing looks inconsistent or before handoff. | Allowed: 0,4,8,12,16,24,32,40,48,56,64,80,96; only change spacing; preserve tokens/hairlines/optical; summarize before applying |
| `.cursor/skills/ui-ux-pro-max/SKILL.md` | `ui-ux-pro-max` | Comprehensive design guide: 50+ styles, 97 palettes, 57 font pairings, 99 UX guidelines, 25 chart types, 9 stacks. | Priority: a11y → touch → performance → layout → type/color → animation → style → charts; search script `--design-system` / `--stack html-tailwind|…`; pre-delivery checklist (contrast, 44px targets, reduced motion, no emoji icons) |
| `.cursor/skills/ux-ui-auditor/SKILL.md` | `ux-ui-auditor` | Independent EasyCity UX/UI audit vs BRD/screen inventory; coverage gaps, nav defects, a11y risks. Use for design QA / BRD coverage / handoff acceptance / re-audit. **Does not approve other skills’ work.** | Requires EasyCity docs CSVs under `docs/`; writes only under `docs/quality-assurance/`; defect severities; never edits requirements/Figma to pass |

**Also present (not in the user’s five):** `.cursor/skills/taste/SKILL.md` (`design-taste-frontend`) — anti-slop landing/marketing frontend skill with browser gate; not dashboard-specific.

**How to invoke accurately**
- Prefer `@.cursor/skills/<name>` or phrase the skill’s description triggers (e.g. “audit spacing”, “polish this UI”, “Figma critique”, “design system for dashboard”, “BRD coverage audit”).
- For **this** dashboard: prefer `better-ui` + `ui-ux-pro-max` (HTML/CSS string UI) over Figma-only `better-interface` / `spacing-audit`, unless a Figma file is in scope. `ux-ui-auditor` expects EasyCity BRD artifacts that this repo does not carry — do not treat it as the Football-Predictor lab auditor unless those docs exist.

---

### 6. Extension Points for Upcoming Test Lab Views

**Keep the architecture**
1. Add a pure page function in `dashboard.py` (e.g. `promotions_list(connection, query) -> str`) that builds HTML via existing helpers: `_masthead(..., active="…")`, `_filter_form` or a dedicated form, `_state`, `_page`.
2. Register one branch in `Handler.do_GET` (and `do_POST` only if a mutation is required) next to `/experiments`.
3. Add a nav link in `_masthead` with a new `active` key.
4. Prefer **short-lived** `connect(Path("data/lab.db"))` for lab-only tables (open/query/close) so experiment/promotion writes do not pin the long-lived football connection.
5. Mutating POSTs: reuse CSRF + `_authenticated` + 303 redirect flash query params (`*_status` / `*_detail`) — same pattern as predict and experiment run.
6. Charts: stay with inline SVG / CSS bars (see `_overview_charts`) — no Chart.js/npm; keeps first paint under ~2s on local SQLite.
7. Never introduce a SPA build, React, or external CSS bundler; extend the inline `:root` tokens and component classes instead.
8. Before shipping UI renames, update `tests/test_dashboard_states.py` / `test_laboratory.py` / predict/auth tests in the same change; keep `match-card` and `market-strip` stable unless tests move with them.
9. Heavy lab jobs (`lab-run`, `tune-spl`) should remain CLI/background — dashboard should **read** results and optionally enqueue, not run multi-minute grids inside the request thread (ThreadingHTTPServer would block that worker; SQLite write storms risk locks). If UI “Run” stays, run async/job-table + poll, or accept redirect-after-complete only for tiny grids.
10. Suggested clean URLs already partially present: `/experiments`, `/experiments/<id>`; add `/promotions` reading promotion history from lab DB; calibration/comparison can reuse `_experiment_comparisons` / overview chart primitives rather than new dependencies.

**SQLite safety checklist for new pages**
- Reads against `football.db`: use the serve-time connection; avoid long write transactions.
- Writes: commit quickly; call `checkpoint_wal` after bulk lab imports if WAL grows.
- Never hold lab and live connections open across await/sleep in the handler.

---

*Generated from direct inspection of `laboratory/dashboard.py`, `laboratory/auth.py`, `laboratory/__main__.py`, `laboratory/team_display.py`, `laboratory/store.py` (`connect`/`checkpoint_wal`), dashboard-related tests, and `.cursor/skills/*/SKILL.md`.*
