"""On-demand Football-Data.org client.

Every method here is called from a user request. There is no refresh loop
and no startup fetch. Team and standings responses are cached only when a
request reads or writes them.
"""

from __future__ import annotations

import asyncio
import contextvars
import difflib
import json
import logging
import re
import time
import unicodedata
from collections import deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from models import (
    Competition,
    Fixture,
    FixtureResolution,
    ProviderError,
    Team,
    TeamResolution,
)
from timeutil import (
    api_date_window_for_cairo_day,
    cairo_today,
    falls_on_cairo_day,
    format_kickoff_cairo,
    format_kickoff_short_cairo,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org"
UPCOMING_STATUSES = frozenset({"SCHEDULED", "TIMED"})
TEAMS_CACHE_TTL_SECONDS = 6 * 60 * 60
STANDINGS_CACHE_TTL_SECONDS = 15 * 60
MAX_CALLS_PER_MINUTE = 8
# Competitions shown by /today. Match ids from that list feed /predict <id>.
TODAY_MATCH_COMPETITIONS: tuple[str, ...] = ("PL", "CL", "PD", "SA", "BL1", "FL1")

FREE_COMPETITIONS: dict[str, dict[str, Any]] = {
    "WC": {
        "name": "FIFA World Cup",
        "aliases": ("world cup", "fifa world cup", "wc"),
    },
    "CL": {
        "name": "UEFA Champions League",
        "aliases": ("champions league", "uefa champions league", "ucl", "cl"),
    },
    "EC": {
        "name": "European Championship",
        "aliases": (
            "european championship",
            "uefa european championship",
            "euro",
            "euros",
            "uefa euro",
            "ec",
        ),
    },
    "PL": {
        "name": "Premier League",
        "aliases": ("premier league", "english premier league", "epl", "pl"),
    },
    "ELC": {
        "name": "Championship",
        "aliases": ("championship", "efl championship", "elc"),
    },
    "BL1": {
        "name": "Bundesliga",
        "aliases": ("bundesliga", "german bundesliga", "bl1"),
    },
    "SA": {
        "name": "Serie A",
        "aliases": ("serie a", "italian serie a", "serie a italy", "sa"),
    },
    "PD": {
        "name": "Primera Division",
        "aliases": (
            "primera division",
            "la liga",
            "laliga",
            "la liga ea sports",
            "spanish primera division",
            "pd",
        ),
    },
    "FL1": {
        "name": "Ligue 1",
        "aliases": ("ligue 1", "french ligue 1", "fl1"),
    },
    "PPL": {
        "name": "Primeira Liga",
        "aliases": ("primeira liga", "liga portugal", "portuguese liga", "ppl"),
    },
    "DED": {
        "name": "Eredivisie",
        "aliases": ("eredivisie", "dutch eredivisie", "ded"),
    },
    "BSA": {
        "name": "Campeonato Brasileiro Série A",
        "aliases": (
            "campeonato brasileiro serie a",
            "brasileirao",
            "brazilian serie a",
            "brazil serie a",
            "serie a brazil",
            "bsa",
        ),
    },
}

# Short names applied only inside the competition whose teams were loaded.
TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "man city": ("manchester city",),
    "mancity": ("manchester city",),
    "mcfc": ("manchester city",),
    "man utd": ("manchester united",),
    "man united": ("manchester united",),
    "manutd": ("manchester united",),
    "barca": ("barcelona",),
    "inter": ("internazionale",),
    "inter milan": ("internazionale",),
    "spurs": ("tottenham",),
    "psg": ("paris saint germain",),
    "paris sg": ("paris saint germain",),
    "bayern": ("bayern",),
    "bvb": ("dortmund",),
    "dortmund": ("dortmund",),
    "atletico": ("atletico",),
    "atleti": ("atletico",),
    "juve": ("juventus",),
    "wolves": ("wolverhampton",),
    "villa": ("aston villa",),
    "palace": ("crystal palace",),
    "hammers": ("west ham",),
    "west ham": ("west ham",),
    "toon": ("newcastle",),
    "forest": ("nottingham",),
    "ac milan": ("milan",),
}


def free_tier_names() -> list[str]:
    return [f"{meta['name']} ({code})" for code, meta in FREE_COMPETITIONS.items()]


