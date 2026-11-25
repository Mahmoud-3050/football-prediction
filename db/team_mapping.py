"""Central team identity mapping and strict provider resolution."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Any

ALLOWED_PROVIDERS = frozenset(
    {
        "football_data",
        "live_football",
        "api_ninjas",
        "manual",
        "understat",
        "fbref",
        "football_data_co_uk",
        "fpl_opta_github",
    }
)

# Explicit SPL mappings between API Ninjas (standings/live) and Live Football API (historical matches)
SPL_NINJAS_TO_LFA_CANONICAL: dict[str, str] = {
    "al fayha": "Feiha",
    "al taawoun": "Al Taawon",
    "al hazem": "Hazm",
    "al diriyah": "Diriyah",
    "al faisaly": "Faisaly",
    "neom sc": "Neom",
    "al qadsiah": "Al-Qadsiah",
    "al orobah": "Orubah",
    "al najma": "Najma",
    "al okhdood": "Al Akhdoud",
    "damac": "Damac FC",
    "al tai": "Al Taee",
    "al adalah": "Adalah",
}


class UnmappedTeamError(LookupError):
    """Raised when a provider team cannot be strictly resolved in team_provider_mappings."""

    def __init__(
        self,
        *,
        provider: str,
        raw_name: str,
        provider_team_id: str | int | None = None,
        competition: str | None = None,
    ) -> None:
        self.provider = provider
        self.raw_name = raw_name
        self.provider_team_id = None if provider_team_id is None else str(provider_team_id)
        self.competition = competition
        pid_flag = f' --provider-id "{self.provider_team_id}"' if self.provider_team_id else ""
        comp_flag = f' --competition "{competition}"' if competition else ""
        msg = (
            f"Unmapped team for provider={provider!r}, provider_team_id={self.provider_team_id!r}, "
            f"raw_name={raw_name!r}. Register it with:\n"
            f'  python -m laboratory map-team --provider {provider} '
            f'--provider-name "{raw_name}" --canonical-name "<CANONICAL_NAME>"{pid_flag}{comp_flag}'
        )
        super().__init__(msg)


def normalize_team_name(name: str) -> str:
    """Deterministic, exact normalization: NFKD accent strip, casefold, collapse spaces."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_provider(provider: str) -> str:
    p = str(provider or "").strip().lower()
    if p not in ALLOWED_PROVIDERS:
        raise ValueError(
            f"Invalid provider {provider!r}; expected one of: {', '.join(sorted(ALLOWED_PROVIDERS))}"
        )
    return p


def has_provider_mappings(connection: sqlite3.Connection, *, provider: str) -> bool:
    p = _validate_provider(provider)
    row = connection.execute(
        "SELECT 1 FROM team_provider_mappings WHERE provider = ? LIMIT 1",
        (p,),
    ).fetchone()
    return row is not None


def resolve_team_id(
    connection: sqlite3.Connection,
    *,
    provider: str,
    raw_name: str,
    provider_team_id: str | int | None = None,
    competition: str | None = None,
) -> str:
    """Strictly resolve a provider team to canonical_teams.team_id.

    1. Match by (provider, str(provider_team_id)) when provided.
    2. Otherwise match by (provider, normalize_team_name(raw_name)).
    3. Never perform fuzzy/substring guessing; raise UnmappedTeamError on miss.
    """
    p = _validate_provider(provider)
    comp_upper = competition.upper() if competition else None

    if provider_team_id is not None and str(provider_team_id).strip() != "":
        pid = str(provider_team_id).strip()
        if comp_upper:
            row = connection.execute(
                """
                SELECT m.team_id
                FROM team_provider_mappings m
                JOIN canonical_teams c ON c.team_id = m.team_id
                WHERE m.provider = ? AND m.provider_team_id = ?
                ORDER BY CASE WHEN UPPER(COALESCE(c.competition, '')) = ? THEN 0 ELSE 1 END, m.id
                LIMIT 1
                """,
                (p, pid, comp_upper),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT team_id FROM team_provider_mappings WHERE provider = ? AND provider_team_id = ? ORDER BY id LIMIT 1",
                (p, pid),
            ).fetchone()
        if row is not None:
            return str(row[0])

    norm = normalize_team_name(raw_name)
    if norm:
        row = connection.execute(
            "SELECT team_id FROM team_provider_mappings WHERE provider = ? AND normalized_name = ? LIMIT 1",
            (p, norm),
        ).fetchone()
        if row is not None:
            return str(row[0])

    raise UnmappedTeamError(
        provider=p,
        raw_name=raw_name,
        provider_team_id=provider_team_id,
        competition=comp_upper,
    )


