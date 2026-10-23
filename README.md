# PytStop fase 4: Execution Service

[![CD](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/workflows/cd.yml/badge.svg?branch=main)](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/workflows/cd.yml)

Serviço de execução e produção da oficina PytStop. Reúne três contextos (as pastas `src/diagnostico`, `src/execucao` e `src/estoque`): Diagnóstico (a fila de diagnóstico e os apontamentos do mecânico), Execução (a fila de execução e a finalização do reparo) e Estoque (o estoque físico de peças, com reserva, liberação e baixa). É participante da saga que o OS Service orquestra ([ADR-035](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/035-saga-orquestrada.md)): recebe os comandos pela fila `execucao.comandos` do RabbitMQ e responde com eventos, gravados na outbox e publicados pelo relay. O início do reparo (`ExecucaoIniciada`) é o pivot da saga: depois dele a OS não cancela. Banco próprio: PostgreSQL 16 (`execucao`). Integrações: RabbitMQ, o Billing (a única chamada síncrona, na conclusão do diagnóstico, para validar preços), o JWKS do OS Service (para validar o JWT do mecânico) e, na observabilidade, o Jaeger, o Prometheus e o Loki do `platform`. Faz parte da entrega da fase 4 do Tech Challenge, e o [índice da entrega](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/INDICE-DA-ENTREGA.md) liga os quatro repositórios, os requisitos e as evidências.

