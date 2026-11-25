"""SQLite store for the laboratory. Prediction bodies are insert-only."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from laboratory import EXPERIMENT_ID

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_experiments (
    experiment_id TEXT PRIMARY KEY,
    competition TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    best_config_json TEXT,
    best_metrics_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS lab_experiment_runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics_json TEXT,
    matches_priced INTEGER,
    matches_evaluated INTEGER,
    error TEXT,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (experiment_id) REFERENCES lab_experiments(experiment_id)
);
CREATE TABLE IF NOT EXISTS lab_promotions (
    promotion_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    competition TEXT NOT NULL,
    promotion_type TEXT NOT NULL,
    promoted_model_id TEXT,
    live_code_version TEXT,
    exp_code_version TEXT,
    previous_config_json TEXT,
    new_config_json TEXT,
    bundle_path TEXT,
    evidence_json TEXT,
    status TEXT NOT NULL,
    rejection_reason TEXT,
    promoted_at TEXT NOT NULL,
    rolled_back_at TEXT
);
CREATE TABLE IF NOT EXISTS holdout_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competition TEXT NOT NULL,
    holdout_seasons_json TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    UNIQUE(competition, holdout_seasons_json, experiment_id, model_id)
);
CREATE TABLE IF NOT EXISTS promotion_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promotion_id TEXT,
    promoted_at TEXT NOT NULL,
    competition TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    run_id TEXT,
    previous_model_id TEXT,
    new_model_id TEXT NOT NULL,
    holdout_seasons TEXT NOT NULL,
    n_holdout_matches INTEGER NOT NULL,
    metric_name TEXT NOT NULL,
    baseline_metric REAL,
    candidate_metric REAL,
    mean_diff REAL NOT NULL,
    ci_lower REAL NOT NULL,
    ci_upper REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    backup_path TEXT
);
CREATE TABLE IF NOT EXISTS prospective_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id TEXT,
    match_key TEXT NOT NULL,
    competition TEXT NOT NULL,
    season INTEGER,
    home_team_id TEXT NOT NULL,
    away_team_id TEXT NOT NULL,
    home_team_name TEXT NOT NULL,
    away_team_name TEXT NOT NULL,
    kickoff_utc TEXT NOT NULL,
    predicted_at_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    model_id TEXT NOT NULL,
    stack_json TEXT NOT NULL,
    prob_home REAL NOT NULL,
    prob_draw REAL NOT NULL,
    prob_away REAL NOT NULL,
    prob_over_25 REAL,
    prob_under_25 REAL,
    most_likely_score TEXT NOT NULL,
    most_likely_score_prob REAL,
    display_pick TEXT,
    lambda_home REAL,
    lambda_away REAL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    actual_home_goals INTEGER,
    actual_away_goals INTEGER,
    actual_outcome TEXT,
    ft_1x2_log_loss REAL,
    ft_1x2_brier REAL,
    exact_score_log_loss REAL,
    exact_score_hit INTEGER,
    settled_at_utc TEXT,
    UNIQUE(competition, home_team_id, away_team_id, kickoff_utc)
);
CREATE TABLE IF NOT EXISTS match_xg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competition TEXT NOT NULL,
    season INTEGER NOT NULL,
    kickoff_utc TEXT NOT NULL,
    home_team_id TEXT NOT NULL REFERENCES canonical_teams(team_id),
    away_team_id TEXT NOT NULL REFERENCES canonical_teams(team_id),
    home_xg REAL NOT NULL,
    away_xg REAL NOT NULL,
    source TEXT NOT NULL,
    imported_at_utc TEXT NOT NULL,
    UNIQUE(competition, season, home_team_id, away_team_id, source)
);
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    competition TEXT NOT NULL,
    season TEXT NOT NULL,
    kickoff TEXT NOT NULL,
    home_id TEXT NOT NULL,
    home_name TEXT NOT NULL,
    away_id TEXT NOT NULL,
    away_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_revisions (
    match_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    ft_home INTEGER,
    ft_away INTEGER,
    ht_home INTEGER,
    ht_away INTEGER,
    source TEXT NOT NULL,
    PRIMARY KEY (match_id, revision)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    leakage_ok INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    model_id TEXT NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (experiment_id, snapshot_id, engine, model_id)
);
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    UNIQUE (prediction_id, revision)
);
CREATE TABLE IF NOT EXISTS engine_runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS standings_meta (
    competition TEXT NOT NULL,
    season TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    matchday INTEGER,
    last_updated TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (competition, season)
);
CREATE TABLE IF NOT EXISTS standings_rows (
    competition TEXT NOT NULL,
    season TEXT NOT NULL,
    position INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    played INTEGER,
    won INTEGER,
    draw INTEGER,
    lost INTEGER,
    goals_for INTEGER,
    goals_against INTEGER,
    goal_difference INTEGER,
    points INTEGER,
    form TEXT,
    PRIMARY KEY (competition, season, team_id)
);
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    display_name TEXT,
    crest_url TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_teams (
    team_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    competition TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_provider_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL REFERENCES canonical_teams(team_id),
    provider TEXT NOT NULL,
    provider_team_id TEXT,
    provider_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    UNIQUE(provider, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_team_provider_id
ON team_provider_mappings(provider, provider_team_id);
CREATE TABLE IF NOT EXISTS competitions (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    emblem_url TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);
"""


