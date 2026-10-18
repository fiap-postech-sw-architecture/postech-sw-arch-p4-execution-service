# Gates locais (os mesmos do CI) e atalhos do ambiente de dev.
# Os testes de integracao sobem PostgreSQL via testcontainers: precisam de
# Docker (no colima o tests/conftest.py ajusta DOCKER_HOST sozinho).

PY := uv run
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: install lock-check lint format typecheck security lint-arch test diff-cover mutacao \
	check smoke manifests print-trivy-version kind-deploy compose-up compose-down compose-logs migrate seed run

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

# Cobertura do codigo novo (ADR-041): as linhas e os ramos que a branch muda
# em relacao a origin/main, medidos no coverage.xml do `make test`, nao podem
# ficar abaixo de 90%. O gate de 95% do .coveragerc mede o total; este impede
# que uma mudanca sem teste passe so porque o total absorveu o codigo novo.
# Roda depois do `make test` e deixa o relatorio em reports/diff-cover.md (o
# CI o copia para o summary do job test). COMPARAR_COM troca a base. Com o
# coverage.xml mais velho que o codigo o diff-cover so veria "No lines with
# coverage information" e passaria em branco, entao o alvo recusa esse caso.
# --include-untracked mede tambem o modulo novo que ainda nao foi para o git.
DIFF_COVER_VERSION := 10.6.0
DIFF_COVER_MINIMO ?= 90
COMPARAR_COM ?= origin/main

diff-cover:
	@mkdir -p reports
	@test -z "$$(find src -name '*.py' -newer coverage.xml 2>/dev/null)" \
		|| { echo "diff-cover: coverage.xml is older than src/, run make test first" >&2; exit 1; }
	uvx diff-cover==$(DIFF_COVER_VERSION) coverage.xml --branch-coverage --include-untracked \
		--compare-branch=$(COMPARAR_COM) --fail-under=$(DIFF_COVER_MINIMO) \
		--format markdown:reports/diff-cover.md

# Mutacao nos modulos criticos (status do diagnostico, da execucao e da reserva,
# saga, idempotencia, consumidor, relay, placa do veiculo e JWT), sem gate e sem
# pontuacao minima. O mutmut nao roda aqui (o pacote se chama `src`, e ele exige
# o pacote fora de `src.`), entao sao mutantes escritos a mao em
# scripts/mutantes.toml, aplicados um por vez numa copia do codigo (mutants/); o
# relatorio sai em reports/mutacao.md. Para quando passa de MUTACAO_PRAZO
# segundos (15 min); os testes de integracao sobem um PostgreSQL (e os de
# mensageria um RabbitMQ) a cada mutante, a menos que TEST_DATABASE_URL aponte
# para um banco *_test no ar (o RabbitMQ sobe sempre). MUTANTES="E01 C03" roda so esses.
MUTACAO_PRAZO ?= 900
MUTANTES ?=

mutacao:
	$(PY) python scripts/mutacao_manual.py --prazo $(MUTACAO_PRAZO) $(if $(MUTANTES),--so $(MUTANTES))

check: lock-check lint lint-arch typecheck security test diff-cover
	@echo "Todos os gates passaram"