**Sumário**: [Como rodar](#como-rodar), [Como testar](#como-testar), [Cobertura](#cobertura), [API](#api), [Mensageria](#mensageria), [Arquitetura](#arquitetura), [Observabilidade](#observabilidade), [CI/CD](#cicd), [Segurança](#segurança) e [Decisões e rastreabilidade](#decisões-e-rastreabilidade).

**Documentos em `docs/`**: [arquitetura](docs/arquitetura.md), [API](docs/api/) e [exemplos da API](docs/api/exemplos.md), [mensageria e saga](docs/mensageria.md), [observabilidade](docs/observabilidade.md), [como rodar](docs/como-rodar.md), [configuração](docs/configuracao.md), [implantação](docs/implantacao.md), [testes](docs/testes.md), [cobertura](docs/cobertura.md), [mutação](docs/mutacao.md), [CI/CD](docs/cicd.md), [dependências](docs/dependencias.md) e [segurança](docs/seguranca.md).

## Como rodar

Requisitos: [uv](https://docs.astral.sh/uv/) 0.11 ou mais novo (instala o Python 3.14 do `.python-version`) e Docker (compose e os testes de integração com testcontainers).

```bash
make compose-up
curl http://127.0.0.1:8003/api/v1/saude
curl http://127.0.0.1:8003/api/v1/saude/pronto
make compose-down
```

O `make compose-up` sobe a API em http://127.0.0.1:8003 (Swagger em `/docs`), o relay, o consumidor, o PostgreSQL (127.0.0.1:5433) e o RabbitMQ (127.0.0.1:5673, console em 15673, usuário `admin`), com migração e seed do estoque no boot. A mesma imagem roda os três processos, cada um com o seu comando. O `make compose-logs` acompanha o log da API, e o `make compose-down` derruba a stack e apaga o volume. Sem o OS Service no ar, só as sondas respondem e as rotas autenticadas dão 401 ou 503: a stack completa vem do repositório [`platform`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform), que sobe o kind com os três serviços. Fora do compose, sem o OS Service, o seed e o fluxo à mão pelo RabbitMQ: [docs/como-rodar.md](docs/como-rodar.md); variáveis de ambiente: [docs/configuracao.md](docs/configuracao.md).

No kind, com o `platform` clonado ao lado deste repositório:

```bash
make -C ../postech-sw-arch-p4-platform kind-up deploy
make kind-deploy
../postech-sw-arch-p4-platform/scripts/ci/smoke-servicos.sh execution-service
```

O `make kind-deploy` constrói a imagem deste checkout, carrega-a no kind e aplica o overlay `kind`, e o smoke confere o Job, os rollouts e a API pela borda (`https://localhost/execucao/api/v1/saude`). Objetos, sondas e smoke: [docs/implantacao.md](docs/implantacao.md).

## Como testar

```bash
make check
make smoke
```

O `make check` roda `uv.lock` em dia, ruff, ruff format, import-linter, mypy strict, bandit, os testes (unitários, de integração e de contrato) com gate de cobertura de 95% e o `diff-cover` (90% no código que a branch muda em relação à `origin/main`). O `make smoke` prova a imagem pelo entrypoint real. Os dois pedem Docker.

- **Integração**: PostgreSQL 16 e RabbitMQ 4.3.6 efêmeros por testcontainers, com as migrações do Alembic e a topologia do `platform`: mensageria de ponta a ponta, relay com duas réplicas, API com JWT real, concorrência, implantação e observabilidade.
- **Contrato**: `contratos/` é cópia do `platform`, e o teste compara cada arquivo byte a byte com o commit de `contratos/ORIGEM` (precisa de rede, marcador `rede`).
- **BDD**: não há neste repositório. O fluxo completo da saga em Gherkin é do `platform` ([`saga_atendimento.feature`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/e2e/features/saga_atendimento.feature)) e roda no E2E do `deploy-kind` do CD deste serviço.
- Os casos por área estão em [docs/testes.md](docs/testes.md).

### Mutação

Mutantes escritos à mão nos módulos críticos, sem gate de CI (`make mutacao`). Em 23/10/2026: 98 mutantes, 97 mortos e 1 equivalente, que fica de fora da conta. A lista, o resultado por área e como refazer: [docs/mutacao.md](docs/mutacao.md).

## Cobertura

Gate de 95% no total, com linhas e ramos somados (`.coveragerc`), e de 90% no código novo de cada pull request (`make diff-cover`), como pede o [ADR-041](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/041-estrategia-de-testes-e-qualidade.md) (RNF-050). A tabela é gerada do `coverage.xml` de um run do CI por `scripts/cobertura_resumo.py`; como ela é conferida e como refazê-la: [docs/cobertura.md](docs/cobertura.md).

<!-- cobertura:inicio -->
### Cobertura de testes: 100.0% no gate (linhas e ramos)

100.0% de linhas e 100.0% de ramos.

| Pacote | Linhas | Cobertas | Cobertura |
|---|---:|---:|---:|
| `src` | 318 | 318 | 100.0% |
| `src/compartilhado/aplicacao` | 59 | 59 | 100.0% |
| `src/compartilhado/dominio` | 144 | 144 | 100.0% |
| `src/compartilhado/infraestrutura` | 1329 | 1329 | 100.0% |
| `src/compartilhado/interfaces` | 236 | 236 | 100.0% |
| `src/diagnostico/aplicacao` | 127 | 127 | 100.0% |
| `src/diagnostico/dominio` | 177 | 177 | 100.0% |
| `src/diagnostico/infraestrutura` | 127 | 127 | 100.0% |
| `src/diagnostico/interfaces` | 92 | 92 | 100.0% |
| `src/estoque/aplicacao` | 155 | 155 | 100.0% |
| `src/estoque/dominio` | 226 | 226 | 100.0% |
| `src/estoque/infraestrutura` | 105 | 105 | 100.0% |
| `src/estoque/interfaces` | 113 | 113 | 100.0% |
| `src/execucao/aplicacao` | 173 | 173 | 100.0% |
| `src/execucao/dominio` | 122 | 122 | 100.0% |
| `src/execucao/infraestrutura` | 99 | 99 | 100.0% |
| `src/execucao/interfaces` | 84 | 84 | 100.0% |
| **total** | 3686 | 3686 | 100.0% |

Tabela gerada por `scripts/cobertura_resumo.py` a partir do `coverage.xml` do [run 39937419103 do CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/runs/39937419103).
<!-- cobertura:fim -->

**Oscilação**: a cobertura não oscila (100,0% de linhas e de ramos nos runs 39536414057 e 39937419103); o que já oscilou foi um teste do relay, corrigido no [#23](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/pull/23). Detalhe em [docs/cobertura.md](docs/cobertura.md#oscilação).

## API

Swagger em `/docs` (pela borda, `/execucao/docs`). O OpenAPI exportado ([`docs/api/openapi.json`](docs/api/openapi.json)) e a coleção Postman v2.1 ([`docs/api/execucao.postman_collection.json`](docs/api/execucao.postman_collection.json): uma requisição por rota, com corpo e resposta de exemplo e as variáveis `baseUrl`, `token`, `ordem_id` e `sku`) saem do próprio app pelo `make docs-api`, e o `make test` reprova se ficarem atrás do código (RNF-052). A coleção do fluxo completo, que roda com newman, é do [`platform`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/api/fluxo-completo.postman_collection.json). Erros no envelope do p3, `{"erro": {"codigo", "mensagem", "id_requisicao"}}`; a exceção, também herdada do p3, é o 422 de validação de schema do FastAPI, que responde `{"detail": [{"type", "loc", "msg"}], "id_requisicao"}` (sem ecoar o valor recebido).

Rotas principais, com o prefixo `/api/v1`:

- `GET /diagnosticos`, `POST /diagnosticos/{ordem_id}/inicio` e `/conclusao`: a fila do mecânico e o diagnóstico (mecânico e admin).
- `GET /fila`, `POST /execucoes/{ordem_id}/inicio` e `/finalizacao`: a fila de execução, o início do reparo (o pivot da saga) e a finalização, que baixa as peças reservadas.
- `GET /estoque` e `GET /estoque/{sku}`: consulta (mecânico, atendente e admin). `POST`, `PUT`, `PATCH /estoque/{sku}/quantidade` e `DELETE`: só o admin.
- `GET /saude` e `GET /saude/pronto`: liveness e readiness, sem token. O `/metrics` fica fora do OpenAPI e da borda.

A tabela completa com os papéis e um `curl` por rota: [docs/api/exemplos.md](docs/api/exemplos.md).

## Mensageria

Comandos e eventos da saga pelo RabbitMQ, com o usuário `execucao` do broker ([ADR-036](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/036-mensageria-rabbitmq.md) e [RFC-004, seção 5](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md#5-mensageria)). A topologia é a do `platform`. Os contratos (AsyncAPI, envelope, schemas e exemplos) são a cópia em [`contratos/`](contratos), presa ao commit de [`contratos/ORIGEM`](contratos/ORIGEM).

| Sentido | Onde | Mensagens |
|---|---|---|
| Consome | fila `execucao.comandos` (`pytstop.comandos`, `comando.execucao.#`) | `SolicitarDiagnostico`, `DescartarDiagnostico`, `ReservarPecas`, `LiberarReserva`, `AgendarExecucao`, `CancelarExecucao`, `AnonimizarVeiculo` (todas do OS Service) |
| Publica | exchange `pytstop.eventos`, routing key `evento.execucao.<tipo em snake_case>` | `DiagnosticoIniciado`, `DiagnosticoConcluido`, `DiagnosticoDescartado`, `PecasReservadas`, `ReservaDePecasFalhou`, `ReservaLiberada`, `ExecucaoAgendada`, `ExecucaoCancelada`, `ExecucaoIniciada`, `ExecucaoFinalizada` |

- **Outbox**: o caso de uso grava o evento na tabela `outbox`, na mesma transação do estado, e o relay (`python -m src.relay`) o publica com confirmação do broker.
- **Idempotência**: o consumidor (`python -m src.consumidor`) grava o `id` da mensagem em `mensagens_processadas` na mesma transação do efeito. `id` repetido recebe ack sem efeito, e comando que não corresponde ao estado (lápide, original atrasado) é ignorado com ack.
- **Retry e DLQ**: erro transitório vai para as filas de retry com atraso (`execucao.comandos.retry.1s` a `.300s`); erro permanente, erro não classificado e a quinta falha vão para `execucao.comandos.dlq`.

Cada comando, o caso de uso, a resposta e a regra de repetição: [docs/mensageria.md](docs/mensageria.md#participação-na-saga). Envelope, lease do relay, entrada hostil, prontidão e métricas: [docs/mensageria.md](docs/mensageria.md#mensageria).

## Arquitetura

DDD + arquitetura em camadas (`dominio` ← `aplicacao` ← `infraestrutura`/`interfaces`), com contratos verificados pelo `import-linter` no `make check`: a camada de cima importa as de baixo e nunca o contrário, domínio e aplicação não importam FastAPI, Pydantic, SQLAlchemy, pika, httpx, PyJWT, jsonschema nem OpenTelemetry, e `compartilhado` não conhece contexto algum. Três contextos, sem import entre os núcleos (a conversa passa por ports):

| Contexto | Agregados | Responsabilidade |
|---|---|---|
| `estoque` | `ItemEstoque` (SKU como VO), `Reserva` | Saldo físico por SKU; reserva tudo-ou-nada por ordem, liberação e baixa |
| `diagnostico` | `Diagnostico` (identidade = `ordem_id`) | Fila do mecânico; itens (serviços e peças) validados no Billing e no estoque local |
| `execucao` | `Execucao` (identidade = `ordem_id`) | Fila de execução por prioridade; início (pivot da saga) e finalização com baixa do estoque |

Módulos, portas entre os contextos, o modelo de dados completo (ER), os fluxos passo a passo em diagramas, a mensageria, a segurança e as decisões estão em [docs/arquitetura.md](docs/arquitetura.md) (RNF-051). As regras do saldo de peça, da reserva concorrente, da outbox e da chamada ao Billing estão na [seção 9](docs/arquitetura.md#9-regras-do-desenho). A arquitetura global da fase 4 está na [RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) e nos [ADRs 034 a 043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura) do `platform`.

## Observabilidade

Métricas Prometheus no `/metrics` de cada processo (a API na porta 8000; o relay e o consumidor na `METRICS_PORT`, 9100), traces pelo protocolo do OpenTelemetry (OTLP) para o Jaeger e logs JSON com `trace_id`, `span_id` e `correlation_id` ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md) e [RFC-004, seção 9](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md#9-observabilidade)). O `/metrics` fica fora da borda: o Kong responde 404 em `/execucao/metrics`, e o Prometheus raspa cada pod. O `platform` lê as séries nos [dashboards](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/observabilidade/dashboards) e nos alertas do Grafana, os traces no Jaeger e os logs no Loki (pelo Promtail), e traz o [catálogo de métricas](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#catálogo-de-métricas) dos três serviços.

Com o kind no ar, `make -C ../postech-sw-arch-p4-platform port-forward` abre o Jaeger (http://localhost:16686), o Grafana (http://localhost:3000) e o Prometheus (http://localhost:9090), e o `make -C ../postech-sw-arch-p4-platform e2e` gera os traces. As séries, os spans, os logs, o que não é exportado e por quê: [docs/observabilidade.md](docs/observabilidade.md).

## CI/CD

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash. Todo PR para a `main` roda dois workflows, com jobs de nome estável (os nove checks obrigatórios da branch protection): `CI` (`lint`, `type-check`, `security`, `test`, `sonarqube`, `build`) e `Security` (`pip-audit`, `gitleaks`, `trivy`; roda também toda segunda-feira). O [`cd.yml`](.github/workflows/cd.yml) é um caller fino do [workflow reutilizável do `platform`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/.github/workflows/cd-servico.yml): publica a imagem no GHCR, com SBOM e proveniência, e implanta num kind efêmero (Kubernetes dentro de contêineres do runner), com smoke e E2E da saga. O deploy remoto segue desligado. O Dependabot abre PRs toda segunda, em três blocos (uv, Actions e Docker). Jobs, gates e pin do reutilizável: [docs/cicd.md](docs/cicd.md); regras e exceções do Dependabot: [docs/dependencias.md](docs/dependencias.md).

## Segurança

- **Autenticação**: JWT RS256 emitido pelo OS Service e validado localmente pela chave pública do JWKS (`iss`, `aud`, `exp`, `sub` e `type=access`), sem segredo compartilhado entre serviços. Toda falha de credencial responde o mesmo 401, papel sem permissão responde 403 e JWKS indisponível e sem cópia, 503 ([ADR-039](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/039-autenticacao-entre-servicos.md)).
- **Papéis**: mecânico, atendente e admin, conferidos por rota. Só o mecânico que iniciou conclui o diagnóstico ou finaliza a execução; o admin age em nome dele, com log de auditoria.
- **LGPD**: o serviço guarda o retrato do veículo e textos livres, e não os dados do cliente. O `AnonimizarVeiculo` elimina a placa e os textos livres. Os logs saem mascarados, os spans sem o caminho cru nem o texto de erro, e as métricas só com códigos do catálogo.
- **Banco e cluster**: papéis separados no PostgreSQL (o dono só no Job de migração, a aplicação só com DML), Pod Security `restricted`, NetworkPolicy com entrada negada por padrão e nenhum Secret no repositório.
- **Cadeia de entrega**: `pip-audit`, `gitleaks` e `trivy` em todo PR ([CI/CD](#cicd)).

O detalhe de cada item está em [docs/seguranca.md](docs/seguranca.md).

### Troca de senha do banco

A senha de cada papel do PostgreSQL (`execucao_app`, `execucao_exporter`, `execucao` e `postgres`) troca por um procedimento de `kubectl`, com janela até o restart e sem deixar a senha no log do servidor: [docs/seguranca.md](docs/seguranca.md#troca-de-senha-do-banco). A mecânica geral é a de [Troca de senha do banco](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#troca-de-senha-do-banco), no README do `platform`.

## Decisões e rastreabilidade

Este repositório é um dos quatro da entrega da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT). Nessa fase o PytStop, sistema de gestão de oficina mecânica das fases anteriores, virou microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes. O [índice da entrega](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/INDICE-DA-ENTREGA.md) liga os quatro repositórios, os requisitos e as evidências.

A arquitetura global da fase 4 está na [RFC-004 e nos ADRs 034 a 043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura) do repositório `platform`: divisão dos serviços, saga, catálogo de mensagens, rotas, dados e segurança. As decisões deste serviço, com o arquivo de cada uma, estão na [seção 8 de docs/arquitetura.md](docs/arquitetura.md#8-decisões). Os entregáveis RNF-050 a RNF-052 vêm da [gap analysis da fase](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/requisitos/fase4/gap-analysis-fase-4.md), e a [matriz de rastreabilidade](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/entrega/matriz-de-rastreabilidade.md) do `platform` liga cada requisito aos pull requests e às evidências.

### Proveniência

`src/compartilhado` e o contexto `estoque` partem do [p3 no commit `08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1) (base de entidades, UoW, outbox, logging com mascaramento de PII, handlers de erro, padrões de teste, Dockerfile), enxugados ao que este serviço usa.

### Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |
