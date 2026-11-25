"""Download free Premier League closing odds CSVs from football-data.co.uk."""

from __future__ import annotations

import argparse
import csv
import io
import ssl
import urllib.error
import urllib.request
from pathlib import Path

SEASON_CODES: dict[int, str] = {
    2022: "2223",
    2023: "2324",
    2024: "2425",
    2025: "2526",
}

DEFAULT_OUTPUT_DIR = Path("data/odds/pl")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _validate_csv_headers(raw_text: str, path: Path) -> list[str]:
    reader = csv.reader(io.StringIO(raw_text))
    headers = next(reader, [])
    header_set = set(headers)
    for req in ("Date", "HomeTeam", "AwayTeam"):
        if req not in header_set:
            raise ValueError(f"{path}: missing required header {req!r}")
    has_ps = {"PSCH", "PSCD", "PSCA"}.issubset(header_set)
    has_b365 = {"B365CH", "B365CD", "B365CA"}.issubset(header_set)
    has_avg = {"AvgCH", "AvgCD", "AvgCA"}.issubset(header_set)
    if not (has_ps or has_b365 or has_avg):
        raise ValueError(
            f"{path}: missing closing 1X2 odds headers (PSCH/B365CH/AvgCH)"
        )
    return headers


def fetch_season_csv(season: int, output_dir: Path) -> Path | None:
    if season not in SEASON_CODES:
        raise ValueError(f"Unsupported season {season}; expected one of {sorted(SEASON_CODES)}")
    code = SEASON_CODES[season]
    url = f"https://www.football-data.co.uk/mmz4281/{code}/E0.csv"
    dest = output_dir / f"E0_{season}.csv"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"[404] {url} not available; skipping season {season}")
            return None
        raise
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    _validate_csv_headers(text, dest)
    output_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    row_count = max(0, len(text.splitlines()) - 1)
    print(f"[OK] season={season} ({code}) -> {dest} ({row_count} rows)")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch PL closing odds from football-data.co.uk")
    parser.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=[2022, 2023, 2024, 2025],
        help="Start years to download (default: 2022 2023 2024 2025)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Destination directory (default: data/odds/pl)",
    )
    args = parser.parse_args()
    for season in args.seasons:
        fetch_season_csv(season, args.output_dir)


if __name__ == "__main__":
    main()
