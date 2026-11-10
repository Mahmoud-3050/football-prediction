#!/usr/bin/env bash
# Safe SQLite backup for the shared football DB (includes WAL data via .backup).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DB_PATH="${DATABASE_PATH:-data/football.db}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${BACKUP_DIR:-backups}"
KEEP="${BACKUP_KEEP:-14}"
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/football-${STAMP}.db"

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

sqlite3 "$DB_PATH" ".backup '$OUT_FILE'"

# Prune older backups; keep the newest $KEEP files.
index=0
while IFS= read -r path; do
  index=$((index + 1))
  if (( index > KEEP )); then
    rm -f "$path"
  fi
done < <(ls -1t "$OUT_DIR"/football-*.db 2>/dev/null || true)

echo "Backup written: $OUT_FILE"
du -h "$OUT_FILE"
