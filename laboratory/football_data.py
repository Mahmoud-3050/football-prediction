"""Import competition matches from Football-Data.org.

Raw responses are stored whole. Parsed rows keep the provider match id.
Results are a separate revision and are not written into the pre-match snapshot.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

SOURCE = "football-data.org"
NINJAS_SOURCE = "api-ninjas.com"
LIVE_FOOTBALL_SOURCE = "live-football-api.com"
API_ROOT = "https://api.football-data.org/v4/competitions/{competition}/matches"

COMPETITION_NAMES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "FL1": "Ligue 1",
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "PPL": "Liga Portugal",
    "DED": "Eredivisie",
    "CL": "Champions League",
    "SPL": "Saudi Pro League",
}


def dataset_version(competition: str) -> str:
    code = competition.upper()
    from providers.live_football_api import is_live_football_competition

    if is_live_football_competition(code):
        return f"live-football-{code.lower()}"
    from providers.api_ninjas import is_ninjas_competition

    if is_ninjas_competition(code):
        return f"api-ninjas-{code.lower()}"
    return f"football-data-{competition.lower()}"


def fetch_season(
    api_key: str,
    season: int,
    *,
    competition: str = "PL",
    ninjas_api_key: str | None = None,
    live_football_api_key: str | None = None,
    id_map_path: Path | None = None,
) -> dict[str, Any]:
    code = competition.upper()
    label = COMPETITION_NAMES.get(code, code)
    from providers.api_ninjas import (
        NinjasIdMap,
        fetch_season_matches,
        is_ninjas_competition,
        season_to_fd as ninjas_season_to_fd,
    )
    from providers.live_football_api import (
        fetch_league_fixtures,
        is_live_football_competition,
        season_to_fd as lfa_season_to_fd,
    )
    from core.models import ProviderError

    # Prefer Live Football API for SPL history (multi-season archive).
    if is_live_football_competition(code):
        key = (live_football_api_key or "").strip()
        if key:
            try:
                data = fetch_league_fixtures(key, competition_code=code, season=season)
            except ProviderError as exc:
                raise RuntimeError(str(exc)) from exc
            return lfa_season_to_fd(data, competition_code=code, season=season)
        # Fall back to API Ninjas when Live Football key is absent.
        if not (ninjas_api_key or "").strip():
            raise RuntimeError(
                f"{label} history needs LIVE_FOOTBALL_API_KEY "
                "(or NINJAS_API_KEY as a fallback)."
            )

    if is_ninjas_competition(code):
        key = (ninjas_api_key or "").strip()
        if not key:
            raise RuntimeError(
                f"{label} history needs LIVE_FOOTBALL_API_KEY or NINJAS_API_KEY."
            )
        path = id_map_path or Path("data/cache/ninjas_match_ids.json")
        try:
            rows = fetch_season_matches(key, season, competition_code=code)
        except ProviderError as exc:
            raise RuntimeError(str(exc)) from exc
        return ninjas_season_to_fd(
            rows,
            competition_code=code,
            season=season,
            id_map=NinjasIdMap(path),
        )

    return _fetch_football_data_matches(api_key, code, label, season=season)


def filter_matches_by_utc_date_window(
    body: dict[str, Any],
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """Keep matches whose UTC calendar date is inside ``[date_from, date_to]`` inclusive."""

    kept: list[Any] = []
    for match in body.get("matches") or []:
        if not isinstance(match, dict):
            continue
        utc = match.get("utcDate")
        if not isinstance(utc, str) or len(utc) < 10:
            continue
        day = utc[:10]
        if date_from <= day <= date_to:
            kept.append(match)
    filtered = dict(body)
    filtered["matches"] = kept
    return filtered


def fetch_matches_window(
    api_key: str,
    season: int,
    *,
    competition: str = "PL",
    date_from: str,
    date_to: str,
    ninjas_api_key: str | None = None,
    live_football_api_key: str | None = None,
    id_map_path: Path | None = None,
) -> dict[str, Any]:
    """Fetch a date window of fixtures for sync (recent results + upcoming).

    Football-Data.org supports ``dateFrom``/``dateTo`` server-side.
    SPL / Ninjas providers fetch the season then filter client-side.
    """

    code = competition.upper()
    label = COMPETITION_NAMES.get(code, code)
    from providers.api_ninjas import is_ninjas_competition
    from providers.live_football_api import is_live_football_competition

    if is_live_football_competition(code) or is_ninjas_competition(code):
        body = fetch_season(
            api_key,
            season,
            competition=code,
            ninjas_api_key=ninjas_api_key,
            live_football_api_key=live_football_api_key,
            id_map_path=id_map_path,
        )
        return filter_matches_by_utc_date_window(
            body, date_from=date_from, date_to=date_to
        )

    # Football-Data treats dateTo as exclusive on /matches; widen by one day.
    try:
        exclusive_to = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
    except ValueError as exc:
        raise RuntimeError(f"Invalid date_to {date_to!r} for {label}.") from exc

    return _fetch_football_data_matches(
        api_key,
        code,
        label,
        season=season,
        date_from=date_from,
        date_to=exclusive_to,
    )


def _fetch_football_data_matches(
    api_key: str,
    code: str,
    label: str,
    *,
    season: int,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    params: dict[str, str] = {"season": str(season)}
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{API_ROOT.format(competition=code)}?{query}",
        headers={"X-Auth-Token": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Football-Data.org rejected the {label} {season} request with HTTP {exc.code}."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Football-Data.org could not be reached ({exc.reason}).") from exc
    if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
        raise RuntimeError(f"Football-Data.org returned no match list for {label} {season}.")
    return body


def parse_season(
    body: dict[str, Any],
    *,
    fallback_season: int,
    competition: str = "PL",
) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for match in body.get("matches") or []:
        record = parse_match(
            match,
            fallback_season=fallback_season,
            competition=competition,
        )
        if record is not None:
            parsed.append(record)
    return parsed


def parse_match(
    match: dict[str, Any],
    *,
    fallback_season: int,
    competition: str = "PL",
) -> dict[str, Any] | None:
    match_id = match.get("id")
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}
    kickoff = match.get("utcDate")
    if not isinstance(match_id, int) or not isinstance(kickoff, str):
        return None
    if not isinstance(home.get("id"), int) or not isinstance(away.get("id"), int):
        return None
    if not isinstance(home.get("name"), str) or not isinstance(away.get("name"), str):
        return None
    season = _season_year(match, fallback_season)
    full_time = _pair((match.get("score") or {}).get("fullTime") or {})
    half_time = _pair((match.get("score") or {}).get("halfTime") or {})
    result = None
    if full_time is not None:
        result = {
            "ft_home": full_time[0],
            "ft_away": full_time[1],
            "ht_home": None if half_time is None else half_time[0],
            "ht_away": None if half_time is None else half_time[1],
        }
    code = competition.upper()
    match_competition = match.get("competition") if isinstance(match.get("competition"), dict) else {}
    return {
        "match_id": str(match_id),
        "dataset_version": dataset_version(code),
        "competition": code,
        "competition_name": (
            match_competition.get("name")
            if isinstance(match_competition.get("name"), str)
            else COMPETITION_NAMES.get(code, code)
        ),
        "emblem_url": (
            match_competition.get("emblem")
            if isinstance(match_competition.get("emblem"), str)
            else None
        ),
        "season": str(season),
        "kickoff": kickoff,
        "home_id": str(home["id"]),
        "home_name": home["name"],
        "home_short_name": home.get("shortName")
        if isinstance(home.get("shortName"), str)
        else None,
        "home_crest": home.get("crest") if isinstance(home.get("crest"), str) else None,
        "away_id": str(away["id"]),
        "away_name": away["name"],
        "away_short_name": away.get("shortName")
        if isinstance(away.get("shortName"), str)
        else None,
        "away_crest": away.get("crest") if isinstance(away.get("crest"), str) else None,
        "result": result,
    }


def import_bodies(
    connection: Any,
    bodies: list[tuple[int, dict[str, Any]]],
    *,
    competition: str = "PL",
    experiment_config: dict[str, Any] | None = None,
    strict_team_mapping: bool = False,
) -> dict[str, int]:
    """Store fetched season payloads. `bodies` is `(requested_season, response)`."""

    from laboratory.job import EXPERIMENT_CONFIG
    from laboratory.store import (
        append_result_if_changed,
        ensure_experiment,
        remove_datasets_with_prefix,
        save_raw_payload,
        upsert_competition,
        upsert_match,
        upsert_team,
    )

    code = competition.upper()
    from providers.api_ninjas import is_ninjas_competition
    from providers.live_football_api import is_live_football_competition

    if is_live_football_competition(code):
        result_source = LIVE_FOOTBALL_SOURCE
        mapping_provider = "live_football"
    elif is_ninjas_competition(code):
        result_source = NINJAS_SOURCE
        mapping_provider = "api_ninjas"
    else:
        result_source = SOURCE
        mapping_provider = "football_data"
    from db.team_mapping import (
        has_provider_mappings,
        register_team_mapping,
        resolve_team_id,
    )

    enforce_strict = strict_team_mapping or has_provider_mappings(
        connection, provider=mapping_provider
    )
    ensure_experiment(connection, experiment_config or EXPERIMENT_CONFIG)
    upsert_competition(connection, code=code, name=COMPETITION_NAMES.get(code, code))
    removed = remove_datasets_with_prefix(connection, "demo-")
    # Drop hashed Ninjas copies only. Live Football rows upsert by match id so
    # importing extra seasons does not wipe seasons already stored.
    if is_live_football_competition(code):
        removed += remove_datasets_with_prefix(connection, f"api-ninjas-{code.lower()}")
    counts = {
        "competition": code,
        "demo_removed": removed,
        "matches": 0,
        "finished": 0,
        "results_added": 0,
        "results_unchanged": 0,
        "skipped": 0,
    }
    for season, body in bodies:
        encoded = json.dumps(body, sort_keys=True)
        identity = payload_id(body)
        save_raw_payload(connection, identity, encoded)
        body_competition = body.get("competition") if isinstance(body.get("competition"), dict) else {}
        if isinstance(body_competition.get("code"), str):
            upsert_competition(
                connection,
                code=str(body_competition["code"]),
                name=str(
                    body_competition.get("name")
                    or COMPETITION_NAMES.get(str(body_competition["code"]).upper(), code)
                ),
                emblem_url=body_competition.get("emblem")
                if isinstance(body_competition.get("emblem"), str)
                else None,
            )
        records = parse_season(body, fallback_season=season, competition=code)
        counts["skipped"] += len(body.get("matches") or []) - len(records)
        for record in records:
            record["raw_payload_id"] = identity
            result = record.pop("result")
            home_crest = record.pop("home_crest", None)
            away_crest = record.pop("away_crest", None)
            home_short = record.pop("home_short_name", None)
            away_short = record.pop("away_short_name", None)
            emblem_url = record.pop("emblem_url", None)
            competition_name = record.pop("competition_name", COMPETITION_NAMES.get(code, code))
            upsert_competition(
                connection,
                code=str(record["competition"]),
                name=str(competition_name),
                emblem_url=emblem_url if isinstance(emblem_url, str) else None,
            )
            if enforce_strict:
                resolved_home_id = resolve_team_id(
                    connection,
                    provider=mapping_provider,
                    raw_name=str(record["home_name"]),
                    provider_team_id=str(record["home_id"]),
                    competition=str(record["competition"]),
                )
                resolved_away_id = resolve_team_id(
                    connection,
                    provider=mapping_provider,
                    raw_name=str(record["away_name"]),
                    provider_team_id=str(record["away_id"]),
                    competition=str(record["competition"]),
                )
                record["home_id"] = resolved_home_id
                record["away_id"] = resolved_away_id
            else:
                register_team_mapping(
                    connection,
                    canonical_name=str(record["home_name"]),
                    provider=mapping_provider,
                    provider_name=str(record["home_name"]),
                    provider_team_id=str(record["home_id"]),
                    competition=str(record["competition"]),
                    team_id=str(record["home_id"]),
                )
                register_team_mapping(
                    connection,
                    canonical_name=str(record["away_name"]),
                    provider=mapping_provider,
                    provider_name=str(record["away_name"]),
                    provider_team_id=str(record["away_id"]),
                    competition=str(record["competition"]),
                    team_id=str(record["away_id"]),
                )
            upsert_team(
                connection,
                team_id=str(record["home_id"]),
                name=str(record["home_name"]),
                crest_url=home_crest if isinstance(home_crest, str) else None,
                short_name=home_short if isinstance(home_short, str) else None,
            )
            upsert_team(
                connection,
                team_id=str(record["away_id"]),
                name=str(record["away_name"]),
                crest_url=away_crest if isinstance(away_crest, str) else None,
                short_name=away_short if isinstance(away_short, str) else None,
            )
            upsert_match(connection, record)
            counts["matches"] += 1
            if result is None:
                continue
            counts["finished"] += 1
            _, changed = append_result_if_changed(
                connection, record["match_id"], result, result_source
            )
            counts["results_added" if changed else "results_unchanged"] += 1
    connection.commit()
    return counts


def payload_id(body: dict[str, Any]) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _season_year(match: dict[str, Any], fallback_season: int) -> int:
    start = ((match.get("season") or {}).get("startDate") or "")
    if len(start) >= 4 and start[:4].isdigit():
        return int(start[:4])
    return fallback_season


def _pair(score: dict[str, Any]) -> tuple[int, int] | None:
    home = score.get("home")
    away = score.get("away")
    if isinstance(home, bool) or isinstance(away, bool):
        return None
    if not isinstance(home, int) or not isinstance(away, int):
        return None
    if home < 0 or away < 0:
        return None
    return home, away
