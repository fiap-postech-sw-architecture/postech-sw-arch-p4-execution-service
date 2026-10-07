# Gates locais (os mesmos do CI) e atalhos do ambiente de dev.
# Os testes de integracao sobem PostgreSQL via testcontainers: precisam de
# Docker (no colima o tests/conftest.py ajusta DOCKER_HOST sozinho).

PY := uv run
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: install lock-check lint format typecheck security lint-arch test check \
	smoke compose-up compose-down compose-logs migrate seed run

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

# Smoke da imagem pelo entrypoint real (migracao, seed), o job build do CI: sobe
# a stack, confere a readiness (banco), que rota autenticada sem token responde
# 401, que a imagem roda como 1001:1001, que a resposta nao traz o header
# `server`, que as linhas de boot do uvicorn saem em JSON e que ele nao escreve
# access log (so o `http_request` estruturado do middleware); que relay e
# consumidor estao saudaveis (broker e banco conectados, heartbeat em dia), que
# os contratos de mensageria estao na imagem (e a topologia do RabbitMQ, com a
# senha do admin de demonstracao, nao), que o relay conectou ao broker e
# que o consumidor assinou a execucao.comandos; e
# derruba tudo com os volumes, inclusive em falha (depois de mostrar os logs).
# Projeto e portas proprios para nao derrubar a stack do compose-up.
API_IMAGE ?= pytstop-execution-service:dev
SMOKE_PORT ?= 18003
SMOKE_DB_PORT ?= 15433
SMOKE_RABBITMQ_PORT ?= 15674
SMOKE_RABBITMQ_UI_PORT ?= 15675
SMOKE_URL := http://127.0.0.1:$(SMOKE_PORT)
# Schemas e AsyncAPI dentro da imagem (sem eles a API recusa gravar a outbox), e
# a topologia do RabbitMQ fora dela (leva a senha de demonstracao do admin).
SMOKE_CONTRATOS := from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, produtor, tipos_com_contrato; assert tipos_com_contrato() and produtor('ReservarPecas') == 'os'; assert not (CONTRATOS / 'rabbitmq').exists() and not (CONTRATOS / 'exemplos').exists()
SMOKE_COMPOSE := API_PORT=$(SMOKE_PORT) DB_PORT=$(SMOKE_DB_PORT) \
	RABBITMQ_PORT=$(SMOKE_RABBITMQ_PORT) RABBITMQ_UI_PORT=$(SMOKE_RABBITMQ_UI_PORT) \
	API_IMAGE=$(API_IMAGE) $(COMPOSE) -p pytstop-execucao-smoke

smoke:
	@status=0; \
	$(SMOKE_COMPOSE) up -d --build --wait \
	&& curl -fsS --max-time 5 $(SMOKE_URL)/api/v1/saude/pronto && echo \
	&& codigo="$$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 $(SMOKE_URL)/api/v1/estoque)" \
	&& test "$$codigo" = 401 \
	&& { test "$$(docker image inspect -f '{{.Config.User}}' $(API_IMAGE))" = 1001:1001 \
		|| { echo "smoke: a imagem nao roda como 1001:1001" >&2; false; }; } \
	&& cabecalhos="$$(curl -fsS --max-time 5 -D - -o /dev/null $(SMOKE_URL)/api/v1/saude)" \
	&& { ! printf '%s\n' "$$cabecalhos" | grep -qi '^server:' \
		|| { echo "smoke: a resposta traz o header server" >&2; false; }; } \
	&& logs="$$($(SMOKE_COMPOSE) logs --no-color api)" \
	&& { printf '%s\n' "$$logs" | grep -q '"event": "Started server process' \
		|| { echo "smoke: as linhas de boot do uvicorn nao saem em JSON" >&2; false; }; } \
	&& { printf '%s\n' "$$logs" | grep -q '"event": "http_request"' \
		|| { echo "smoke: o access log estruturado (http_request) nao saiu" >&2; false; }; } \
	&& { ! printf '%s\n' "$$logs" | grep -q 'uvicorn.access' \
		|| { echo "smoke: o uvicorn escreveu access log (--no-access-log nao vale)" >&2; false; }; } \
	&& { test "$$(docker inspect -f '{{.State.Health.Status}}' "$$($(SMOKE_COMPOSE) ps -q relay)")" = healthy \
		|| { echo "smoke: o relay nao esta pronto" >&2; false; }; } \
	&& { test "$$(docker inspect -f '{{.State.Health.Status}}' "$$($(SMOKE_COMPOSE) ps -q consumidor)")" = healthy \
		|| { echo "smoke: o consumidor nao esta pronto" >&2; false; }; } \
	&& { $(SMOKE_COMPOSE) logs --no-color relay | grep -q '"event": "relay_broker_connected"' \
		|| { echo "smoke: o relay nao conectou ao broker" >&2; false; }; } \
	&& { $(SMOKE_COMPOSE) exec -T api python -c "$(SMOKE_CONTRATOS)" \
		|| { echo "smoke: a imagem nao traz os contratos de mensageria, ou traz a topologia" >&2; false; }; } \
	&& { $(SMOKE_COMPOSE) exec -T rabbitmq rabbitmqctl -q list_consumers queue_name \
		| grep -qx 'execucao.comandos' \
		|| { echo "smoke: o consumidor nao assinou a execucao.comandos" >&2; false; }; } \
	&& echo "smoke ok: readiness 200, 401 sem token, usuario 1001:1001, sem header server, boot em JSON, sem access log do uvicorn, contratos na imagem e topologia fora, relay e consumidor prontos" \
	|| status=$$?; \
	if [ $$status -ne 0 ]; then $(SMOKE_COMPOSE) logs --no-color --tail=200; fi; \
	$(SMOKE_COMPOSE) down -v; \
	exit $$status

# Stack local: API, relay, consumidor, PostgreSQL 16 e RabbitMQ 4.3.6 proprios,
# migracoes e seed do estoque no boot.
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
