#!/usr/bin/env bash
# Start bot + jobs + dashboard on this Mac. Ctrl+C stops all three.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Create it with:"
  echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example and fill BOT_TOKEN, FOOTBALL_API_KEY, GEMINI_API_KEY."
  exit 1
fi

# Load dashboard host/port for the status line (optional).
set -a
# shellcheck disable=SC1091
source .env
set +a

PYTHON="$ROOT/.venv/bin/python"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8765}"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"

BOT_PID=""
JOBS_PID=""
DASH_PID=""

cleanup() {
  trap - EXIT INT TERM
  echo ""
  echo "Stopping Football Predictor…"
  for pid in "$DASH_PID" "$JOBS_PID" "$BOT_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
  echo "Stopped."
}
trap cleanup EXIT INT TERM

# Drop a stale dashboard listener on the same port (best-effort).
if command -v lsof >/dev/null 2>&1; then
  stale="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$stale" ]]; then
    echo "Freeing port $PORT (pid $stale)…"
    kill $stale 2>/dev/null || true
    sleep 0.3
  fi
fi

echo "Starting Telegram bot…"
"$PYTHON" -u main.py >>"$LOG_DIR/bot.log" 2>&1 &
BOT_PID=$!

echo "Starting server jobs…"
"$PYTHON" -u -m jobs >>"$LOG_DIR/jobs.log" 2>&1 &
JOBS_PID=$!

echo "Starting laboratory dashboard…"
"$PYTHON" -u -m laboratory serve --host "$HOST" --port "$PORT" >>"$LOG_DIR/dashboard.log" 2>&1 &
DASH_PID=$!

sleep 0.8
echo ""
echo "Football Predictor is running"
echo "  Dashboard  http://${HOST}:${PORT}"
echo "  Logs       $LOG_DIR/{bot,jobs,dashboard}.log"
echo "  Stop       Ctrl+C"
echo ""

# Exit if any child dies; otherwise wait until Ctrl+C.
while true; do
  for pid in "$BOT_PID" "$JOBS_PID" "$DASH_PID"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "A process exited unexpectedly — check $LOG_DIR" >&2
      exit 1
    fi
  done
  sleep 2
done
