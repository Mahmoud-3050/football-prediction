"""Resilient, stdlib-only EPL match xG fetcher, normalizer, and optional DB importer."""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import json
import math
import random
import re
import ssl
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path("data/xg/pl")
DEFAULT_MANUAL_DIR = DEFAULT_OUTPUT_DIR / "manual"

UNDERSTAT_SEASONS = (2022, 2023, 2024, 2025)
FBREF_SEASON_SPANS: dict[int, str] = {
    2022: "2022-2023",
    2023: "2023-2024",
    2024: "2024-2025",
    2025: "2025-2026",
}
FPL_GITHUB_SLUGS: dict[int, str] = {
    2022: "2022-23",
    2023: "2023-24",
    2024: "2024-25",
    2025: "2025-26",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAINTENANCE_MARKERS = (
    "we will come back soon",
    "under maintenance",
    "just a moment",
    "attention required! | cloudflare",
)
_DATES_DATA_RE = re.compile(r"datesData\s*=\s*JSON\.parse\('([^']+)'\)")


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _min_matches_for_season(season: int) -> int:
    return 10 if season >= 2025 else 300


def _is_valid_record(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if not item.get("home_team") or not item.get("away_team"):
        return False
    if not (item.get("kickoff_utc") or item.get("date")):
        return False
    try:
        hxg = float(item["home_xg"])
        axg = float(item["away_xg"])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(hxg) and hxg >= 0.0 and math.isfinite(axg) and axg >= 0.0


def check_local_cache(cache_path: Path, season: int) -> list[dict[str, Any]] | None:
    """Return cached records if cache_path exists, parses cleanly, and meets min match threshold."""
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    valid = [item for item in data if _is_valid_record(item)]
    if len(valid) < _min_matches_for_season(season):
        return None
    return valid


def fetch_with_backoff(
    url: str,
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> tuple[str | None, str]:
    """Fetch URL with exponential backoff, jitter, and Retry-After support.

    Returns ``(body_text, status_reason)``. ``body_text`` is ``None`` when unavailable.
    """
    ctx = _ssl_context()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                lowered = text.lower()
                for marker in _MAINTENANCE_MARKERS:
                    if marker in lowered:
                        return None, f"UNAVAILABLE (maintenance/challenge marker: {marker!r})"
                return text, "OK"
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    wait = float(retry_after) if retry_after else base_delay * (2**attempt)
                except ValueError:
                    wait = base_delay * (2**attempt)
                time.sleep(wait * random.uniform(0.5, 1.5))
                continue
            if exc.code == 403 or attempt >= max_retries:
                return None, f"UNAVAILABLE (HTTP {exc.code})"
            time.sleep(base_delay * (2**attempt) * random.uniform(0.5, 1.5))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt >= max_retries:
                return None, f"UNAVAILABLE ({exc})"
            time.sleep(base_delay * (2**attempt) * random.uniform(0.5, 1.5))
    return None, "UNAVAILABLE (exhausted retries)"


class _FBrefScheduleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_sched_table = False
        self.in_row = False
        self.cur_stat: str | None = None
        self.cur_cell_text: list[str] = []
        self.cur_row: dict[str, str] = {}
        self.rows: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "table" and str(attr_map.get("id") or "").startswith("sched_"):
            self.in_sched_table = True
            return
        if not self.in_sched_table:
            return
        if tag == "tr":
            self.in_row = True
            self.cur_row = {}
            return
        if self.in_row and tag in ("td", "th"):
            stat = attr_map.get("data-stat")
            if stat:
                self.cur_stat = stat
                self.cur_cell_text = []

    def handle_data(self, data: str) -> None:
        if self.in_row and self.cur_stat is not None:
            self.cur_cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.in_sched_table:
            self.in_sched_table = False
            return
        if not self.in_sched_table:
            return
        if self.in_row and tag in ("td", "th") and self.cur_stat is not None:
            self.cur_row[self.cur_stat] = "".join(self.cur_cell_text).strip()
            self.cur_stat = None
            return
        if tag == "tr" and self.in_row:
            self.in_row = False
            if self.cur_row:
                self.rows.append(self.cur_row)


def _normalize_record(
    *,
    date_str: str,
    home_team: str,
    away_team: str,
    home_xg: float,
    away_xg: float,
    source: str,
) -> dict[str, Any]:
    dt = date_str.strip()
    if " " in dt and "T" not in dt:
        kickoff_utc = dt.replace(" ", "T")
    elif "T" in dt:
        kickoff_utc = dt
    else:
        kickoff_utc = f"{dt}T15:00:00+00:00"
    if "+" not in kickoff_utc and not kickoff_utc.endswith("Z"):
        kickoff_utc += "+00:00"
    return {
        "date": kickoff_utc[:10],
        "kickoff_utc": kickoff_utc,
        "home_team": home_team.strip(),
        "away_team": away_team.strip(),
        "home_xg": float(home_xg),
        "away_xg": float(away_xg),
        "source": source,
    }


def _parse_understat_html(html_text: str) -> list[dict[str, Any]] | None:
    m = _DATES_DATA_RE.search(html_text)
    if not m:
        return None
    decoded = codecs.decode(m.group(1), "unicode_escape")
    raw_matches = json.loads(decoded)
    out: list[dict[str, Any]] = []
    for item in raw_matches:
        if not item.get("isResult"):
            continue
        xg = item.get("xG") or {}
        if xg.get("h") is None or xg.get("a") is None:
            continue
        out.append(
            _normalize_record(
                date_str=str(item.get("datetime") or ""),
                home_team=str((item.get("h") or {}).get("title") or ""),
                away_team=str((item.get("a") or {}).get("title") or ""),
                home_xg=float(xg["h"]),
                away_xg=float(xg["a"]),
                source="understat",
            )
        )
    return out if out else None


def _parse_fbref_html(html_text: str) -> list[dict[str, Any]] | None:
    if "sched_" not in html_text:
        return None
    parser = _FBrefScheduleParser()
    parser.feed(html_text)
    out: list[dict[str, Any]] = []
    for row in parser.rows:
        d = row.get("date", "")
        h = row.get("home_team", "")
        a = row.get("away_team", "")
        hxg = row.get("home_xg", "")
        axg = row.get("away_xg", "")
        if not d or not h or not a or not hxg or not axg:
            continue
        try:
            h_val, a_val = float(hxg), float(axg)
        except ValueError:
            continue
        st = row.get("start_time", "").strip()
        hhmm = st[:5] if len(st) >= 5 and ":" in st else "15:00"
        out.append(
            _normalize_record(
                date_str=f"{d}T{hhmm}:00+00:00",
                home_team=h,
                away_team=a,
                home_xg=h_val,
                away_xg=a_val,
                source="fbref",
            )
        )
    return out if out else None


def _parse_manual_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    out: list[dict[str, Any]] = []
    if suffix == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if "xG" in item and isinstance(item["xG"], dict):
                if not item.get("isResult", True):
                    continue
                xg = item["xG"]
                if xg.get("h") is None or xg.get("a") is None:
                    continue
                out.append(
                    _normalize_record(
                        date_str=str(item.get("datetime") or ""),
                        home_team=str((item.get("h") or {}).get("title") or ""),
                        away_team=str((item.get("a") or {}).get("title") or ""),
                        home_xg=float(xg["h"]),
                        away_xg=float(xg["a"]),
                        source="understat",
                    )
                )
            else:
                h = str(item.get("home_team") or item.get("home") or "").strip()
                a = str(item.get("away_team") or item.get("away") or "").strip()
                d = str(item.get("kickoff_utc") or item.get("date") or "").strip()
                hxg = item.get("home_xg")
                axg = item.get("away_xg")
                src = str(item.get("source") or "fbref")
                if h and a and d and hxg is not None and axg is not None:
                    out.append(
                        _normalize_record(
                            date_str=d,
                            home_team=h,
                            away_team=a,
                            home_xg=float(hxg),
                            away_xg=float(axg),
                            source=src,
                        )
                    )
        return out

    if suffix == ".csv":
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        headers = next(reader, [])
        if not headers:
            return []
        # Handle duplicate FBref 'xG' columns: first -> home_xg, second -> away_xg
        xg_indices = [idx for idx, col in enumerate(headers) if col.strip().lower() == "xg"]
        col_map = {col.strip(): idx for idx, col in enumerate(headers)}
        for row in reader:
            if len(row) < len(headers):
                continue
            date_val = (
                row[col_map["Date"]] if "Date" in col_map
                else (row[col_map["date"]] if "date" in col_map else "")
            ).strip()
            home_val = (
                row[col_map["Home"]] if "Home" in col_map
                else (row[col_map["home_team"]] if "home_team" in col_map else "")
            ).strip()
            away_val = (
                row[col_map["Away"]] if "Away" in col_map
                else (row[col_map["away_team"]] if "away_team" in col_map else "")
            ).strip()
            if "home_xg" in col_map and "away_xg" in col_map:
                hxg_raw, axg_raw = row[col_map["home_xg"]], row[col_map["away_xg"]]
            elif len(xg_indices) >= 2:
                hxg_raw, axg_raw = row[xg_indices[0]], row[xg_indices[1]]
            else:
                continue
            if not date_val or not home_val or not away_val:
                continue
            try:
                h_val, a_val = float(hxg_raw), float(axg_raw)
            except ValueError:
                continue
            out.append(
                _normalize_record(
                    date_str=date_val,
                    home_team=home_val,
                    away_team=away_val,
                    home_xg=h_val,
                    away_xg=a_val,
                    source="fbref",
                )
            )
    return out


def _fetch_fpl_opta_github(season: int) -> tuple[list[dict[str, Any]] | None, str]:
    slug = FPL_GITHUB_SLUGS.get(season)
    if not slug:
        return None, f"UNAVAILABLE (unsupported season {season})"
    base = f"https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{slug}"
    teams_csv, t_status = fetch_with_backoff(f"{base}/teams.csv")
    if teams_csv is None:
        return None, t_status
    gw_csv, g_status = fetch_with_backoff(f"{base}/gws/merged_gw.csv")
    if gw_csv is None:
        return None, g_status

    id_to_name: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(teams_csv)):
        tid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if tid and name:
            id_to_name[tid] = name

    fixtures: dict[tuple[str, str], dict[str, Any]] = {}
    for row in csv.DictReader(io.StringIO(gw_csv)):
        fix_id = str(row.get("fixture") or "").strip()
        kickoff = str(row.get("kickoff_time") or "").strip()
        if not fix_id or not kickoff:
            continue
        xg_raw = row.get("expected_goals")
        if xg_raw is None or str(xg_raw).strip() == "":
            continue
        try:
            xg_val = float(xg_raw)
        except ValueError:
            continue
        if not math.isfinite(xg_val) or xg_val < 0.0:
            continue

        was_home = str(row.get("was_home") or "").strip().lower() == "true"
        raw_team = str(row.get("team") or "").strip()
        raw_opp = str(row.get("opponent_team") or "").strip()
        team_name = id_to_name.get(raw_team, raw_team)
        opp_name = id_to_name.get(raw_opp, raw_opp)
        if not team_name or not opp_name:
            continue

        entry = fixtures.setdefault(
            (fix_id, kickoff),
            {
                "kickoff": kickoff,
                "home_team": None,
                "away_team": None,
                "home_xg": 0.0,
                "away_xg": 0.0,
                "seen_home": False,
                "seen_away": False,
            },
        )
        if was_home:
            entry["home_team"] = team_name
            entry["away_team"] = opp_name
            entry["home_xg"] += xg_val
            entry["seen_home"] = True
        else:
            entry["away_team"] = team_name
            entry["home_team"] = opp_name
            entry["away_xg"] += xg_val
            entry["seen_away"] = True

    out: list[dict[str, Any]] = []
    for (_fid, _k), entry in sorted(fixtures.items(), key=lambda kv: (kv[1]["kickoff"], kv[0][0])):
        if not (entry["seen_home"] and entry["seen_away"]):
            continue
        if not entry["home_team"] or not entry["away_team"]:
            continue
        out.append(
            _normalize_record(
                date_str=entry["kickoff"],
                home_team=entry["home_team"],
                away_team=entry["away_team"],
                home_xg=round(entry["home_xg"], 4),
                away_xg=round(entry["away_xg"], 4),
                source="fpl_opta_github",
            )
        )
    return (out if out else None), ("OK" if out else "UNAVAILABLE (no finished xG rows)")


def _find_manual_fallback(manual_dir: Path, season: int) -> list[dict[str, Any]] | None:
    if not manual_dir.is_dir():
        return None
    span = FBREF_SEASON_SPANS.get(season, str(season))
    short_span = f"{str(season)[-2:]}{str(season + 1)[-2:]}"
    tokens = (str(season), span, short_span)
    for candidate in sorted(manual_dir.iterdir()):
        if not candidate.is_file() or candidate.suffix.lower() not in (".csv", ".json"):
            continue
        if any(tok in candidate.name for tok in tokens):
            records = _parse_manual_file(candidate)
            if records:
                return records
    return None


def _file_sha256_short(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def run_fetch(
    *,
    seasons: list[int],
    output_dir: Path,
    manual_dir: Path,
    source: str,
    force: bool,
    delay: float,
    import_db: bool,
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manual_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, Any]] = []
    network_calls = 0

    for season in seasons:
        cache_path = output_dir / f"pl_xg_{season}.json"
        if not force:
            cached = check_local_cache(cache_path, season)
            if cached is not None:
                rec_src = str(cached[0].get("source") or source)
                print(f"[CACHE HIT] season={season} -> {cache_path} ({len(cached)} matches, sha={_file_sha256_short(cache_path)})")
                summary_rows.append(
                    {"season": season, "source": rec_src, "status": "CACHED", "matches": len(cached), "path": cache_path}
                )
                continue

        if network_calls > 0 and delay > 0:
            time.sleep(max(3.0, delay))

        records: list[dict[str, Any]] | None = None
        used_source = source
        status_reason = "UNAVAILABLE"

        if source in ("auto", "fpl_opta_github"):
            network_calls += 1
            records, status_reason = _fetch_fpl_opta_github(season)
            used_source = "fpl_opta_github"
        if records is None and source in ("auto", "fbref"):
            span = FBREF_SEASON_SPANS[season]
            url = f"https://fbref.com/en/comps/9/{span}/schedule/{span}-Premier-League-Scores-and-Fixtures"
            network_calls += 1
            body, status_reason = fetch_with_backoff(url)
            if body is not None:
                records = _parse_fbref_html(body)
                used_source = "fbref"
        if records is None and source in ("auto", "understat"):
            if network_calls > 0 and delay > 0:
                time.sleep(max(3.0, delay))
            url = f"https://understat.com/league/EPL/{season}"
            network_calls += 1
            body, status_reason = fetch_with_backoff(url)
            if body is not None:
                records = _parse_understat_html(body)
                used_source = "understat"

        if records:
            cache_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
            summary_rows.append(
                {"season": season, "source": used_source, "status": "DOWNLOADED", "matches": len(records), "path": cache_path}
            )
            continue

        manual_records = _find_manual_fallback(manual_dir, season)
        if manual_records:
            rec_src = str(manual_records[0].get("source") or "fbref")
            cache_path.write_text(json.dumps(manual_records, indent=2), encoding="utf-8")
            summary_rows.append(
                {"season": season, "source": rec_src, "status": "MANUAL_FALLBACK", "matches": len(manual_records), "path": cache_path}
            )
            continue

        print(f"[PENDING] season={season} remote={status_reason}; drop CSV/JSON into {manual_dir}/")
        summary_rows.append(
            {"season": season, "source": source, "status": "PENDING", "matches": 0, "path": None}
        )

    print("\nSEASON | SOURCE     | STATUS          | MATCHES")
    print("-------+------------+-----------------+--------")
    for row in summary_rows:
        print(
            f"{row['season']:<6} | {row['source']:<10} | {row['status']:<15} | {row['matches']}"
        )

    if import_db:
        import sys
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from db.db_paths import resolve_database_path
        from db.xg_store import import_xg_records
        from laboratory.store import checkpoint_wal, connect

        ready = [r for r in summary_rows if r["path"] is not None and r["matches"] > 0]
        if ready:
            conn = connect(resolve_database_path())
            try:
                for r in ready:
                    recs = json.loads(Path(r["path"]).read_text(encoding="utf-8"))
                    res = import_xg_records(
                        conn,
                        recs,
                        source=str(r["source"]),
                        competition="PL",
                        season=int(r["season"]),
                    )
                    print(f"[DB IMPORT] season={r['season']} source={r['source']}: {res}")
                checkpoint_wal(conn, mode="TRUNCATE")
            finally:
                conn.close()

    return summary_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart resilient EPL xG ingestion CLI")
    parser.add_argument("--seasons", type=int, nargs="+", default=[2022, 2023, 2024, 2025])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--manual-dir", type=Path, default=DEFAULT_MANUAL_DIR)
    parser.add_argument("--source", choices=("auto", "fpl_opta_github", "fbref", "understat"), default="auto")
    parser.add_argument("--force", action="store_true", help="Bypass local cache check.")
    parser.add_argument("--delay", type=float, default=3.5, help="Polite delay >= 3.0s between requests.")
    parser.add_argument("--import-db", action="store_true", help="Auto-import ready seasons into match_xg.")
    args = parser.parse_args()
    run_fetch(
        seasons=args.seasons,
        output_dir=args.output_dir,
        manual_dir=args.manual_dir,
        source=args.source,
        force=args.force,
        delay=args.delay,
        import_db=args.import_db,
    )


if __name__ == "__main__":
    main()
