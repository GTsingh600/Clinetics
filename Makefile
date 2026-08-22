# Clinetics developer commands.
#   make help   -> list every target
#
# Windows note: run these from Git Bash, or use the raw commands shown in
# docs/00-phase-0-scaffold.md if you do not have `make` installed.

SHELL := /bin/bash
PY    := backend/.venv/Scripts/python
UV    := uv

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

# --- Infrastructure ---------------------------------------------------------

.PHONY: up
up: ## Start Postgres + Redis
	docker compose up -d
	@echo "waiting for healthy services..."
	@docker compose ps

.PHONY: down
down: ## Stop containers (keeps data volumes)
	docker compose down

.PHONY: nuke
nuke: ## Stop containers AND delete all data volumes
	docker compose down -v

.PHONY: logs
logs: ## Tail infrastructure logs
	docker compose logs -f

.PHONY: psql
psql: ## Open a psql shell on the dev database
	docker compose exec db psql -U clinetics -d clinetics

# --- Backend ----------------------------------------------------------------

.PHONY: install
install: ## Install backend + frontend dependencies (exact locked versions)
	cd backend && $(UV) sync --extra ml --group dev --frozen
	cd frontend && npm install

.PHONY: lock
lock: ## Re-resolve uv.lock after changing pyproject dependencies
	cd backend && $(UV) lock

.PHONY: api
api: ## Run the API with hot reload
	cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

.PHONY: worker
worker: ## Run the Celery worker
	cd backend && .venv/Scripts/python -m celery -A app.workers.celery_app.celery_app worker --loglevel=info --pool=solo

.PHONY: migrate
migrate: ## Apply all migrations
	cd backend && .venv/Scripts/python -m alembic upgrade head

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="add doctor table"
	cd backend && .venv/Scripts/python -m alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	cd backend && .venv/Scripts/python -m alembic downgrade -1

# --- Data ------------------------------------------------------------------

.PHONY: seed
seed: ## Generate synthetic data (destructive: --reset truncates first)
	cd backend && $(UV) run python scripts/generate_data.py --reset --months 36 --doctors 16 --patients 4000

.PHONY: validate
validate: ## THE VALIDATION GATE: prove the synthetic data has learnable structure
	cd backend && $(UV) run python scripts/validate_data.py

.PHONY: demo-users
demo-users: ## Create one login per role, linked to the busiest seeded records
	cd backend && $(UV) run python scripts/create_demo_users.py

# --- Models ----------------------------------------------------------------

.PHONY: train
train: ## Train the no-show, demand and duration models; write artifacts
	cd backend && $(UV) run python scripts/train_models.py

.PHONY: eval
eval: ## THE MODEL GATE: rolling-origin CV against the baselines, writes metrics
	cd backend && $(UV) run python scripts/evaluate.py

.PHONY: explain
explain: ## EXPLAIN ANALYZE the (doctor_id, appointment_date) index, with/without
	cd backend && $(UV) run python scripts/explain_index.py

.PHONY: test
test: ## Run backend tests
	cd backend && .venv/Scripts/python -m pytest

.PHONY: lint
lint: ## Lint, format-check, typecheck, and verify architecture purity
	cd backend && .venv/Scripts/python -m ruff check .
	cd backend && .venv/Scripts/python -m black --check .
	cd backend && .venv/Scripts/python -m mypy app forecasting optimizer scripts
	cd backend && .venv/Scripts/python scripts/check_purity.py

.PHONY: fmt
fmt: ## Auto-format backend code
	cd backend && .venv/Scripts/python -m black .
	cd backend && .venv/Scripts/python -m ruff check --fix .

# --- Frontend ---------------------------------------------------------------

.PHONY: web
web: ## Run the Next.js dev server
	cd frontend && npm run dev

.PHONY: web-build
web-build: ## Production build of the frontend
	cd frontend && npm run build

.PHONY: check
check: lint test web-build ## Everything CI runs, locally
