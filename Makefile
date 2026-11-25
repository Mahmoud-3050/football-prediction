# Football Predictor — common local commands
PYTHON ?= .venv/bin/python
PORT   ?= 8765
HOST   ?= 127.0.0.1

.PHONY: help install test bot jobs daily-sync dash dash-restart dash-kill start kill restart backup

help:
	@echo "Targets:"
	@echo "  make install       Create .venv and install requirements"
	@echo "  make test          Run pytest"
	@echo "  make bot           Start Telegram bot"
	@echo "  make jobs          Start server jobs loop"
	@echo "  make daily-sync    Run one-shot sync, settle, tomorrow lock, and price-finished"
	@echo "  make dash          Start laboratory dashboard (:$(PORT))"
	@echo "  make dash-restart  Restart dashboard only"
	@echo "  make dash-kill     Stop process on :$(PORT)"
	@echo "  make start         Bot + jobs + dashboard (./start.sh)"
	@echo "  make backup        Backup football.db and lab.db to backups/"
	@echo "  make kill          Alias for dash-kill"
	@echo "  make restart       Alias for dash-restart"

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -r requirements.txt

test:
	$(PYTHON) -m pytest -q

bot:
	$(PYTHON) main.py

jobs:
	$(PYTHON) -m jobs

daily-sync:
	$(PYTHON) -m jobs.daily_sync

dash:
	$(PYTHON) -m laboratory serve --host $(HOST) --port $(PORT)

dash-kill kill:
	@pids=$$(lsof -tiTCP:$(PORT) -sTCP:LISTEN 2>/dev/null || true); \
	if [ -n "$$pids" ]; then kill $$pids; echo "Stopped PID(s) $$pids on :$(PORT)"; \
	else echo "Nothing listening on :$(PORT)"; fi

dash-restart restart: dash-kill
	@sleep 0.3
	$(PYTHON) -m laboratory serve --host $(HOST) --port $(PORT)

start:
	./start.sh

backup:
	./scripts/backup_db.sh