def ensure_laboratory_schema(connection: sqlite3.Connection) -> None:
    """Create laboratory tables (and light migrations) on an open connection."""

    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(matches)")}
    if "raw_payload_id" not in columns:
        connection.execute("ALTER TABLE matches ADD COLUMN raw_payload_id TEXT")
    team_columns = {row[1] for row in connection.execute("PRAGMA table_info(teams)")}
    if "display_name" not in team_columns:
        connection.execute("ALTER TABLE teams ADD COLUMN display_name TEXT")
    lab_exp_columns = {row[1] for row in connection.execute("PRAGMA table_info(lab_experiments)")}
    if "error" not in lab_exp_columns:
        connection.execute("ALTER TABLE lab_experiments ADD COLUMN error TEXT")
    refresh_team_display_names(connection)
    migrate_strip_fd_match_prefix(connection)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_payloads (
            payload_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            body_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS standings_meta (
            competition TEXT NOT NULL,
            season TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            matchday INTEGER,
            last_updated TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (competition, season)
        );
        CREATE TABLE IF NOT EXISTS standings_rows (
            competition TEXT NOT NULL,
            season TEXT NOT NULL,
            position INTEGER NOT NULL,
            team_id TEXT NOT NULL,
            team_name TEXT NOT NULL,
            played INTEGER,
            won INTEGER,
            draw INTEGER,
            lost INTEGER,
            goals_for INTEGER,
            goals_against INTEGER,
            goal_difference INTEGER,
            points INTEGER,
            form TEXT,
            PRIMARY KEY (competition, season, team_id)
        );
        CREATE TABLE IF NOT EXISTS teams (
            team_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            crest_url TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS competitions (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            emblem_url TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            ok INTEGER NOT NULL,
            summary_json TEXT NOT NULL
        );
        """
    )
    # One-time backfill from cached standings/match JSON when logo tables are empty.
    team_count = connection.execute("SELECT COUNT(*) AS n FROM teams").fetchone()["n"]
    if team_count == 0:
        backfill_logos_from_payloads(connection)
    # Dashboard match-list filters / joins (safe IF NOT EXISTS on every connect).
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_matches_kickoff
            ON matches(kickoff);
        CREATE INDEX IF NOT EXISTS idx_matches_comp_season_kickoff
            ON matches(competition, season, kickoff);
        CREATE INDEX IF NOT EXISTS idx_result_revisions_match
            ON result_revisions(match_id);
        CREATE INDEX IF NOT EXISTS idx_predictions_match_engine_exp
            ON predictions(match_id, engine, experiment_id);
        CREATE INDEX IF NOT EXISTS idx_evaluations_prediction_rev
            ON evaluations(prediction_id, revision);
        CREATE INDEX IF NOT EXISTS idx_engine_runs_match_engine_exp
            ON engine_runs(match_id, engine, experiment_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_match_experiment
            ON snapshots(match_id, experiment_id);
        """
    )


def migrate_strip_fd_match_prefix(connection: sqlite3.Connection) -> int:
    """Drop legacy `fd-` prefix from football-data.org match ids (one-time)."""

    pending = connection.execute(
        "SELECT COUNT(*) AS n FROM matches WHERE match_id LIKE 'fd-%'"
    ).fetchone()["n"]
    if not pending:
        return 0

    connection.execute(
        """
        UPDATE engine_runs
        SET run_id = REPLACE(run_id, ':' || match_id || ':', ':' || substr(match_id, 4) || ':'),
            match_id = substr(match_id, 4)
        WHERE match_id LIKE 'fd-%'
        """
    )
    connection.execute(
        """
        UPDATE snapshots
        SET payload_json = REPLACE(
                payload_json,
                '"match_id": "' || match_id || '"',
                '"match_id": "' || substr(match_id, 4) || '"'
            ),
            match_id = substr(match_id, 4)
        WHERE match_id LIKE 'fd-%'
        """
    )
    connection.execute(
        """
        UPDATE predictions
        SET match_id = substr(match_id, 4)
        WHERE match_id LIKE 'fd-%'
        """
    )
    connection.execute(
        """
        UPDATE result_revisions
        SET match_id = substr(match_id, 4)
        WHERE match_id LIKE 'fd-%'
        """
    )
    connection.execute(
        """
        UPDATE matches
        SET match_id = substr(match_id, 4)
        WHERE match_id LIKE 'fd-%'
        """
    )
    return int(pending)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Long timeout: dashboard + bot + lab jobs share one file; heavy WAL
    # checkpoints after tune/reprice can briefly block writers.
    connection = sqlite3.connect(path, check_same_thread=False, timeout=60.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 60000")
    # WAL lets the dashboard read while the bot writes.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA wal_autocheckpoint = 1000")
    connection.execute("PRAGMA temp_store = MEMORY")
    # Laboratory + bot tables live in the same file.
    from db.database import ensure_shared_schemas

    ensure_shared_schemas(connection)
    return connection


def checkpoint_wal(connection: sqlite3.Connection, *, mode: str = "TRUNCATE") -> dict[str, Any]:
    """Flush the WAL into the main DB so writers stop contending on a huge log."""

    label = str(mode or "TRUNCATE").upper()
    if label not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError("mode must be PASSIVE, FULL, RESTART, or TRUNCATE")
    row = connection.execute(f"PRAGMA wal_checkpoint({label})").fetchone()
    connection.commit()
    return {
        "mode": label,
        "busy": None if row is None else int(row[0]),
        "log": None if row is None else int(row[1]),
        "checkpointed": None if row is None else int(row[2]),
    }


def ensure_experiment(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    *,
    experiment_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    eid = experiment_id or str(config.get("experiment_id") or EXPERIMENT_ID)
    connection.execute(
        """
        INSERT INTO experiments (id, status, config_json, created_at)
        VALUES (?, 'locked', ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (eid, json.dumps(config, sort_keys=True), now),
    )
    connection.commit()


CREST_ROOT = "https://crests.football-data.org"
# Football-Data hosts some competition emblems under short codes, not the API code.
COMPETITION_EMBLEM_FILES = {
    "DED": "ED.png",  # Eredivisie — DED.png 404s
}


def crest_cache_dir() -> Path:
    from db.db_paths import resolve_database_path

    return resolve_database_path(prefer_lab_override=True).parent / "crests"


def crest_url_for_team(
    team_id: str | int,
    crest: str | None = None,
    *,
    fetch: bool = True,
) -> str:
    """Resolve a team crest, preferring a locally cached file under ``/crests/``.

    Set ``fetch=False`` on hot list paths so missing crests do not block on HTTP.
    """

    key = str(team_id)
    local = crest_cache_dir() / f"{key}.png"
    if isinstance(crest, str) and crest.startswith("/crests/"):
        return crest
    if isinstance(crest, str) and crest.startswith("http"):
        if fetch and cache_remote_crest(local, crest):
            return f"/crests/{key}.png"
        return crest
    if local.is_file() and local.stat().st_size > 0:
        return f"/crests/{key}.png"
    remote = f"{CREST_ROOT}/{key}.png"
    if fetch and cache_remote_crest(local, remote):
        return f"/crests/{key}.png"
    return remote


def emblem_remote_url(code: str, emblem: str | None = None) -> str:
    if isinstance(emblem, str) and emblem.startswith("http"):
        return emblem
    key = code.upper()
    filename = COMPETITION_EMBLEM_FILES.get(key, f"{key}.png")
    return f"{CREST_ROOT}/{filename}"


def emblem_url_for_competition(
    code: str,
    emblem: str | None = None,
    *,
    fetch: bool = True,
) -> str:
    """Prefer a locally cached emblem so dashboard pills do not hotlink (and break).

    Set ``fetch=False`` on hot list paths to avoid synchronous HTTP downloads.
    """

    key = code.upper()
    local = crest_cache_dir() / f"{key}.png"
    if local.is_file() and local.stat().st_size > 0:
        return f"/crests/{key}.png"
    remote = emblem_remote_url(key, emblem)
    if fetch and cache_remote_crest(local, remote):
        return f"/crests/{key}.png"
    return remote


def cache_remote_crest(
    dest: Path,
    remote_url: str,
    *,
    force: bool = False,
) -> bool:
    """Download a crest/emblem into ``dest``. Returns True when the file is usable."""

    if not force and dest.is_file() and dest.stat().st_size > 0:
        return True
    if not isinstance(remote_url, str) or not remote_url.startswith("http"):
        return False
    try:
        import urllib.request

        dest.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            remote_url,
            headers={"User-Agent": "FootballPredictorDashboard/1.0"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
        if not payload or not response.headers.get_content_type().startswith("image/"):
            return False
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(dest)
        return True
    except Exception:  # noqa: BLE001 — fall back to remote URL
        return False


def upsert_team(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    name: str,
    crest_url: str | None = None,
    display_name: str | None = None,
    short_name: str | None = None,
) -> None:
    from laboratory.team_display import resolve_display_name

    now = datetime.now(timezone.utc).isoformat()
    url = crest_url_for_team(team_id, crest_url)
    label = (display_name or "").strip() or resolve_display_name(name, short_name)
    connection.execute(
        """
        INSERT INTO teams (team_id, name, display_name, crest_url, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            name = excluded.name,
            display_name = excluded.display_name,
            crest_url = COALESCE(excluded.crest_url, teams.crest_url),
            updated_at = excluded.updated_at
        """,
        (str(team_id), name, label, url, now),
    )


def cache_competition_logos(
    connection: sqlite3.Connection,
    competition: str,
    *,
    league_emblem_url: str | None = None,
) -> dict[str, int]:
    """Download team crests and the league emblem into ``data/crests/`` and update SQLite."""

    from laboratory.football_data import COMPETITION_NAMES

    code = competition.upper()
    team_rows = connection.execute(
        """
        SELECT DISTINCT t.team_id, t.name, t.crest_url
        FROM teams t
        WHERE t.team_id IN (
            SELECT home_id FROM matches WHERE competition = ?
            UNION
            SELECT away_id FROM matches WHERE competition = ?
        )
        ORDER BY t.name
        """,
        (code, code),
    ).fetchall()
    cached = 0
    failed = 0
    for row in team_rows:
        team_id = str(row["team_id"])
        remote = row["crest_url"] if isinstance(row["crest_url"], str) else None
        if not remote or not remote.startswith("http"):
            # Live Football stable path when only the id is known.
            if code == "SPL":
                remote = f"https://live-football-api.com/teams/{team_id}.png"
            else:
                remote = f"{CREST_ROOT}/{team_id}.png"
        local_url = crest_url_for_team(team_id, remote)
        upsert_team(
            connection,
            team_id=team_id,
            name=str(row["name"]),
            crest_url=local_url,
        )
        if local_url.startswith("/crests/"):
            cached += 1
        else:
            failed += 1

    emblem_remote = league_emblem_url
    if not emblem_remote and code == "SPL":
        from providers.live_football_api import lfa_league_id

        emblem_remote = f"https://live-football-api.com/leagues/{lfa_league_id(code)}.png"
    emblem_path = crest_cache_dir() / f"{code}.png"
    if emblem_remote and cache_remote_crest(emblem_path, emblem_remote, force=True):
        emblem_local = f"/crests/{code}.png"
    else:
        emblem_local = emblem_url_for_competition(code, emblem_remote)
    upsert_competition(
        connection,
        code=code,
        name=COMPETITION_NAMES.get(code, code),
        emblem_url=emblem_local if emblem_local.startswith(("http", "/crests/")) else emblem_remote,
    )
    connection.commit()
    return {
        "competition": code,
        "teams": len(team_rows),
        "crests_cached": cached,
        "crests_failed": failed,
        "emblem": 1 if emblem_local.startswith("/crests/") else 0,
    }


def refresh_team_display_names(connection: sqlite3.Connection) -> int:
    """Recompute display_name for every team from the curated short-name map."""

    from laboratory.team_display import resolve_display_name

    rows = connection.execute("SELECT team_id, name FROM teams").fetchall()
    updated = 0
    for row in rows:
        label = resolve_display_name(str(row["name"]))
        connection.execute(
            "UPDATE teams SET display_name = ? WHERE team_id = ?",
            (label, str(row["team_id"])),
        )
        updated += 1
    return updated


def team_display_map(
    connection: sqlite3.Connection,
    team_ids: list[str] | set[str] | None = None,
) -> dict[str, str]:
    if team_ids is not None:
        ids = [str(item) for item in team_ids if item]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""
            SELECT team_id, COALESCE(NULLIF(display_name, ''), name) AS label
            FROM teams WHERE team_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT team_id, COALESCE(NULLIF(display_name, ''), name) AS label
            FROM teams
            """
        ).fetchall()
    return {str(row["team_id"]): str(row["label"]) for row in rows if row["label"]}


def upsert_competition(
    connection: sqlite3.Connection,
    *,
    code: str,
    name: str,
    emblem_url: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    key = code.upper()
    explicit = isinstance(emblem_url, str) and (
        emblem_url.startswith("http") or emblem_url.startswith("/crests/")
    )
    # Always resolve through the local cache when possible.
    remote = emblem_url if (isinstance(emblem_url, str) and emblem_url.startswith("http")) else None
    url = emblem_url_for_competition(key, remote)
    if explicit or remote:
        connection.execute(
            """
            INSERT INTO competitions (code, name, emblem_url, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                emblem_url = excluded.emblem_url,
                updated_at = excluded.updated_at
            """,
            (key, name, url, now),
        )
    else:
        # Seed/fallback must not clobber a better emblem already stored from the API.
        connection.execute(
            """
            INSERT INTO competitions (code, name, emblem_url, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                emblem_url = COALESCE(competitions.emblem_url, excluded.emblem_url),
                updated_at = excluded.updated_at
            """,
            (key, name, url, now),
        )


def team_crest_map(
    connection: sqlite3.Connection,
    team_ids: list[str] | set[str] | None = None,
) -> dict[str, str]:
    if team_ids is not None:
        ids = [str(item) for item in team_ids if item]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT team_id, crest_url FROM teams WHERE team_id IN ({placeholders})",
            ids,
        ).fetchall()
    else:
        rows = connection.execute("SELECT team_id, crest_url FROM teams").fetchall()
    return {
        str(row["team_id"]): str(row["crest_url"])
        for row in rows
        if row["crest_url"]
    }


def competition_emblem_map(
    connection: sqlite3.Connection,
    codes: list[str] | set[str] | None = None,
    *,
    fetch: bool = True,
) -> dict[str, str]:
    if codes is not None:
        keys = [str(item).upper() for item in codes if item]
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = connection.execute(
            f"SELECT code, emblem_url FROM competitions WHERE code IN ({placeholders})",
            keys,
        ).fetchall()
    else:
        rows = connection.execute("SELECT code, emblem_url FROM competitions").fetchall()
    resolved: dict[str, str] = {}
    for row in rows:
        code = str(row["code"]).upper()
        stored = str(row["emblem_url"]) if row["emblem_url"] else None
        if stored and stored.startswith("/crests/"):
            resolved[code] = stored
            continue
        remote = stored if stored and stored.startswith("http") else None
        resolved[code] = emblem_url_for_competition(code, remote, fetch=fetch)
    # Ensure requested codes still resolve even when the competitions row is missing.
    if codes is not None:
        for code in keys:
            resolved.setdefault(code, emblem_url_for_competition(code, fetch=fetch))
    return resolved


def backfill_logos_from_payloads(connection: sqlite3.Connection) -> dict[str, int]:
    """Extract crest/emblem URLs from cached standings and match JSON."""

    from laboratory.football_data import COMPETITION_NAMES

    teams = 0
    competitions = 0
    for row in connection.execute("SELECT competition, payload_json FROM standings_meta"):
        try:
            body = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        code = str(row["competition"]).upper()
        competition = body.get("competition") if isinstance(body, dict) else None
        emblem = None
        name = COMPETITION_NAMES.get(code, code)
        if isinstance(competition, dict):
            if isinstance(competition.get("emblem"), str):
                emblem = competition["emblem"]
            if isinstance(competition.get("name"), str):
                name = competition["name"]
        upsert_competition(connection, code=code, name=name, emblem_url=emblem)
        competitions += 1
        for block in (body.get("standings") or []) if isinstance(body, dict) else []:
            if not isinstance(block, dict):
                continue
            for entry in block.get("table") or []:
                if not isinstance(entry, dict):
                    continue
                team = entry.get("team") or {}
                if not isinstance(team.get("id"), int) or not isinstance(team.get("name"), str):
                    continue
                crest = team.get("crest") if isinstance(team.get("crest"), str) else None
                upsert_team(
                    connection,
                    team_id=str(team["id"]),
                    name=team["name"],
                    crest_url=crest,
                    short_name=team.get("shortName")
                    if isinstance(team.get("shortName"), str)
                    else None,
                )
                teams += 1
    for row in connection.execute("SELECT body_json FROM raw_payloads"):
        try:
            body = json.loads(row["body_json"])
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            continue
        competition = body.get("competition") if isinstance(body.get("competition"), dict) else None
        if competition and isinstance(competition.get("code"), str):
            code = competition["code"].upper()
            upsert_competition(
                connection,
                code=code,
                name=str(competition.get("name") or COMPETITION_NAMES.get(code, code)),
                emblem_url=competition.get("emblem")
                if isinstance(competition.get("emblem"), str)
                else None,
            )
            competitions += 1
        for match in body.get("matches") or []:
            if not isinstance(match, dict):
                continue
            match_comp = match.get("competition") if isinstance(match.get("competition"), dict) else None
            if match_comp and isinstance(match_comp.get("code"), str):
                code = match_comp["code"].upper()
                upsert_competition(
                    connection,
                    code=code,
                    name=str(match_comp.get("name") or COMPETITION_NAMES.get(code, code)),
                    emblem_url=match_comp.get("emblem")
                    if isinstance(match_comp.get("emblem"), str)
                    else None,
                )
                competitions += 1
            for side in ("homeTeam", "awayTeam"):
                team = match.get(side) or {}
                if not isinstance(team.get("id"), int) or not isinstance(team.get("name"), str):
                    continue
                upsert_team(
                    connection,
                    team_id=str(team["id"]),
                    name=team["name"],
                    crest_url=team.get("crest") if isinstance(team.get("crest"), str) else None,
                    short_name=team.get("shortName")
                    if isinstance(team.get("shortName"), str)
                    else None,
                )
                teams += 1
    # Ensure known league emblems exist even without payload competition objects.
    for code, name in COMPETITION_NAMES.items():
        upsert_competition(connection, code=code, name=name)
        competitions += 1
    # Seed team crests from standings_rows / matches ids when JSON had no crest field.
    for row in connection.execute(
        "SELECT DISTINCT team_id, team_name FROM standings_rows WHERE team_id IS NOT NULL"
    ):
        upsert_team(connection, team_id=str(row["team_id"]), name=str(row["team_name"]))
        teams += 1
    for row in connection.execute(
        """
        SELECT home_id AS team_id, home_name AS name FROM matches
        UNION
        SELECT away_id, away_name FROM matches
        """
    ):
        upsert_team(connection, team_id=str(row["team_id"]), name=str(row["name"]))
        teams += 1
    connection.commit()
    return {"teams": teams, "competitions": competitions}


def upsert_match(connection: sqlite3.Connection, match: dict[str, Any]) -> None:
    competition = str(match.get("competition") or "PL")
    connection.execute(
        """
        INSERT INTO matches (
            match_id, dataset_version, competition, season, kickoff,
            home_id, home_name, away_id, away_name, raw_payload_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            dataset_version = excluded.dataset_version,
            competition = excluded.competition,
            season = excluded.season,
            kickoff = excluded.kickoff,
            home_id = excluded.home_id,
            home_name = excluded.home_name,
            away_id = excluded.away_id,
            away_name = excluded.away_name,
            raw_payload_id = excluded.raw_payload_id
        """,
        (
            match["match_id"],
            match["dataset_version"],
            competition,
            match["season"],
            match["kickoff"],
            match["home_id"],
            match["home_name"],
            match["away_id"],
            match["away_name"],
            match.get("raw_payload_id"),
        ),
    )


def save_raw_payload(connection: sqlite3.Connection, payload_id: str, body: str) -> None:
    connection.execute(
        """
        INSERT INTO raw_payloads (payload_id, provider, fetched_at, body_json)
        VALUES (?, 'football-data.org', ?, ?)
        ON CONFLICT(payload_id) DO NOTHING
        """,
        (payload_id, datetime.now(timezone.utc).isoformat(), body),
    )


def append_result_if_changed(
    connection: sqlite3.Connection,
    match_id: str,
    result: dict[str, Any],
    source: str,
) -> tuple[int, bool]:
    latest = connection.execute(
        """
        SELECT revision, ft_home, ft_away, ht_home, ht_away
        FROM result_revisions
        WHERE match_id = ?
        ORDER BY revision DESC
        LIMIT 1
        """,
        (match_id,),
    ).fetchone()
    incoming = (
        result.get("ft_home"),
        result.get("ft_away"),
        result.get("ht_home"),
        result.get("ht_away"),
    )
    if latest is not None and incoming == (
        latest["ft_home"],
        latest["ft_away"],
        latest["ht_home"],
        latest["ht_away"],
    ):
        return int(latest["revision"]), False
    return append_result(connection, match_id, result, source), True


def delete_predictions_for_unfinished_matches(connection: sqlite3.Connection) -> dict[str, int]:
    """Remove stored predictions (and related rows) for matches with no full-time result.

    Fixtures may remain. Predictions are created only for finished walk-forward
    matches; live pricing uses /predict or the 1-day pre-round job.
    """

    match_ids = [
        row["match_id"]
        for row in connection.execute(
            """
            SELECT m.match_id
            FROM matches m
            LEFT JOIN result_revisions r
              ON r.match_id = m.match_id
             AND r.revision = (
                SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id
             )
            WHERE r.ft_home IS NULL OR r.ft_away IS NULL
            """
        )
    ]
    if not match_ids:
        return {"matches": 0, "predictions": 0, "evaluations": 0, "snapshots": 0, "runs": 0}
    marks = ",".join("?" for _ in match_ids)
    prediction_ids = [
        row["prediction_id"]
        for row in connection.execute(
            f"SELECT prediction_id FROM predictions WHERE match_id IN ({marks})",
            match_ids,
        )
    ]
    evaluations = 0
    if prediction_ids:
        prediction_marks = ",".join("?" for _ in prediction_ids)
        evaluations = connection.execute(
            f"DELETE FROM evaluations WHERE prediction_id IN ({prediction_marks})",
            prediction_ids,
        ).rowcount
    predictions = connection.execute(
        f"DELETE FROM predictions WHERE match_id IN ({marks})",
        match_ids,
    ).rowcount
    snapshots = connection.execute(
        f"DELETE FROM snapshots WHERE match_id IN ({marks})",
        match_ids,
    ).rowcount
    runs = connection.execute(
        f"DELETE FROM engine_runs WHERE match_id IN ({marks})",
        match_ids,
    ).rowcount
    connection.commit()
    return {
        "matches": len(match_ids),
        "predictions": predictions,
        "evaluations": evaluations,
        "snapshots": snapshots,
        "runs": runs,
    }


def remove_datasets_with_prefix(connection: sqlite3.Connection, prefix: str) -> int:
    match_ids = [
        row["match_id"]
        for row in connection.execute(
            "SELECT match_id FROM matches WHERE dataset_version LIKE ?",
            (prefix + "%",),
        )
    ]
    if not match_ids:
        return 0
    marks = ",".join("?" for _ in match_ids)
    prediction_ids = [
        row["prediction_id"]
        for row in connection.execute(
            f"SELECT prediction_id FROM predictions WHERE match_id IN ({marks})",
            match_ids,
        )
    ]
    if prediction_ids:
        prediction_marks = ",".join("?" for _ in prediction_ids)
        connection.execute(
            f"DELETE FROM evaluations WHERE prediction_id IN ({prediction_marks})",
            prediction_ids,
        )
    connection.execute(f"DELETE FROM predictions WHERE match_id IN ({marks})", match_ids)
    connection.execute(f"DELETE FROM snapshots WHERE match_id IN ({marks})", match_ids)
    connection.execute(f"DELETE FROM engine_runs WHERE match_id IN ({marks})", match_ids)
    connection.execute(f"DELETE FROM result_revisions WHERE match_id IN ({marks})", match_ids)
    connection.execute(f"DELETE FROM matches WHERE match_id IN ({marks})", match_ids)
    return len(match_ids)


def append_result(connection: sqlite3.Connection, match_id: str, result: dict[str, Any], source: str) -> int:
    current = connection.execute(
        "SELECT COALESCE(MAX(revision), 0) AS revision FROM result_revisions WHERE match_id = ?",
        (match_id,),
    ).fetchone()
    revision = int(current["revision"]) + 1
    connection.execute(
        """
        INSERT INTO result_revisions (
            match_id, revision, ft_home, ft_away, ht_home, ht_away, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            revision,
            result.get("ft_home"),
            result.get("ft_away"),
            result.get("ht_home"),
            result.get("ht_away"),
            source,
        ),
    )
    return revision


def _slim_snapshot_json(payload: dict[str, Any]) -> str:
    """Serialize snapshot metadata without the redundant prior_matches array."""
    slim = dict(payload)
    slim.pop("prior_matches", None)
    return json.dumps(slim, sort_keys=True)


def save_snapshot(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    experiment_id: str = EXPERIMENT_ID,
) -> str:
    snapshot_id = f"{experiment_id}:{payload['payload_hash']}"
    connection.execute(
        """
        INSERT INTO snapshots (snapshot_id, experiment_id, match_id, payload_json, leakage_ok)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id) DO NOTHING
        """,
        (
            snapshot_id,
            experiment_id,
            payload["match_id"],
            _slim_snapshot_json(payload),
            1 if payload["leakage_ok"] else 0,
        ),
    )
    return snapshot_id


def delete_match_engine_outputs(
    connection: sqlite3.Connection,
    *,
    match_id: str,
    experiment_id: str,
    engine: str,
) -> None:
    """Remove stored prediction/evaluation/run rows for one match engine slot."""

    prediction_ids = [
        row["prediction_id"]
        for row in connection.execute(
            """
            SELECT prediction_id FROM predictions
            WHERE match_id = ? AND experiment_id = ? AND engine = ?
            """,
            (match_id, experiment_id, engine),
        )
    ]
    if prediction_ids:
        marks = ",".join("?" for _ in prediction_ids)
        connection.execute(
            f"DELETE FROM evaluations WHERE prediction_id IN ({marks})",
            prediction_ids,
        )
        connection.execute(
            f"DELETE FROM predictions WHERE prediction_id IN ({marks})",
            prediction_ids,
        )
    connection.execute(
        """
        DELETE FROM engine_runs
        WHERE match_id = ? AND experiment_id = ? AND engine = ?
        """,
        (match_id, experiment_id, engine),
    )


def delete_experiment_engine_outputs(
    connection: sqlite3.Connection,
    *,
    experiment_id: str,
    engine: str,
) -> int:
    """Remove all prediction/evaluation/run rows for one experiment engine."""

    prediction_ids = [
        row["prediction_id"]
        for row in connection.execute(
            """
            SELECT prediction_id FROM predictions
            WHERE experiment_id = ? AND engine = ?
            """,
            (experiment_id, engine),
        )
    ]
    if prediction_ids:
        marks = ",".join("?" for _ in prediction_ids)
        connection.execute(
            f"DELETE FROM evaluations WHERE prediction_id IN ({marks})",
            prediction_ids,
        )
        connection.execute(
            f"DELETE FROM predictions WHERE prediction_id IN ({marks})",
            prediction_ids,
        )
    connection.execute(
        "DELETE FROM engine_runs WHERE experiment_id = ? AND engine = ?",
        (experiment_id, engine),
    )
    connection.commit()
    return len(prediction_ids)


def insert_prediction(
    connection: sqlite3.Connection,
    *,
    match_id: str,
    snapshot_id: str,
    engine: str,
    model_id: str,
    body: dict[str, Any],
    experiment_id: str = EXPERIMENT_ID,
) -> str | None:
    prediction_id = f"{experiment_id}:{snapshot_id}:{engine}:{model_id}"
    cursor = connection.execute(
        """
        INSERT INTO predictions (
            prediction_id, experiment_id, match_id, snapshot_id, engine,
            model_id, body_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(prediction_id) DO NOTHING
        """,
        (
            prediction_id,
            experiment_id,
            match_id,
            snapshot_id,
            engine,
            model_id,
            json.dumps(body, sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    if cursor.rowcount == 0:
        return None
    return prediction_id


def insert_evaluation(
    connection: sqlite3.Connection,
    prediction_id: str,
    revision: int,
    metrics: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO evaluations (evaluation_id, prediction_id, revision, metrics_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(evaluation_id) DO NOTHING
        """,
        (
            f"{prediction_id}:r{revision}",
            prediction_id,
            revision,
            json.dumps(metrics, sort_keys=True),
        ),
    )


def record_run(
    connection: sqlite3.Connection,
    *,
    match_id: str,
    engine: str,
    status: str,
    detail: str,
    experiment_id: str = EXPERIMENT_ID,
) -> None:
    connection.execute(
        """
        INSERT INTO engine_runs (run_id, experiment_id, match_id, engine, status, detail)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, detail = excluded.detail
        """,
        (
            f"{experiment_id}:{match_id}:{engine}",
            experiment_id,
            match_id,
            engine,
            status,
            detail,
        ),
    )


def list_matches(
    connection: sqlite3.Connection,
    *,
    competition: str | None = None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT m.*, r.ft_home, r.ft_away, r.ht_home, r.ht_away, r.revision
        FROM matches m
        LEFT JOIN result_revisions r
          ON r.match_id = m.match_id
         AND r.revision = (
            SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id
         )
    """
    params: tuple[Any, ...] = ()
    if competition:
        sql += " WHERE m.competition = ?"
        params = (competition.upper(),)
    sql += " ORDER BY m.kickoff, m.match_id"
    return list(connection.execute(sql, params))


def list_competitions(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT DISTINCT competition
        FROM matches
        ORDER BY competition
        """
    ).fetchall()
    return [str(row["competition"]) for row in rows]


def prediction_body(connection: sqlite3.Connection, prediction_id: str) -> str:
    row = connection.execute(
        "SELECT body_json FROM predictions WHERE prediction_id = ?",
        (prediction_id,),
    ).fetchone()
    if row is None:
        raise KeyError(prediction_id)
    return str(row["body_json"])


def record_job_run(
    connection: sqlite3.Connection,
    *,
    name: str,
    started_at: str,
    finished_at: str,
    ok: bool,
    summary: dict[str, Any],
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO job_runs (name, started_at, finished_at, ok, summary_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            name,
            started_at,
            finished_at,
            1 if ok else 0,
            json.dumps(summary, sort_keys=True, default=str),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def latest_job_runs(
    connection: sqlite3.Connection,
    *,
    names: tuple[str, ...] = ("tomorrow", "standings"),
) -> list[sqlite3.Row]:
    """Latest run per job name, newest first among the selected names."""

    rows: list[sqlite3.Row] = []
    for name in names:
        row = connection.execute(
            """
            SELECT id, name, started_at, finished_at, ok, summary_json
            FROM job_runs
            WHERE name = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (name,),
        ).fetchone()
        if row is not None:
            rows.append(row)
    return rows


# =====================================================================
# Lab Experiment Storage
# =====================================================================

def create_lab_experiment(
    connection: sqlite3.Connection,
    experiment_id: str,
    competition: str,
    config: dict[str, Any],
) -> None:
    """Register a new lab experiment."""
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO lab_experiments (
            experiment_id, competition, config_json, status, created_at
        ) VALUES (?, ?, ?, 'running', ?)
        ON CONFLICT(experiment_id) DO NOTHING
        """,
        (experiment_id, competition, json.dumps(config, sort_keys=True), now),
    )
    connection.commit()


def get_lab_experiment(
    connection: sqlite3.Connection,
    experiment_id: str,
) -> dict[str, Any] | None:
    """Fetch a lab experiment by ID."""
    row = connection.execute(
        """
        SELECT *
        FROM lab_experiments WHERE experiment_id = ?
        """,
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    keys = row.keys()
    return {
        "experiment_id": row["experiment_id"],
        "competition": row["competition"],
        "config": json.loads(row["config_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "best_config": json.loads(row["best_config_json"]) if row["best_config_json"] else None,
        "best_metrics": json.loads(row["best_metrics_json"]) if row["best_metrics_json"] else None,
        "error": row["error"] if "error" in keys else None,
    }


def fail_lab_experiment(
    connection: sqlite3.Connection,
    experiment_id: str,
    error: str,
) -> None:
    """Mark experiment as failed with error message / traceback."""
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        UPDATE lab_experiments
        SET status = 'failed', completed_at = ?, error = ?
        WHERE experiment_id = ?
        """,
        (now, error, experiment_id),
    )
    connection.commit()


def delete_lab_experiment(
    connection: sqlite3.Connection,
    experiment_id: str,
    *,
    cascade_predictions: bool = True,
) -> int:
    """Delete a lab experiment and its runs. Returns rows deleted."""
    def _table_exists(table_name: str) -> bool:
        r = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return r is not None

    deleted = 0
    if _table_exists("lab_experiment_runs"):
        cur = connection.execute(
            "DELETE FROM lab_experiment_runs WHERE experiment_id = ?",
            (experiment_id,),
        )
        deleted += cur.rowcount

    if cascade_predictions:
        if _table_exists("evaluations") and _table_exists("predictions"):
            connection.execute(
                """DELETE FROM evaluations WHERE prediction_id IN (
                    SELECT prediction_id FROM predictions WHERE experiment_id = ?
                )""",
                (experiment_id,),
            )
        if _table_exists("predictions"):
            connection.execute(
                "DELETE FROM predictions WHERE experiment_id = ?",
                (experiment_id,),
            )
        if _table_exists("snapshots"):
            connection.execute(
                "DELETE FROM snapshots WHERE experiment_id = ?",
                (experiment_id,),
            )

    if _table_exists("lab_experiments"):
        cur = connection.execute(
            "DELETE FROM lab_experiments WHERE experiment_id = ?",
            (experiment_id,),
        )
        deleted += cur.rowcount

    connection.commit()
    return deleted


def reset_lab_experiment_for_rerun(
    connection: sqlite3.Connection,
    experiment_id: str,
    *,
    cascade_predictions: bool = True,
) -> int:
    """Clear runs/outputs and mark the experiment ``running`` for a fresh lab-run.

    Keeps the ``lab_experiments`` row and its stored config. Returns rows deleted
    from ``lab_experiment_runs`` (0 if the experiment does not exist).
    """

    def _table_exists(table_name: str) -> bool:
        r = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return r is not None

    if not _table_exists("lab_experiments"):
        return 0
    exists = connection.execute(
        "SELECT 1 FROM lab_experiments WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    if exists is None:
        return 0

    deleted_runs = 0
    if _table_exists("lab_experiment_runs"):
        cur = connection.execute(
            "DELETE FROM lab_experiment_runs WHERE experiment_id = ?",
            (experiment_id,),
        )
        deleted_runs = int(cur.rowcount)

    if cascade_predictions:
        if _table_exists("evaluations") and _table_exists("predictions"):
            connection.execute(
                """DELETE FROM evaluations WHERE prediction_id IN (
                    SELECT prediction_id FROM predictions WHERE experiment_id = ?
                )""",
                (experiment_id,),
            )
        if _table_exists("predictions"):
            connection.execute(
                "DELETE FROM predictions WHERE experiment_id = ?",
                (experiment_id,),
            )
        if _table_exists("snapshots"):
            connection.execute(
                "DELETE FROM snapshots WHERE experiment_id = ?",
                (experiment_id,),
            )

    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        UPDATE lab_experiments
        SET status = 'running',
            created_at = ?,
            completed_at = NULL,
            best_config_json = NULL,
            best_metrics_json = NULL,
            error = NULL
        WHERE experiment_id = ?
        """,
        (now, experiment_id),
    )
    connection.commit()
    return deleted_runs


def available_competitions(connection: sqlite3.Connection) -> list[str]:
    """Distinct competition codes from matches table, or fallback defaults."""
    from laboratory.football_data import COMPETITION_NAMES

    try:
        rows = connection.execute(
            "SELECT DISTINCT competition FROM matches ORDER BY competition"
        ).fetchall()
        codes = [str(row["competition"]) for row in rows if row["competition"]]
        if codes:
            return codes
    except sqlite3.OperationalError:
        pass
    return list(COMPETITION_NAMES.keys())


def available_seasons(connection: sqlite3.Connection) -> list[str]:
    """Distinct seasons from matches table, or fallback defaults."""
    try:
        rows = connection.execute(
            "SELECT DISTINCT season FROM matches ORDER BY season DESC"
        ).fetchall()
        szns = [str(row["season"]) for row in rows if row["season"]]
        if szns:
            return szns
    except sqlite3.OperationalError:
        pass
    from datetime import datetime
    now_year = datetime.now().year
    return [str(now_year - 2), str(now_year - 1), str(now_year)]


def list_lab_experiments(
    connection: sqlite3.Connection,
    competition: str | None = None,
) -> list[dict[str, Any]]:
    """List all lab experiments, optionally filtered by competition."""
    sql = """
        SELECT experiment_id, competition, status, created_at, completed_at
        FROM lab_experiments
    """
    params: tuple[Any, ...] = ()
    if competition:
        sql += " WHERE competition = ?"
        params = (competition.upper(),)
    sql += " ORDER BY created_at DESC"
    rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def complete_lab_experiment(
    connection: sqlite3.Connection,
    experiment_id: str,
    best_config: dict[str, Any] | None,
    best_metrics: dict[str, Any] | None,
) -> None:
    """Mark experiment as completed with winning config and metrics."""
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        UPDATE lab_experiments
        SET status = 'completed', completed_at = ?,
            best_config_json = ?, best_metrics_json = ?
        WHERE experiment_id = ?
        """,
        (
            now,
            json.dumps(best_config, sort_keys=True) if best_config else None,
            json.dumps(best_metrics, sort_keys=True) if best_metrics else None,
            experiment_id,
        ),
    )
    connection.commit()


def upsert_lab_experiment_run(
    connection: sqlite3.Connection,
    run_id: str,
    experiment_id: str,
    model_id: str,
    config: dict[str, Any],
    status: str = "pending",
    metrics: dict[str, Any] | None = None,
    matches_priced: int | None = None,
    matches_evaluated: int | None = None,
    error: str | None = None,
) -> None:
    """Insert or update a lab experiment run."""
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO lab_experiment_runs (
            run_id, experiment_id, model_id, config_json, status,
            metrics_json, matches_priced, matches_evaluated, error,
            started_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status = excluded.status,
            metrics_json = excluded.metrics_json,
            matches_priced = excluded.matches_priced,
            matches_evaluated = excluded.matches_evaluated,
            error = excluded.error,
            completed_at = excluded.completed_at
        """,
        (
            run_id,
            experiment_id,
            model_id,
            json.dumps(config, sort_keys=True),
            status,
            json.dumps(metrics, sort_keys=True) if metrics else None,
            matches_priced,
            matches_evaluated,
            error,
            now if status != "pending" else None,
            now if status in ("completed", "failed") else None,
        ),
    )
    connection.commit()


def get_lab_experiment_runs(
    connection: sqlite3.Connection,
    experiment_id: str,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all runs for an experiment, optionally filtered by status."""
    sql = """
        SELECT run_id, experiment_id, model_id, config_json, status,
               metrics_json, matches_priced, matches_evaluated, error,
               started_at, completed_at
        FROM lab_experiment_runs
        WHERE experiment_id = ?
    """
    params: list[Any] = [experiment_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY started_at"
    rows = connection.execute(sql, params).fetchall()
    return [
        {
            "run_id": row["run_id"],
            "experiment_id": row["experiment_id"],
            "model_id": row["model_id"],
            "config": json.loads(row["config_json"]),
            "status": row["status"],
            "metrics": json.loads(row["metrics_json"]) if row["metrics_json"] else None,
            "matches_priced": row["matches_priced"],
            "matches_evaluated": row["matches_evaluated"],
            "error": row["error"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }
        for row in rows
    ]
