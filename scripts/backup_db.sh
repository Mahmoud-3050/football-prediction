#!/usr/bin/env bash
# Safe SQLite backup for shared football.db and lab.db (includes WAL data via .backup).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

FOOTBALL_DB="${DATABASE_PATH:-data/football.db}"
LAB_DB="${LAB_DATABASE_PATH:-data/lab.db}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${BACKUP_DIR:-backups}"
KEEP="${BACKUP_KEEP:-14}"
COMPRESS="${BACKUP_COMPRESS:-0}"
TIMEOUT_MS="${BACKUP_TIMEOUT_MS:-60000}"

mkdir -p "$OUT_DIR"

if [[ ! -f "$FOOTBALL_DB" ]]; then
  echo "Database not found: $FOOTBALL_DB" >&2
  exit 1
fi

backup_one() {
  local src="$1"
  local prefix="$2"
  local tmp_file="$OUT_DIR/.${prefix}-${STAMP}.db.tmp"
  local out_file="$OUT_DIR/${prefix}-${STAMP}.db"

  rm -f "$tmp_file"
  sqlite3 -cmd ".timeout $TIMEOUT_MS" "$src" ".backup '$tmp_file'"

  local integrity
  integrity="$(sqlite3 "$tmp_file" "PRAGMA integrity_check;")"
  if [[ "$integrity" != "ok" ]]; then
    rm -f "$tmp_file"
    echo "Backup integrity check failed for $src: $integrity" >&2
    exit 1
  fi

  if [[ "$COMPRESS" == "1" ]]; then
    gzip -f "$tmp_file"
    out_file="${out_file}.gz"
    mv -f "${tmp_file}.gz" "$out_file"
  else
    mv -f "$tmp_file" "$out_file"
  fi

  local index=0
  while IFS= read -r path; do
    index=$((index + 1))
    if (( index > KEEP )); then
      rm -f "$path"
    fi
  done < <(ls -1t "$OUT_DIR/${prefix}"-*.db* 2>/dev/null || true)

  echo "Backup written (integrity=ok): $out_file"
  du -h "$out_file"
}

backup_one "$FOOTBALL_DB" "football"

if [[ -f "$LAB_DB" && "$LAB_DB" != "$FOOTBALL_DB" ]]; then
  backup_one "$LAB_DB" "lab"
fi
