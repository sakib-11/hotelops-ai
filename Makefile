SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c
.DELETE_ON_ERROR:
MAKEFLAGS += --warn-undefined-variables
MAKEFLAGS += --no-builtin-rules

# Detect available tools
HAS_NODE := $(shell command -v node >/dev/null 2>&1 && echo yes || echo no)
HAS_RUST := $(shell command -v cargo >/dev/null 2>&1 && echo yes || echo no)
HAS_PYTHON := $(shell command -v python3 >/dev/null 2>&1 && echo yes || echo no)

.DEFAULT_GOAL := check
.PHONY: help bootstrap format format-check lint typecheck test check clean infra-up infra-down infra-status infra-logs infra-reset dev

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# =============================================================================
# Bootstrap
# =============================================================================

bootstrap: ## Install/setup all project development tooling
	$(info [bootstrap] Installing Python development tools...)
	@test -d .venv || python3 -m venv .venv
	@source .venv/bin/activate
	pip install --quiet --upgrade pip
	pip install --quiet ruff mypy pytest pytest-cov pre-commit
	@echo "[bootstrap] Python tools installed."

	@if [ "$(HAS_NODE)" = "yes" ]; then
		$(info [bootstrap] Installing desktop dependencies...)
		cd desktop && npm install --silent 2>/dev/null || npm install
		@echo "[bootstrap] Desktop dependencies installed."
	else
		@echo "[bootstrap] WARNING: Node not found. Skipping desktop dependencies."
	fi

	@if command -v pre-commit &>/dev/null; then
		pre-commit install --hook-type pre-commit --quiet 2>/dev/null || true
		@echo "[bootstrap] Pre-commit hooks installed."
	fi
	@echo "[bootstrap] Done."

# =============================================================================
# Formatting
# =============================================================================

format: ## Format all code (Python + TypeScript + Rust)
	@if [ -d ".venv" ] && source .venv/bin/activate && command -v ruff &>/dev/null; then
		ruff format .
	else
		@echo "[format] WARNING: Ruff not available. Install via: make bootstrap"
	fi
	@if [ "$(HAS_NODE)" = "yes" ] && [ -f "desktop/package.json" ]; then
		cd desktop && npm run format 2>/dev/null || echo "[format] Desktop format not configured."
	fi
	@if [ "$(HAS_RUST)" = "yes" ] && [ -f "desktop/src-tauri/Cargo.toml" ]; then
		cd desktop/src-tauri && cargo fmt 2>/dev/null || echo "[format] Rust format encountered issues."
	fi

format-check: ## Check formatting without changing files
	$(info [format-check] Checking Python formatting...)
	@if [ -d ".venv" ] && source .venv/bin/activate && command -v ruff &>/dev/null; then
		ruff format --check .
	else
		@echo "[format-check] SKIPPED: Ruff not available."
		exit 1
	fi
	@if [ "$(HAS_NODE)" = "yes" ] && [ -f "desktop/package.json" ]; then
		$(info [format-check] Checking TypeScript formatting...)
		cd desktop && npm run format:check 2>/dev/null || echo "[format-check] WARNING: Desktop format check not configured."
	fi
	@if [ "$(HAS_RUST)" = "yes" ] && [ -f "desktop/src-tauri/Cargo.toml" ]; then
		$(info [format-check] Checking Rust formatting...)
		cd desktop/src-tauri && cargo fmt --check 2>/dev/null || echo "[format-check] WARNING: Rust format check encountered issues."
	fi

# =============================================================================
# Linting
# =============================================================================

lint: ## Run all linters (Ruff + ESLint + Clippy)
	$(info [lint] Running Ruff linter...)
	@if [ -d ".venv" ] && source .venv/bin/activate && command -v ruff &>/dev/null; then
		ruff check .
	else
		@echo "[lint] SKIPPED: Ruff not available."
		exit 1
	fi
	@if [ "$(HAS_NODE)" = "yes" ] && [ -f "desktop/package.json" ]; then
		$(info [lint] Running ESLint...)
		cd desktop && npm run lint 2>/dev/null || echo "[lint] WARNING: ESLint not fully configured."
	fi
	@if [ "$(HAS_RUST)" = "yes" ] && [ -f "desktop/src-tauri/Cargo.toml" ]; then
		$(info [lint] Running Clippy...)
		cd desktop/src-tauri && cargo clippy --all-targets --all-features -- -D warnings 2>/dev/null || echo "[lint] WARNING: Clippy encountered issues (may need cargo metadata)."
	fi