def register_team_mapping(
    connection: sqlite3.Connection,
    *,
    canonical_name: str,
    provider: str,
    provider_name: str,
    provider_team_id: str | int | None = None,
    competition: str | None = None,
    team_id: str | int | None = None,
) -> str:
    """Idempotently register or update a canonical team and its provider mapping."""
    p = _validate_provider(provider)
    cname = str(canonical_name).strip()
    pname = str(provider_name).strip()
    if not cname or not pname:
        raise ValueError("canonical_name and provider_name must be non-empty.")

    norm = normalize_team_name(pname)
    if not norm:
        raise ValueError(f"provider_name {provider_name!r} normalizes to empty string.")

    pid = None if provider_team_id is None or str(provider_team_id).strip() == "" else str(provider_team_id).strip()
    comp_upper = competition.strip().upper() if competition and str(competition).strip() else None

    resolved_tid: str | None = None if team_id is None else str(team_id).strip()
    if not resolved_tid:
        if comp_upper:
            row = connection.execute(
                """
                SELECT team_id FROM canonical_teams
                WHERE LOWER(canonical_name) = LOWER(?) AND UPPER(COALESCE(competition, '')) = ?
                LIMIT 1
                """,
                (cname, comp_upper),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT team_id FROM canonical_teams WHERE LOWER(canonical_name) = LOWER(?) LIMIT 1",
                (cname,),
            ).fetchone()
        if row is not None:
            resolved_tid = str(row[0])
        elif pid is not None:
            exists = connection.execute(
                "SELECT 1 FROM canonical_teams WHERE team_id = ?", (pid,)
            ).fetchone()
            resolved_tid = pid if exists is None else f"{p}:{pid}"
        else:
            # Deterministic canonical ID for manual entries without provider id
            slug = re.sub(r"\s+", "-", normalize_team_name(cname))
            resolved_tid = f"manual-{slug}"

    connection.execute(
        """
        INSERT INTO canonical_teams (team_id, canonical_name, competition, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            canonical_name = CASE
                WHEN canonical_teams.canonical_name = '' THEN excluded.canonical_name
                ELSE canonical_teams.canonical_name
            END,
            competition = COALESCE(canonical_teams.competition, excluded.competition)
        """,
        (resolved_tid, cname, comp_upper, _utc_now()),
    )

    connection.execute(
        """
        INSERT INTO team_provider_mappings (
            team_id, provider, provider_team_id, provider_name, normalized_name
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(provider, normalized_name) DO UPDATE SET
            team_id = excluded.team_id,
            provider_team_id = COALESCE(excluded.provider_team_id, team_provider_mappings.provider_team_id),
            provider_name = excluded.provider_name
        """,
        (resolved_tid, p, pid, pname, norm),
    )
    return resolved_tid