# Smoke da imagem pelo entrypoint real (migracao, seed), o job build do CI. Sobe
# a stack e confere:
# - a readiness da API (banco), o 401 de rota autenticada sem token, a imagem
#   rodando como 1001:1001 e a resposta sem o header `server`;
# - as linhas de boot do uvicorn em JSON, sem access log dele (so o
#   `http_request` estruturado do middleware) e o boot sem o aviso da telemetria
#   automatica do FastAPI (o compose leva o endpoint OTLP no ambiente da API,
#   como o cluster);
# - relay e consumidor saudaveis (broker e banco conectados, heartbeat em dia),
#   o relay conectado ao broker e o consumidor assinando a execucao.comandos;
# - os contratos de mensageria na imagem, e a topologia do RabbitMQ (com a senha
#   do admin de demonstracao) fora dela;
# - o /metrics de cada processo, logo depois do boot e sem trafego, com as
#   familias e as series de rotulo fechado dele (scripts/smoke_metricas.py);
# - a imagem, com ENVIRONMENT=production, recusando a senha de demonstracao do
#   compose (o compose sobe com development).
# Derruba tudo com os volumes, inclusive em falha (depois de mostrar os logs).
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
SMOKE_GUARDA := from src.compartilhado.infraestrutura.ambiente import url_de_conexao; url_de_conexao('DATABASE_URL')
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
	&& { ! printf '%s\n' "$$logs" | grep -q 'automatic telemetry' \
		|| { echo "smoke: o FastAPI avisou que a telemetria automatica falhou (telemetry auto_configure)" >&2; false; }; } \
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
	&& { $(SMOKE_COMPOSE) exec -T api python - api http://127.0.0.1:8000/metrics \
		< scripts/smoke_metricas.py \
		|| { echo "smoke: o /metrics da API nao traz as familias e series dela" >&2; false; }; } \
	&& { $(SMOKE_COMPOSE) exec -T relay python - relay http://127.0.0.1:9100/metrics \
		< scripts/smoke_metricas.py \
		|| { echo "smoke: o /metrics do relay nao traz as familias e series dele" >&2; false; }; } \
	&& { $(SMOKE_COMPOSE) exec -T consumidor python - consumidor http://127.0.0.1:9100/metrics \
		< scripts/smoke_metricas.py \
		|| { echo "smoke: o /metrics do consumidor nao traz as familias e series dele" >&2; false; }; } \
	&& { $(SMOKE_COMPOSE) exec -T -e ENVIRONMENT=production api python -c "$(SMOKE_GUARDA)" 2>&1 \
		| grep -q 'DATABASE_URL usa a senha de demonstracao' \
		|| { echo "smoke: com ENVIRONMENT=production a imagem aceita a senha de demonstracao" >&2; false; }; } \
	&& echo "smoke ok: readiness 200, 401 sem token, usuario 1001:1001, sem header server, boot em JSON, sem access log do uvicorn, sem aviso de telemetria, contratos na imagem e topologia fora, relay e consumidor prontos, familias e series dos tres processos no /metrics, senha de demonstracao recusada em producao" \
	|| status=$$?; \
	if [ $$status -ne 0 ]; then $(SMOKE_COMPOSE) logs --no-color --tail=200; fi; \
	$(SMOKE_COMPOSE) down -v; \
	exit $$status

# Manifestos do Kubernetes (k8s/), como no platform: os tres overlays pelo
# kubectl kustomize, no kubeconform contra os schemas do Kubernetes do no do
# kind (Secret reprova: senha nao entra nos manifests) e no trivy config, sem
# achado HIGH ou CRITICAL. O que o trivy ignora, com o motivo, esta em
# k8s/trivy-ignore.rego: a excecao vale para o ConfigMap do papeis.sql inteiro, e
# confere-senhas-do-script.sh exige que todo PASSWORD do script venha de uma
# variavel do psql lida por \getenv. Ferramentas pelas imagens pinadas (as do
# platform).
KUBERNETES_VERSION := 1.35.0
KUBECONFORM := docker run --rm -i ghcr.io/yannh/kubeconform:v0.8.0 -strict -summary \
	-output text -kubernetes-version $(KUBERNETES_VERSION) -reject Secret -
# Fonte unica da versao do trivy: o security.yml instala a de print-trivy-version.
TRIVY_VERSION := 0.72.0
TRIVY_CONFIG := docker run --rm -i -v "$(CURDIR)/k8s/trivy-ignore.rego:/trivy-ignore.rego:ro" \
	--entrypoint sh aquasec/trivy:$(TRIVY_VERSION) -c 'cat > /tmp/manifestos.yaml && trivy config \
	--quiet --severity HIGH,CRITICAL --exit-code 1 --ignore-policy /trivy-ignore.rego \
	/tmp/manifestos.yaml'

manifests:
	@scripts/ci/confere-senhas-do-script.sh
	@set -e; for overlay in kind kind-ci k3s; do \
		echo ">> k8s/overlays/$$overlay: kubeconform e trivy config"; \
		manifestos="$$(kubectl kustomize "k8s/overlays/$$overlay")"; \
		printf '%s\n' "$$manifestos" | $(KUBECONFORM); \
		printf '%s\n' "$$manifestos" | $(TRIVY_CONFIG); \
	done

print-trivy-version:
	@echo $(TRIVY_VERSION)

# Implanta este checkout no kind da plataforma (make kind-up deploy no
# platform, clonado ao lado deste) pelo scripts/ci/implantar-servicos.sh dele, o
# mesmo do CD: constroi a imagem com o commit como tag, carrega no kind, aplica
# o overlay com ela e espera o banco, o Job de migracao e os Deployments.
PLATFORM_DIR ?= ../postech-sw-arch-p4-platform
KIND_OVERLAY ?= kind

kind-deploy:
	$(PLATFORM_DIR)/scripts/ci/implantar-servicos.sh --overlay $(KIND_OVERLAY) \
		execution-service=$(CURDIR)

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
