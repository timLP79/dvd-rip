# DVD Rip — convenience targets.
#
# Override at invocation time, e.g.:
#   make up OUTPUT_DIR=/mnt/plex/movies DVD_DEVICE=/dev/sr1

SHELL := /bin/bash

OUTPUT_DIR ?= ./output
DVD_DEVICE ?= /dev/sr0
PORT ?= 8000

# Run the container as the invoking user so files in OUTPUT_DIR end up owned by
# them, and join the host's cdrom group so /dev/sr0 is readable.
PUID ?= $(shell id -u)
PGID ?= $(shell id -g)
CDROM_GID ?= $(shell getent group cdrom 2>/dev/null | cut -d: -f3 || echo 24)

# SCSI-generic node MakeMKV uses for the optical drive. Its /dev/sgN number shifts
# across reboots/USB reconnects, so derive it from the drive's sysfs entry rather
# than hardcoding (a stale value fails `make up` with "no such device"). Falls back
# to /dev/sg1 (the compose default) if the drive isn't present.
BD_SG_DEVICE ?= $(shell n=$$(ls /sys/block/$(notdir $(DVD_DEVICE))/device/scsi_generic/ 2>/dev/null | head -n1); if [ -n "$$n" ]; then echo /dev/$$n; else echo /dev/sg1; fi)

# TMDB_API_KEY is intentionally NOT in COMPOSE_ENV: an empty Makefile value would
# clobber whatever's in `.env` (docker compose substitutes the shell env first).
# Instead, let docker compose read it from `.env` or your exported shell env.
COMPOSE_ENV := OUTPUT_DIR=$(OUTPUT_DIR) DVD_DEVICE=$(DVD_DEVICE) \
	PUID=$(PUID) PGID=$(PGID) CDROM_GID=$(CDROM_GID) BD_SG_DEVICE=$(BD_SG_DEVICE)
COMPOSE := $(COMPOSE_ENV) docker compose
SERVICE := dvd-rip

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "DVD Rip make targets:\n\n"} \
		/^[a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf "\nVariables (override with make VAR=value):\n"
	@printf "  OUTPUT_DIR    where ripped MP4s go on host    [%s]\n" "$(OUTPUT_DIR)"
	@printf "  DVD_DEVICE    DVD device on host              [%s]\n" "$(DVD_DEVICE)"
	@printf "  PORT          host port for the web UI        [%s]\n" "$(PORT)"
	@printf "  PUID          uid to run the container as     [%s]\n" "$(PUID)"
	@printf "  PGID          gid to run the container as     [%s]\n" "$(PGID)"
	@printf "  CDROM_GID     host cdrom group gid            [%s]\n" "$(CDROM_GID)"
	@printf "  BD_SG_DEVICE  optical drive SCSI-generic node [%s]\n" "$(BD_SG_DEVICE)"

.PHONY: build
build: ## Build the docker image
	$(COMPOSE) build

.PHONY: up
up: ## Start the container in the foreground (Ctrl-C to stop)
	@mkdir -p "$(OUTPUT_DIR)"
	$(COMPOSE) up

.PHONY: up-d
up-d: ## Start the container detached
	@mkdir -p "$(OUTPUT_DIR)"
	$(COMPOSE) up -d
	@echo "Open http://localhost:$(PORT)"

.PHONY: down
down: check-no-active-jobs ## Stop and remove the container (warns if jobs are running)
	$(COMPOSE) down

.PHONY: restart
restart: check-no-active-jobs ## Restart the container (warns if jobs are running)
	$(COMPOSE) down
	@mkdir -p "$(OUTPUT_DIR)"
	$(COMPOSE) up -d
	@echo "Open http://localhost:$(PORT)"

# Bails out (exit 1) if the container is up and reports queued/running jobs,
# unless FORCE=1 is set. Used by `down` and `restart` so we don't silently
# kill HandBrake mid-rip.
.PHONY: check-no-active-jobs
check-no-active-jobs:
	@if [ "$(FORCE)" = "1" ]; then exit 0; fi; \
	if ! docker ps --filter name=$(SERVICE) --format '{{.Names}}' | grep -q '^$(SERVICE)$$'; then exit 0; fi; \
	ACTIVE=$$(curl -fsS http://localhost:$(PORT)/api/jobs 2>/dev/null \
	  | python3 -c "import json,sys; jobs=json.load(sys.stdin); print(sum(1 for j in jobs if j['status'] in ('running','queued')))" 2>/dev/null \
	  || echo 0); \
	if [ "$$ACTIVE" -gt 0 ]; then \
	  echo ""; \
	  echo "  ⚠  $$ACTIVE active job(s) — restarting will kill them and leave partial files."; \
	  echo "     Cancel the job(s) via the UI first, or re-run with FORCE=1 to override."; \
	  echo ""; \
	  exit 1; \
	fi

.PHONY: logs
logs: ## Tail container logs
	$(COMPOSE) logs -f --tail=200

.PHONY: shell
shell: ## Open a shell inside the running container
	$(COMPOSE) exec $(SERVICE) /bin/bash

.PHONY: scan
scan: ## Trigger a DVD scan via the API (container must be running)
	@curl -fsS http://localhost:$(PORT)/api/scan | python3 -m json.tool

.PHONY: jobs
jobs: ## List current rip jobs
	@curl -fsS http://localhost:$(PORT)/api/jobs | python3 -m json.tool

.PHONY: open
open: ## Open the web UI in the default browser (Linux)
	@xdg-open http://localhost:$(PORT) 2>/dev/null || echo "Visit http://localhost:$(PORT)"

.PHONY: test
test: venv ## Run the test suite in a local venv
	.venv/bin/python -m pytest tests/ -v

.PHONY: dev
dev: venv ## Run uvicorn locally with auto-reload (no docker)
	OUTPUT_ROOT=$(OUTPUT_DIR) DVD_DEVICE=$(DVD_DEVICE) \
		.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port $(PORT)

.PHONY: secrets
secrets: venv ## Print fresh SESSION_SECRET + VAPID keys for .env (phone push/auth)
	@.venv/bin/python -c "import secrets, base64; \
from py_vapid import Vapid01; \
from cryptography.hazmat.primitives import serialization as s; \
v=Vapid01(); v.generate_keys(); \
priv=v.private_key.private_numbers().private_value.to_bytes(32,'big'); \
pub=v.public_key.public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint); \
b=lambda x: base64.urlsafe_b64encode(x).rstrip(b'=').decode(); \
print('SESSION_SECRET='+secrets.token_hex(32)); \
print('VAPID_PRIVATE_KEY='+b(priv)); \
print('VAPID_PUBLIC_KEY='+b(pub)); \
print('VAPID_SUBJECT=mailto:you@example.com')"

.PHONY: venv
venv: .venv/bin/python ## Create local Python venv with dev deps

.venv/bin/python:
	python3 -m venv .venv
	.venv/bin/pip install --quiet -r requirements.txt pytest httpx

.PHONY: clean
clean: ## Remove the built image (does not touch output files)
	-$(COMPOSE) down --rmi local

.PHONY: clean-venv
clean-venv: ## Remove the local Python venv
	rm -rf .venv

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps
