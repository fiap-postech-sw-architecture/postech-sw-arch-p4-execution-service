# Gates locais (os mesmos do CI) e atalhos do ambiente de dev.
# Os testes de integracao sobem PostgreSQL via testcontainers: precisam de
# Docker (no colima o tests/conftest.py ajusta DOCKER_HOST sozinho).

PY := uv run
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: install lock-check lint format typecheck security lint-arch test check \
	compose-up compose-down compose-logs migrate seed run

install:
	uv sync --frozen

lock-check:
	uv lock --check

lint:
	$(PY) ruff check .
	$(PY) ruff format --check .

format:
	$(PY) ruff format .
	$(PY) ruff check --fix .

typecheck:
	$(PY) mypy src

security:
	$(PY) bandit -r src -q

lint-arch:
	$(PY) lint-imports

# Unitarios + integracao (testcontainers) com o gate de cobertura do .coveragerc.
# coverage.xml alimenta o SonarQube e o resumo do CI; htmlcov/ e reports/ viram
# artefato do job `test`.
test:
	$(PY) pytest --cov-report=xml:coverage.xml --cov-report=html:htmlcov --junitxml=reports/junit.xml

check: lock-check lint lint-arch typecheck security test
	@echo "Todos os gates passaram"

# Stack local: API + PostgreSQL 16 proprio, migracoes e seed do estoque no boot.
compose-up:
	$(COMPOSE) up -d --build --wait

compose-down:
	$(COMPOSE) down -v

compose-logs:
	$(COMPOSE) logs -f api

# Contra o banco apontado por DATABASE_URL (ex.: o do compose, porta 5433;
# valores de demonstracao em .env.example).
migrate:
	$(PY) alembic upgrade head

seed:
	$(PY) python -m src.estoque.infraestrutura.seed

# Porta 8003, a mesma do compose (OS em 8000, Billing em 8002).
run:
	$(PY) uvicorn src.main:app --reload --port 8003
