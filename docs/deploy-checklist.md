# Pre-production checklist (dashboard + DB)

Use this before exposing the laboratory dashboard and shared SQLite database.

## Ready in code

- [x] Single shared DB: `DATABASE_PATH` → `data/football.db` (bot + lab tables)
- [x] Form login (`/login`) + HttpOnly session cookie via `DASHBOARD_USER` / `DASHBOARD_PASSWORD`
- [x] Fail-closed: binding outside loopback without a password exits
- [x] Unauthenticated `/healthz` for uptime checks only
- [x] Security headers: `Cache-Control`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`
- [x] SQLite WAL + busy timeout for bot write / dashboard read concurrency
- [x] `.env`, `data/`, `*.db`, `.DS_Store`, and `.cursor/` are gitignored
- [x] Auth unit/integration tests
- [x] Example systemd units in `docs/systemd/`
- [x] DB backup script: `scripts/backup_db.sh`

## You must configure before public deploy

- [x] Set a strong `DASHBOARD_PASSWORD` and `DASHBOARD_SESSION_SECRET` in local `.env` (never commit `.env`)
- [ ] Bind with `DASHBOARD_HOST=0.0.0.0` **only behind** a reverse proxy (Caddy/nginx) that terminates HTTPS
- [ ] Do **not** expose port 8765 raw on the internet; proxy with TLS
- [ ] Confirm `.env` is never committed; **rotate** `BOT_TOKEN` / `FOOTBALL_API_KEY` / `GEMINI_API_KEY` if they were shared in chat or screenshots
- [ ] Copy `data/football.db` to the host (or restore from backup) and set `DATABASE_PATH`
- [ ] Run `pytest` on the deploy machine/image before cutover
- [ ] Install process managers from `docs/systemd/` (bot, jobs, dashboard)
- [ ] Install the backup **timer** (`football-backup.timer`) so SQLite is snapshotted daily
- [ ] Keep the dashboard behind HTTPS proxy only (`DASHBOARD_HOST=127.0.0.1`); never publish :8765 on WAN
- [ ] Disk headroom: keep free space ≥ 2× DB size for vacuum/backup
- [ ] Firewall: allow only 80/443 (proxy) and SSH; deny direct 8765 from WAN
- [ ] If the Telegram bot also runs on the same host, point it at the same `DATABASE_PATH`
- [ ] Behind HTTPS set `DASHBOARD_COOKIE_SECURE=1`

## HTTPS / exposure (required)

Keep the Python server on loopback and terminate TLS at the proxy:

```bash
# App (.env)
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8765
DASHBOARD_COOKIE_SECURE=1
```

Caddy example:

```caddyfile
lab.example.com {
  reverse_proxy 127.0.0.1:8765
}
```

nginx example:

```nginx
server {
  listen 443 ssl http2;
  server_name lab.example.com;
  # ssl_certificate …;
  location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
```

## Process managers

Example units live in `docs/systemd/`. On the host:

```bash
sudo cp docs/systemd/football-*.service docs/systemd/football-*.timer /etc/systemd/system/
# Edit WorkingDirectory / EnvironmentFile / User to match the host
sudo systemctl daemon-reload
sudo systemctl enable --now football-bot football-jobs football-dashboard
sudo systemctl enable --now football-backup.timer
systemctl list-timers football-backup.timer
```

Default path: reverse proxy terminates TLS → `127.0.0.1:8765`. Do not bind the dashboard to a public interface without that proxy.

## Database backup

Preferred: systemd timer (daily 03:15, see `docs/systemd/football-backup.timer`).

```bash
chmod +x scripts/backup_db.sh
# Manual run
./scripts/backup_db.sh
# Or via unit
sudo systemctl start football-backup.service
# Optional: BACKUP_DIR=/var/backups/football BACKUP_KEEP=14
```

Cron fallback (only if systemd timers are unavailable):

```cron
15 3 * * * cd /opt/football-predictor && ./scripts/backup_db.sh >>/var/log/football-backup.log 2>&1
```

## Suggested production command

```bash
# .env
DATABASE_PATH=data/football.db
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=<long-random-secret>
DASHBOARD_SESSION_SECRET=<long-random-secret>
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8765
DASHBOARD_COOKIE_SECURE=1

python -m laboratory serve --host 127.0.0.1 --port 8765
```

## Smoke test after deploy

```bash
curl -fsS https://your.domain/healthz   # expect: ok
curl -I https://your.domain/           # expect: 303 → /login
curl -I https://your.domain/login      # expect: 200 Sign in page
pytest
```

## Secrets hygiene

- `.env` is gitignored; never force-add it.
- If API tokens appeared in chat, logs, or screenshots, rotate them at BotFather, football-data.org, and Google AI Studio before public traffic.
- Prefer a unique `DASHBOARD_SESSION_SECRET` so cookie signatures survive password changes.

## Not in scope yet (accept or schedule)

- [ ] Native HTTPS in the Python server (use a reverse proxy instead)
- [ ] Multi-user accounts / SSO
- [ ] Managed Postgres migration (SQLite is intentional for now)
- [ ] Automated off-site backup verification restore drill