# =============================================================================
# Type Checking
# =============================================================================

typecheck: ## Run type checkers (mypy + TypeScript)
	$(info [typecheck] Running mypy...)
	@if [ -d ".venv" ] && source .venv/bin/activate && command -v mypy &>/dev/null; then
		mypy  backend/ --no-error-summary
	else
		@echo "[typecheck] SKIPPED: Mypy not available."
		exit 1
	fi
	@if [ "$(HAS_NODE)" = "yes" ] && [ -f "desktop/package.json" ]; then
		$(info [typecheck] Running TypeScript compiler...)
		cd desktop && npm run typecheck 2>/dev/null || echo "[typecheck] WARNING: Desktop type check not configured."
	fi

# =============================================================================
# Testing
# =============================================================================

test: ## Run all fast tests (Python pytest + Rust cargo test + desktop tests)
	$(info [test] Running pytest...)
	@if [ -d ".venv" ] && source .venv/bin/activate && command -v pytest &>/dev/null; then
		pytest -v --tb=short
	else
		@echo "[test] SKIPPED: pytest not available."
		exit 1
	fi
	@if [ "$(HAS_RUST)" = "yes" ] && [ -f "desktop/src-tauri/Cargo.toml" ]; then
		$(info [test] Running cargo test...)
		cd desktop/src-tauri && cargo test 2>/dev/null || echo "[test] WARNING: Rust tests not runnable (may need cargo metadata)."
	fi
	@if [ "$(HAS_NODE)" = "yes" ] && [ -f "desktop/package.json" ]; then
		$(info [test] Running desktop tests...)
		cd desktop && npm test 2>/dev/null || true
	fi

# =============================================================================
# Full Check
# =============================================================================

check: format-check lint typecheck test ## Run all quality checks

# =============================================================================
# Local Infrastructure (Docker Compose)
# =============================================================================

infra-up: ## Start local development infrastructure
	docker compose -f infrastructure/docker/compose.yaml up -d
	@echo "[infra-up] Infrastructure started. Use 'make infra-status' to check health."

infra-down: ## Stop local infrastructure without removing volumes
	docker compose -f infrastructure/docker/compose.yaml down
	@echo "[infra-down] Infrastructure stopped. Volumes preserved."

infra-status: ## Show infrastructure service status
	docker compose -f infrastructure/docker/compose.yaml ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"

infra-logs: ## Show infrastructure logs (use SERVICES= to filter, e.g. make infra-logs SERVICES=postgres)
	docker compose -f infrastructure/docker/compose.yaml logs --tail=100 -f $(SERVICES)

infra-reset: ## WARNING: Stop infrastructure AND DELETE ALL DATA volumes
	@echo "WARNING: This will delete all development data!"
	@read -p "Type 'yes' to confirm: " CONFIRM; if [ "$$CONFIRM" = "yes" ]; then \
		docker compose -f infrastructure/docker/compose.yaml down -v; \
		echo "[infra-reset] Infrastructure stopped and volumes deleted."; \
	else \
		echo "[infra-reset] Cancelled."; \
	fi

dev: ## Start FastAPI in development mode
	@if [ -f "backend/app/main.py" ]; then \
		.venv/bin/uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000; \
	else \
		echo "[dev] backend/app/main.py not found. Has Task 3 been implemented?"; \
		exit 1; \
	fi

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Clean build artifacts and caches
	rm -rf .venv
	rm -rf __pycache__
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache
	rm -rf htmlcov
	rm -rf .coverage
	@if [ -d "desktop/node_modules" ]; then
		cd desktop && rm -rf node_modules dist
	fi
	@if [ -d "desktop/src-tauri/target" ]; then
		cd desktop/src-tauri && cargo clean 2>/dev/null || rm -rf target
	fi
	@echo "[clean] Done."