def backfill_team_mappings(connection: sqlite3.Connection) -> dict[str, int]:
    """Deterministically backfill canonical_teams and team_provider_mappings from DB + known aliases."""
    now = _utc_now()
    canonical_added = 0
    mappings_added = 0

    # Infer primary competition for each team_id from matches
    comp_by_team: dict[str, str] = {}
    match_teams: list[tuple[str, str, str]] = []
    for row in connection.execute(
        """
        SELECT DISTINCT competition, home_id, home_name FROM matches
        UNION
        SELECT DISTINCT competition, away_id, away_name FROM matches
        """
    ).fetchall():
        comp = str(row[0] or "").upper()
        tid = str(row[1])
        tname = str(row[2])
        match_teams.append((comp, tid, tname))
        if tid not in comp_by_team or comp != "CL":
            comp_by_team[tid] = comp

    # 1. Seed canonical_teams from existing teams table (1-to-1 team_id preservation)
    for row in connection.execute("SELECT team_id, name, display_name FROM teams").fetchall():
        tid = str(row[0])
        name = str(row[1])
        comp = comp_by_team.get(tid)
        cur = connection.execute(
            """
            INSERT INTO canonical_teams (team_id, canonical_name, competition, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id) DO NOTHING
            """,
            (tid, name, comp, now),
        )
        canonical_added += int(cur.rowcount or 0)

    # Ensure every team seen in matches also exists in canonical_teams
    for comp, tid, tname in match_teams:
        cur = connection.execute(
            """
            INSERT INTO canonical_teams (team_id, canonical_name, competition, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id) DO NOTHING
            """,
            (tid, tname, comp or None, now),
        )
        canonical_added += int(cur.rowcount or 0)

    # 2. Seed provider mappings from matches (SPL -> live_football; others -> football_data)
    for comp, tid, tname in match_teams:
        provider = "live_football" if comp == "SPL" else "football_data"
        norm = normalize_team_name(tname)
        if not norm:
            continue
        cur = connection.execute(
            """
            INSERT INTO team_provider_mappings (
                team_id, provider, provider_team_id, provider_name, normalized_name
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, normalized_name) DO UPDATE SET
                provider_team_id = COALESCE(team_provider_mappings.provider_team_id, excluded.provider_team_id)
            """,
            (tid, provider, tid, tname, norm),
        )
        mappings_added += int(cur.rowcount or 0)

    # Seed football_data mappings for any remaining teams in `teams`
    for row in connection.execute("SELECT team_id, name FROM teams").fetchall():
        tid = str(row[0])
        tname = str(row[1])
        comp = comp_by_team.get(tid, "")
        provider = "live_football" if comp == "SPL" else "football_data"
        norm = normalize_team_name(tname)
        if not norm:
            continue
        cur = connection.execute(
            """
            INSERT INTO team_provider_mappings (
                team_id, provider, provider_team_id, provider_name, normalized_name
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, normalized_name) DO NOTHING
            """,
            (tid, provider, tid, tname, norm),
        )
        mappings_added += int(cur.rowcount or 0)

    # 3. Seed SPL api_ninjas mappings from standings_rows + explicit SPL_NINJAS_TO_LFA_CANONICAL
    spl_by_norm: dict[str, str] = {}
    for row in connection.execute(
        "SELECT team_id, canonical_name FROM canonical_teams WHERE UPPER(COALESCE(competition, '')) = 'SPL'"
    ).fetchall():
        spl_by_norm[normalize_team_name(str(row[1]))] = str(row[0])

    for ninjas_norm, lfa_name in SPL_NINJAS_TO_LFA_CANONICAL.items():
        lfa_tid = spl_by_norm.get(normalize_team_name(lfa_name))
        if lfa_tid:
            cur = connection.execute(
                """
                INSERT INTO team_provider_mappings (
                    team_id, provider, provider_team_id, provider_name, normalized_name
                ) VALUES (?, 'api_ninjas', NULL, ?, ?)
                ON CONFLICT(provider, normalized_name) DO NOTHING
                """,
                (lfa_tid, lfa_name, ninjas_norm),
            )
            mappings_added += int(cur.rowcount or 0)

    for row in connection.execute(
        "SELECT DISTINCT team_id, team_name FROM standings_rows WHERE competition = 'SPL'"
    ).fetchall():
        ninjas_pid = str(row[0])
        ninjas_name = str(row[1])
        norm = normalize_team_name(ninjas_name)
        target_tid = spl_by_norm.get(norm)
        if not target_tid and norm in SPL_NINJAS_TO_LFA_CANONICAL:
            target_tid = spl_by_norm.get(normalize_team_name(SPL_NINJAS_TO_LFA_CANONICAL[norm]))
        if target_tid:
            cur = connection.execute(
                """
                INSERT INTO team_provider_mappings (
                    team_id, provider, provider_team_id, provider_name, normalized_name
                ) VALUES (?, 'api_ninjas', ?, ?, ?)
                ON CONFLICT(provider, normalized_name) DO UPDATE SET
                    provider_team_id = COALESCE(team_provider_mappings.provider_team_id, excluded.provider_team_id)
                """,
                (target_tid, ninjas_pid, ninjas_name, norm),
            )
            mappings_added += int(cur.rowcount or 0)

    # 4. Seed manual aliases from TEAM_ALIASES and DISPLAY_NAME_BY_OFFICIAL
    try:
        from providers.data_fetcher import TEAM_ALIASES
        from laboratory.team_display import DISPLAY_NAME_BY_OFFICIAL

        all_canon = connection.execute(
            "SELECT team_id, canonical_name FROM canonical_teams"
        ).fetchall()
        canon_by_norm = {normalize_team_name(str(r[1])): str(r[0]) for r in all_canon}

        for official, short in DISPLAY_NAME_BY_OFFICIAL.items():
            tid = canon_by_norm.get(normalize_team_name(official))
            if tid:
                for label in (official, short):
                    norm = normalize_team_name(label)
                    if norm:
                        connection.execute(
                            """
                            INSERT INTO team_provider_mappings (
                                team_id, provider, provider_team_id, provider_name, normalized_name
                            ) VALUES (?, 'manual', NULL, ?, ?)
                            ON CONFLICT(provider, normalized_name) DO NOTHING
                            """,
                            (tid, label, norm),
                        )

        for alias_key, targets in TEAM_ALIASES.items():
            matched_tid = None
            for target_frag in targets:
                t_norm = normalize_team_name(target_frag)
                for c_norm, tid in canon_by_norm.items():
                    if t_norm == c_norm or t_norm in c_norm.split():
                        matched_tid = tid
                        break
                if matched_tid:
                    break
            if matched_tid:
                norm_alias = normalize_team_name(alias_key)
                if norm_alias:
                    connection.execute(
                        """
                        INSERT INTO team_provider_mappings (
                            team_id, provider, provider_team_id, provider_name, normalized_name
                        ) VALUES (?, 'manual', NULL, ?, ?)
                        ON CONFLICT(provider, normalized_name) DO NOTHING
                        """,
                        (matched_tid, alias_key, norm_alias),
                    )
    except ImportError:
        pass

    connection.commit()
    return {"canonical_added": canonical_added, "mappings_added": mappings_added}


