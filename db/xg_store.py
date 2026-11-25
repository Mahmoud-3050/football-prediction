"""Normalized Expected Goals (xG) storage, strict canonical importer, and match attachment."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from db.team_mapping import resolve_team_id

_ATTACH_WINDOW_SECONDS = 24 * 3600


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _validate_xg(value: Any, label: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(num) or num < 0.0:
        raise ValueError(f"{label} must be a finite non-negative float, got {value!r}")
    return num


def load_xg_file(path: Path) -> list[dict[str, Any]]:
    """Load xG records from a JSON array or CSV file."""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".json":
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Expected JSON list in {target}")
        return [dict(item) for item in data]
    if suffix == ".csv":
        with target.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError(f"Unsupported xG file format: {target.suffix!r} (expected .csv or .json)")


def import_xg_records(
    conn: sqlite3.Connection,
    records: Sequence[Mapping[str, Any]],
    *,
    source: str,
    competition: str,
    season: int,
) -> dict[str, int]:
    """Strictly resolve teams via team_mapping and upsert xG records into match_xg."""
    src = str(source or "").strip().lower()
    comp = str(competition or "").strip().upper()
    season_int = int(season)
    if not src:
        raise ValueError("source is required")
    if not comp:
        raise ValueError("competition is required")

    now_iso = datetime.now(timezone.utc).isoformat()
    rows_to_upsert: list[tuple[Any, ...]] = []

    for rec in records:
        home_raw = str(
            rec.get("home_team")
            or rec.get("home_team_name")
            or rec.get("home")
            or ""
        ).strip()
        away_raw = str(
            rec.get("away_team")
            or rec.get("away_team_name")
            or rec.get("away")
            or ""
        ).strip()
        if not home_raw or not away_raw:
            raise ValueError(f"Missing home/away team name in xG record: {dict(rec)!r}")

        kickoff_raw = str(
            rec.get("kickoff_utc")
            or rec.get("kickoff")
            or rec.get("date")
            or ""
        ).strip()
        if not kickoff_raw:
            raise ValueError(f"Missing kickoff_utc in xG record: {dict(rec)!r}")
        kickoff_iso = _parse_utc(kickoff_raw).isoformat()

        home_xg = _validate_xg(
            rec.get("home_xg") if rec.get("home_xg") is not None else rec.get("xg_home"),
            "home_xg",
        )
        away_xg = _validate_xg(
            rec.get("away_xg") if rec.get("away_xg") is not None else rec.get("xg_away"),
            "away_xg",
        )

        home_team_id = resolve_team_id(
            conn,
            provider=src,
            raw_name=home_raw,
            competition=comp,
        )
        away_team_id = resolve_team_id(
            conn,
            provider=src,
            raw_name=away_raw,
            competition=comp,
        )
        rows_to_upsert.append(
            (
                comp,
                season_int,
                kickoff_iso,
                str(home_team_id),
                str(away_team_id),
                home_xg,
                away_xg,
                src,
                now_iso,
            )
        )

    if rows_to_upsert:
        conn.executemany(
            """
            INSERT INTO match_xg (
                competition, season, kickoff_utc,
                home_team_id, away_team_id,
                home_xg, away_xg, source, imported_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(competition, season, home_team_id, away_team_id, source)
            DO UPDATE SET
                kickoff_utc = excluded.kickoff_utc,
                home_xg = excluded.home_xg,
                away_xg = excluded.away_xg,
                imported_at_utc = excluded.imported_at_utc
            """,
            rows_to_upsert,
        )
        conn.commit()
    return {"imported": len(rows_to_upsert)}


def attach_xg_to_matches(
    conn: sqlite3.Connection,
    matches: Sequence[Mapping[str, Any]],
    *,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Left-join match_xg onto match dicts by (competition, home_id, away_id) within +-24h."""
    if not matches:
        return []

    sql = "SELECT competition, kickoff_utc, home_team_id, away_team_id, home_xg, away_xg, source FROM match_xg"
    params: list[Any] = []
    if source:
        sql += " WHERE LOWER(source) = ?"
        params.append(source.strip().lower())
    sql += " ORDER BY id ASC"

    try:
        xg_rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return [{**dict(m), "home_xg": m.get("home_xg"), "away_xg": m.get("away_xg")} for m in matches]

    by_pair: dict[tuple[str, str, str], list[tuple[datetime, float, float]]] = {}
    for row in xg_rows:
        try:
            k_dt = _parse_utc(row["kickoff_utc"])
        except Exception:
            continue
        key = (
            str(row["competition"]).upper(),
            str(row["home_team_id"]),
            str(row["away_team_id"]),
        )
        by_pair.setdefault(key, []).append(
            (k_dt, float(row["home_xg"]), float(row["away_xg"]))
        )

    attached: list[dict[str, Any]] = []
    for match in matches:
        item = dict(match)
        comp = str(item.get("competition") or "").upper()
        hid = str(item.get("home_id") if item.get("home_id") is not None else item.get("home_team_id") or "")
        aid = str(item.get("away_id") if item.get("away_id") is not None else item.get("away_team_id") or "")
        kick_raw = item.get("kickoff") or item.get("kickoff_utc")
        home_xg = item.get("home_xg")
        away_xg = item.get("away_xg")

        if comp and hid and aid and kick_raw and (home_xg is None or away_xg is None):
            candidates = by_pair.get((comp, hid, aid)) or []
            if candidates:
                try:
                    m_dt = _parse_utc(str(kick_raw))
                    best_diff = _ATTACH_WINDOW_SECONDS + 1.0
                    for c_dt, hxg, axg in candidates:
                        diff = abs((c_dt - m_dt).total_seconds())
                        if diff <= _ATTACH_WINDOW_SECONDS and diff < best_diff:
                            best_diff = diff
                            home_xg = hxg
                            away_xg = axg
                except Exception:
                    pass

        item["home_xg"] = home_xg
        item["away_xg"] = away_xg
        attached.append(item)
    return attached