def normalize_label(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_team(value: str) -> str:
    text = normalize_label(value)
    text = re.sub(r"\b(fc|afc|cf|sc|ac|club|the)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def resolve_competition(text: str) -> Competition | None:
    """Map a user label to one free-tier code. Unknown names are not guessed."""

    label = normalize_label(text)
    squeezed = label.replace(" ", "")
    for code, meta in FREE_COMPETITIONS.items():
        names = {normalize_label(meta["name"]), code.casefold()}
        names.update(normalize_label(alias) for alias in meta["aliases"])
        squeezed_names = {name.replace(" ", "") for name in names}
        if label in names or squeezed in squeezed_names:
            return Competition(code=code, name=meta["name"])
    return None


def unknown_competition_reason(text: str) -> str:
    listed = ", ".join(free_tier_names())
    return f'Unknown competition "{text.strip()}". Free-tier competitions: {listed}.'


def resolve_team(query: str, teams: list[Team], competition_name: str) -> TeamResolution:
    """Resolve a club inside one competition.

    Exact names and known aliases can select a club. A typo returns the closest
    names. An ambiguous name asks for a more specific one. Nothing here swaps
    the requested home and away clubs.
    """

    cleaned = query.strip()
    if not cleaned:
        return TeamResolution(None, "A club name is required.")

    query_team = normalize_team(cleaned)
    query_label = normalize_label(cleaned)
    exact = [
        team
        for team in teams
        if query_team in _team_keys(team) or _tla_matches(team, query_label)
    ]
    exact = _dedupe(exact)
    if len(exact) == 1:
        return TeamResolution(exact[0], None)
    if len(exact) > 1:
        return TeamResolution(None, _ambiguous(cleaned, competition_name, exact))

    alias_hits = _dedupe(_alias_hits(query_label, teams) + _alias_hits(query_team, teams))
    if len(alias_hits) == 1:
        return TeamResolution(alias_hits[0], None)
    if len(alias_hits) > 1:
        return TeamResolution(None, _ambiguous(cleaned, competition_name, alias_hits))

    token_hits = _dedupe(_token_hits(query_team, teams))
    if len(token_hits) == 1:
        return TeamResolution(token_hits[0], None)
    if len(token_hits) > 1:
        return TeamResolution(None, _ambiguous(cleaned, competition_name, token_hits))

    prefix_hits = _dedupe(_prefix_hits(query_team, teams))
    if len(prefix_hits) > 1:
        return TeamResolution(None, _ambiguous(cleaned, competition_name, prefix_hits))

    closest = closest_team_names(cleaned, teams)
    if closest:
        listed = ", ".join(closest)
        return TeamResolution(
            None,
            f'"{cleaned}" did not match a club in {competition_name}. Closest names: {listed}.',
        )
    return TeamResolution(
        None,
        f'"{cleaned}" did not match a club in {competition_name}. That competition returned no teams.',
    )


def closest_team_names(query: str, teams: list[Team], limit: int = 3) -> list[str]:
    query_norm = normalize_team(query)
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for team in teams:
        if not team.name or team.name in seen:
            continue
        options = [normalize_team(team.name)]
        if team.short_name:
            options.append(normalize_team(team.short_name))
        options = [option for option in options if option]
        if not options or not query_norm:
            ratio = 0.0
        else:
            ratio = max(
                difflib.SequenceMatcher(None, query_norm, option).ratio() for option in options
            )
        seen.add(team.name)
        scored.append((ratio, team.name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored[:limit]]


def teams_from_payload(payload: dict[str, Any]) -> list[Team]:
    teams: list[Team] = []
    for raw in payload.get("teams") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
            continue
        teams.append(
            Team(
                id=raw["id"],
                name=str(raw.get("name") or ""),
                short_name=raw.get("shortName") if isinstance(raw.get("shortName"), str) else None,
                tla=raw.get("tla") if isinstance(raw.get("tla"), str) else None,
            )
        )
    return teams


def parse_fixture(raw: dict[str, Any]) -> Fixture:
    score = raw.get("score") or {}
    full_time = score.get("fullTime") or {}
    half_time = score.get("halfTime") or {}
    home = raw.get("homeTeam") or {}
    away = raw.get("awayTeam") or {}
    competition = raw.get("competition") or {}
    return Fixture(
        id=int(raw["id"]),
        status=str(raw.get("status") or ""),
        utc_date=raw.get("utcDate") if isinstance(raw.get("utcDate"), str) else None,
        competition_code=str(competition.get("code") or ""),
        competition_name=str(competition.get("name") or ""),
        home_team_id=int(home["id"]),
        home_team_name=str(home.get("name") or ""),
        away_team_id=int(away["id"]),
        away_team_name=str(away.get("name") or ""),
        full_time_home=_optional_int(full_time.get("home")),
        full_time_away=_optional_int(full_time.get("away")),
        half_time_home=_optional_int(half_time.get("home")),
        half_time_away=_optional_int(half_time.get("away")),
        last_updated=raw.get("lastUpdated") if isinstance(raw.get("lastUpdated"), str) else None,
    )


def fixtures_from_payload(
    payload: dict[str, Any],
    *,
    cairo_day: date | None = None,
) -> list[Fixture]:
    fixtures: list[Fixture] = []
    for raw in payload.get("matches") or []:
        if not isinstance(raw, dict):
            continue
        try:
            fixture = parse_fixture(raw)
        except (KeyError, TypeError, ValueError):
            continue
        if fixture.competition_code not in TODAY_MATCH_COMPETITIONS:
            continue
        if cairo_day is not None and not falls_on_cairo_day(fixture.utc_date, cairo_day):
            continue
        fixtures.append(fixture)
    return sort_todays_fixtures(fixtures)


def sort_todays_fixtures(fixtures: list[Fixture]) -> list[Fixture]:
    order = {code: index for index, code in enumerate(TODAY_MATCH_COMPETITIONS)}
    return sorted(
        fixtures,
        key=lambda fixture: (
            order.get(fixture.competition_code, len(order)),
            fixture.utc_date or "",
            fixture.id,
        ),
    )


def format_todays_matches(fixtures: list[Fixture], *, max_chars: int = 3500) -> list[str]:
    return format_match_list(fixtures, day_label="today", max_chars=max_chars)


def format_match_list(
    fixtures: list[Fixture],
    *,
    day_label: str,
    max_chars: int = 3500,
) -> list[str]:
    """Build Telegram-ready pages. Match ids are Football-Data.org ids."""

    if not fixtures:
        competitions = ", ".join(
            FREE_COMPETITIONS[code]["name"] for code in TODAY_MATCH_COMPETITIONS
        )
        return [
            f"No matches listed for {day_label} in:\n"
            f"{competitions}\n\n"
            "This list is display-only. No prediction is made here."
        ]

    heading = (
        "Today's matches (no predictions):"
        if day_label == "today"
        else f"Matches for {day_label} (no predictions):"
    )
    lines: list[str] = [
        heading,
        "Tap a match button below, or use /predict <id>.",
        "",
    ]
    current_competition = ""
    for fixture in fixtures:
        competition_name = fixture.competition_name or FREE_COMPETITIONS.get(
            fixture.competition_code, {}
        ).get("name", fixture.competition_code)
        if competition_name != current_competition:
            if current_competition:
                lines.append("")
            lines.append(f"{competition_name}")
            current_competition = competition_name
        kickoff = format_kickoff_short_cairo(fixture.utc_date)
        lines.append(
            f"#{fixture.id}  {kickoff}  "
            f"{fixture.home_team_name} vs {fixture.away_team_name}  [{fixture.status}]"
        )
    lines.extend(
        [
            "",
            "Example: /predict #" + str(fixtures[0].id),
            "This list does not run predictions.",
        ]
    )
    return _chunk_lines(lines, max_chars=max_chars)


def team_from_fixture_side(team_id: int, team_name: str) -> Team:
    return Team(id=team_id, name=team_name, short_name=None, tla=None)


def _chunk_lines(lines: list[str], *, max_chars: int) -> list[str]:
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        addition = len(line) + (1 if current else 0)
        if current and size + addition > max_chars:
            pages.append("\n".join(current))
            current = [line]
            size = len(line)
        else:
            current.append(line)
            size += addition
    if current:
        pages.append("\n".join(current))
    return pages


def classify_fixtures(
    matches: list[dict[str, Any]],
    *,
    competition_code: str,
    home_id: int,
    away_id: int,
) -> FixtureResolution:
    """Pick the requested orientation only. The opposite venue is never substituted."""

    correct: list[Fixture] = []
    opposite: list[Fixture] = []
    for raw in matches:
        if not isinstance(raw, dict):
            continue
        competition = raw.get("competition") or {}
        if competition.get("code") != competition_code:
            continue
        home = (raw.get("homeTeam") or {}).get("id")
        away = (raw.get("awayTeam") or {}).get("id")
        try:
            fixture = parse_fixture(raw)
        except (KeyError, TypeError, ValueError):
            continue
        if home == home_id and away == away_id:
            correct.append(fixture)
        elif home == away_id and away == home_id:
            opposite.append(fixture)

    upcoming = _by_date(fixture for fixture in correct if fixture.status in UPCOMING_STATUSES)
    if upcoming:
        return FixtureResolution("upcoming", upcoming[0])

    finished = _by_date(fixture for fixture in correct if fixture.status == "FINISHED")
    if finished:
        return FixtureResolution("finished", finished[-1])

    other = _by_date(
        fixture
        for fixture in correct
        if fixture.status not in UPCOMING_STATUSES and fixture.status != "FINISHED"
    )
    if other:
        return FixtureResolution("unavailable", other[0])

    if opposite:
        upcoming_opposite = _by_date(
            fixture for fixture in opposite if fixture.status in UPCOMING_STATUSES
        )
        if upcoming_opposite:
            return FixtureResolution("opposite", upcoming_opposite[0])
        return FixtureResolution("opposite", _by_date(opposite)[-1])

    return FixtureResolution("none", None)


def explain_fixture(
    resolution: FixtureResolution,
    *,
    competition_name: str,
    requested_home: str,
    requested_away: str,
) -> str:
    fixture = resolution.fixture
    if resolution.kind == "none" or fixture is None:
        return (
            f"Checked {competition_name} for {requested_home} vs {requested_away}. "
            "No fixture with that home team, away team, and competition was found."
        )
    kickoff_text = format_kickoff_cairo(fixture.utc_date)
    kickoff = f" Kickoff: {kickoff_text}." if kickoff_text else ""
    if resolution.kind == "opposite":
        return (
            f"Checked {competition_name} for {requested_home} vs {requested_away}. "
            f"The only meeting is {fixture.home_team_name} vs {fixture.away_team_name} "
            f"({fixture.status}).{kickoff} "
            "Home and away were not swapped, so that fixture was not analyzed."
        )
    if resolution.kind == "finished":
        score = fixture.score_text() or "the provider did not supply a full-time score"
        return (
            f"{requested_home} vs {requested_away} in {competition_name} is finished "
            f"({score}). No prediction is made for a match that has already been played."
        )
    if resolution.kind == "unavailable":
        return (
            f"{requested_home} vs {requested_away} in {competition_name} is {fixture.status}. "
            "Only scheduled or timed fixtures are predicted."
        )
    return ""


def meetings_in_history(
    payloads: list[dict[str, Any]],
    home_id: int,
    away_id: int,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    found: dict[Any, dict[str, Any]] = {}
    for payload in payloads:
        for match in payload.get("matches") or []:
            if not isinstance(match, dict):
                continue
            ids = {
                (match.get("homeTeam") or {}).get("id"),
                (match.get("awayTeam") or {}).get("id"),
            }
            if ids == {home_id, away_id} and match.get("id") is not None:
                found[match["id"]] = match
    ordered = sorted(found.values(), key=lambda match: match.get("utcDate") or "", reverse=True)
    return ordered[:limit]


def extract_last_updated(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    stamp = payload.get("lastUpdated")
    if isinstance(stamp, str) and stamp:
        return stamp
    stamps: list[str] = []
    matches = payload.get("matches")
    if isinstance(matches, list):
        for match in matches:
            if isinstance(match, dict) and isinstance(match.get("lastUpdated"), str):
                stamps.append(match["lastUpdated"])
    if stamps:
        return max(stamps)
    return None


class RateLimiter:
    """In-process cap of about 8 Football-Data calls per minute."""

    def __init__(
        self,
        max_calls: int = MAX_CALLS_PER_MINUTE,
        period_seconds: float = 60.0,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                self._drop_expired(now)
                if len(self._times) < self.max_calls:
                    self._times.append(now)
                    return
                wait = self.period_seconds - (now - self._times[0])
            await self._sleep(wait if wait > 0 else 0)

    def _drop_expired(self, now: float) -> None:
        while self._times and now - self._times[0] >= self.period_seconds:
            self._times.popleft()


class _RequestState:
    def __init__(self) -> None:
        self.waited_for_rate_limit = False


class DataFetcher:
    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        *,
        client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self._client = client
        self._sleep = sleep or asyncio.sleep
        self.rate_limiter = rate_limiter or RateLimiter(sleep=self._sleep)
        self._state: contextvars.ContextVar[_RequestState | None] = contextvars.ContextVar(
            "football_request_state",
            default=None,
        )

    @asynccontextmanager
    async def request_scope(self) -> AsyncIterator[None]:
        token = self._state.set(_RequestState())
        try:
            yield
        finally:
            self._state.reset(token)

    async def get_teams(self, competition_code: str) -> dict[str, Any]:
        return await self._cached(
            "teams",
            competition_code,
            TEAMS_CACHE_TTL_SECONDS,
            f"/v4/competitions/{competition_code}/teams",
        )

    async def get_standings(self, competition_code: str) -> dict[str, Any]:
        return await self._cached(
            "standings",
            competition_code,
            STANDINGS_CACHE_TTL_SECONDS,
            f"/v4/competitions/{competition_code}/standings",
        )

    async def get_team_matches(
        self,
        team_id: int,
        competition_code: str,
    ) -> dict[str, Any]:
        return await self._get(
            f"/v4/teams/{team_id}/matches",
            params={"competitions": competition_code, "limit": 100},
        )

    async def get_finished_matches(self, team_id: int) -> dict[str, Any]:
        return await self._get(
            f"/v4/teams/{team_id}/matches",
            params={"status": "FINISHED", "limit": 30},
        )

    async def get_head_to_head(self, match_id: int) -> dict[str, Any]:
        return await self._get(
            f"/v4/matches/{match_id}/head2head",
            params={"limit": 5},
        )

    async def get_todays_matches(self) -> dict[str, Any]:
        """Cairo-today fixtures for the /today competitions. Display only."""

        return await self.get_matches_for_cairo_day(cairo_today())

    async def get_matches_for_cairo_day(self, day: date) -> dict[str, Any]:
        """Fixtures that may fall on one Africa/Cairo calendar day."""

        date_from, date_to = api_date_window_for_cairo_day(day)
        params: dict[str, Any] = {
            "competitions": ",".join(TODAY_MATCH_COMPETITIONS),
            "dateFrom": date_from,
            "dateTo": date_to,
        }
        return await self._get("/v4/matches", params=params)

    async def get_matches_for_date(self, api_date: str | None) -> dict[str, Any]:
        """Back-compat wrapper. Prefer get_matches_for_cairo_day."""

        if api_date:
            year, month, day = (int(part) for part in api_date.split("-", 2))
            return await self.get_matches_for_cairo_day(date(year, month, day))
        return await self.get_todays_matches()

    async def get_match(self, match_id: int) -> dict[str, Any]:
        return await self._get(f"/v4/matches/{match_id}")

    async def _cached(
        self,
        kind: str,
        key: str,
        ttl_seconds: float,
        path: str,
    ) -> dict[str, Any]:
        cache_path = self._cache_file(kind, key)
        cached = self._read_cache(cache_path, ttl_seconds)
        if cached is not None:
            return cached
        payload = await self._get(path)
        self._write_cache(cache_path, payload)
        return payload

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_rate_limit_retry: bool = True,
    ) -> dict[str, Any]:
        await self.rate_limiter.acquire()
        client = await self._ensure_client()
        try:
            response = await client.get(
                path,
                params=params,
                headers={"X-Auth-Token": self.api_key},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"Football-Data.org timed out ({exc}).") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Football-Data.org could not be reached ({exc}).") from exc

        if response.status_code == 429:
            state = self._current_state()
            if state.waited_for_rate_limit or not allow_rate_limit_retry:
                raise ProviderError(
                    "Football-Data.org free-tier limit was hit after one wait."
                )
            state.waited_for_rate_limit = True
            await self._sleep(self._retry_delay(response))
            return await self._get(path, params, allow_rate_limit_retry=False)

        if response.status_code in (401, 403):
            detail = _error_detail(response)
            suffix = f" {detail}" if detail else ""
            raise ProviderError(
                "Football-Data.org rejected the API key or competition "
                f"(FOOTBALL_API_KEY, HTTP {response.status_code}) for {path}.{suffix}"
            )

        if response.status_code == 404 and path.endswith("/head2head"):
            return {"matches": []}

        if response.status_code >= 400:
            detail = _error_detail(response)
            suffix = f" {detail}" if detail else ""
            raise ProviderError(
                f"Football-Data.org returned HTTP {response.status_code} for {path}.{suffix}"
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError("Football-Data.org returned a response that was not JSON.") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Football-Data.org returned a response that was not an object.")
        return payload

    def _current_state(self) -> _RequestState:
        state = self._state.get()
        if state is None:
            state = _RequestState()
            self._state.set(state)
        return state

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=httpx.Timeout(20.0),
            )
        return self._client

    def _retry_delay(self, response: httpx.Response) -> float:
        raw = response.headers.get("Retry-After", "60")
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            delay = 2.0
        return min(max(delay, 0.0), 60.0)

    def _cache_file(self, kind: str, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        return self.cache_dir / f"{kind}_{safe}.json"

    def _read_cache(self, path: Path, ttl_seconds: float) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("ignoring unreadable cache file %s", path)
            return None
        cached_at = body.get("cached_at")
        payload = body.get("payload")
        if not isinstance(cached_at, (int, float)) or not isinstance(payload, dict):
            return None
        if time.time() - float(cached_at) > ttl_seconds:
            return None
        return payload

    def _write_cache(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"cached_at": time.time(), "payload": payload}),
            encoding="utf-8",
        )


def _team_keys(team: Team) -> set[str]:
    keys = {normalize_team(team.name)}
    if team.short_name:
        keys.add(normalize_team(team.short_name))
    return {key for key in keys if key}


def _tla_matches(team: Team, query_label: str) -> bool:
    return bool(team.tla and query_label == normalize_label(team.tla))


def _token_hits(query_norm: str, teams: list[Team]) -> list[Team]:
    """A single distinctive token that appears in more than one club is ambiguous."""

    if " " in query_norm or len(query_norm) < 4:
        return []
    hits: list[Team] = []
    for team in teams:
        tokens = set(normalize_team(team.name).split())
        if team.short_name:
            tokens |= set(normalize_team(team.short_name).split())
        if query_norm in tokens:
            hits.append(team)
    return hits


def _alias_hits(query_norm: str, teams: list[Team]) -> list[Team]:
    targets = TEAM_ALIASES.get(query_norm)
    if not targets:
        return []
    hits: list[Team] = []
    for team in teams:
        hay = set(normalize_team(team.name).split())
        if team.short_name:
            hay |= set(normalize_team(team.short_name).split())
        for target in targets:
            need = set(normalize_team(target).split())
            if need and need <= hay:
                hits.append(team)
                break
    return hits


def _prefix_hits(query_norm: str, teams: list[Team]) -> list[Team]:
    if len(query_norm) < 3:
        return []
    hits: list[Team] = []
    for team in teams:
        tokens = set(normalize_team(team.name).split())
        if team.short_name:
            tokens |= set(normalize_team(team.short_name).split())
        if any(token.startswith(query_norm) and token != query_norm for token in tokens):
            hits.append(team)
    return hits


def _ambiguous(query: str, competition_name: str, teams: list[Team]) -> str:
    names = ", ".join(team.name for team in teams)
    return (
        f'"{query}" matches more than one club in {competition_name}: {names}. '
        "Send a more specific name."
    )


def _dedupe(teams: list[Team]) -> list[Team]:
    seen: set[int] = set()
    unique: list[Team] = []
    for team in teams:
        if team.id in seen:
            continue
        seen.add(team.id)
        unique.append(team)
    return unique


def _by_date(fixtures: Any) -> list[Fixture]:
    return sorted(fixtures, key=lambda fixture: fixture.utc_date or "")


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        if isinstance(message, str):
            return message.strip()
    text = response.text.strip()
    return text[:200]