def audit_team_mappings(connection: sqlite3.Connection) -> dict[str, Any]:
    """Audit canonical_teams and team_provider_mappings for missing/orphaned entries."""
    unmapped_match_teams = [
        {"competition": r[0], "team_id": r[1], "team_name": r[2]}
        for r in connection.execute(
            """
            SELECT DISTINCT m.competition, m.home_id, m.home_name
            FROM matches m
            LEFT JOIN canonical_teams c ON c.team_id = m.home_id
            WHERE c.team_id IS NULL
            UNION
            SELECT DISTINCT m.competition, m.away_id, m.away_name
            FROM matches m
            LEFT JOIN canonical_teams c ON c.team_id = m.away_id
            WHERE c.team_id IS NULL
            """
        ).fetchall()
    ]
    orphaned_mappings = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM team_provider_mappings m
            LEFT JOIN canonical_teams c ON c.team_id = m.team_id
            WHERE c.team_id IS NULL
            """
        ).fetchone()[0]
    )
    canonical_count = int(connection.execute("SELECT COUNT(*) FROM canonical_teams").fetchone()[0])
    mapping_count = int(connection.execute("SELECT COUNT(*) FROM team_provider_mappings").fetchone()[0])
    return {
        "canonical_teams": canonical_count,
        "provider_mappings": mapping_count,
        "unmapped_match_teams": unmapped_match_teams,
        "orphaned_mappings": orphaned_mappings,
        "ok": len(unmapped_match_teams) == 0 and orphaned_mappings == 0,
    }
