# PytStop fase 4: Execution Service

Serviço de execução e produção da oficina: fila de diagnóstico, fila de execução, apontamentos do mecânico e estoque físico de peças (reserva, liberação e baixa). Banco próprio: PostgreSQL 16 (`execucao`). Participa da saga de atendimento orquestrada pelo OS Service.

Parte da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT): o PytStop, sistema de gestão de oficina mecânica das fases anteriores, refatorado em microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

Este repositório traz o domínio, os casos de uso, a API REST e os dois processos de mensageria do serviço: o relay da outbox para o RabbitMQ e o consumidor dos comandos da saga ([Mensageria](#mensageria)). A mesma imagem roda os três processos, com comandos diferentes.

Arquitetura da fase 4: [RFC-004 e ADRs 034 a 043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura) no repositório `platform` (divisão dos serviços, saga, catálogo de mensagens, rotas, dados e segurança).

## Arquitetura

DDD + arquitetura em camadas (`dominio` ← `aplicacao` ← `infraestrutura`/`interfaces`), com contratos verificados pelo `import-linter`. Três contextos, sem import entre os núcleos (a conversa passa por ports):

| Contexto | Agregados | Responsabilidade |
|---|---|---|
| `estoque` | `ItemEstoque` (SKU como VO), `Reserva` | Saldo físico por SKU; reserva tudo-ou-nada por ordem, liberação e baixa |
| `diagnostico` | `Diagnostico` (identidade = `ordem_id`) | Fila do mecânico; itens (serviços e peças) validados no Billing e no estoque local |
| `execucao` | `Execucao` (identidade = `ordem_id`) | Fila de execução por prioridade; início (pivot da saga) e finalização com baixa do estoque |

- **Saldo de peça:** `quantidade_disponivel` é o físico (inclui o reservado); `quantidade_reservada` está comprometida com reservas ativas; `quantidade_livre` (disponível menos reservada) é o que ainda pode ser reservado. No evento `ReservaDePecasFalhou`, `faltantes[].disponivel` é esse saldo livre (nome do contrato da saga). O invariante `0 <= reservada <= disponivel` vale no agregado e num `CHECK` do banco.
- **Reserva concorrente:** o repositório trava as linhas com `SELECT ... FOR UPDATE` em ordem de SKU (sem deadlock entre reservas) e relê o valor comitado; com qualquer peça em falta nada é separado, a recusa fica registrada (status `RECUSADA`) e a resposta lista `{sku, solicitado, disponivel}`.
- **Outbox transacional (padrão do p3):** cada caso de uso grava o evento do catálogo da saga na tabela `outbox`, na mesma transação do estado, já como o envelope do contrato (validado contra o JSON Schema na gravação), com o destino (`pytstop.eventos`, `evento.execucao.<tipo>`) e o `traceparent` de quem gravou, e emite `NOTIFY outbox_novo` no commit. O relay a publica no RabbitMQ ([Mensageria](#mensageria)).
- **Única chamada síncrona entre serviços:** conclusão do diagnóstico → Billing `POST /api/v1/precos/validacao`, com timeout de 2 s, 2 retries com jitter só em erro transitório (timeout, rede, protocolo, 502/503/504) e circuit breaker (5 falhas abrem por 30 s; depois, uma chamada de prova). O token do mecânico é repassado no `Authorization`. Circuito aberto, 5xx ou timeout depois dos retries: 503 (com `Retry-After` quando o circuito está aberto); 4xx do Billing ou resposta fora do contrato: 502 `RESPOSTA_INVALIDA_DA_DEPENDENCIA`, sem retry. Códigos e SKUs têm no máximo 50 caracteres, o limite do Billing, dono dos códigos.
- **Autenticação:** JWT RS256 emitido pelo OS Service, validado pela chave pública do JWKS (`JWKS_URL`, timeout de 2 s, cópia fresca por 10 min; com o OS fora, a última cópia boa vale por até 1 h e 3 falhas seguidas abrem um circuit breaker por 30 s), conferindo assinatura, `iss=pytstop-os-service`, `aud=pytstop`, `exp`, `sub` e `type=access`, com 10 s de tolerância de relógio. Toda falha de credencial responde o mesmo 401; papel válido sem permissão, 403; JWKS indisponível e sem cópia, 503 com `Retry-After`. Nenhum segredo compartilhado entre serviços.

Proveniência: `src/compartilhado` e o contexto `estoque` partem do [p3 em `08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1) (base de entidades, UoW, outbox, logging com mascaramento de PII, handlers de erro, padrões de teste, Dockerfile), enxugados ao que este serviço usa.

## Participação na saga

| Comando (OS → Execução) | Caso de uso | Resposta (evento) | Repetição do comando |
|---|---|---|---|
| `SolicitarDiagnostico` | `RegistrarSolicitacaoDeDiagnostico` | — (o próximo fato é `DiagnosticoIniciado`) | devolve o diagnóstico existente; concluído ou descartado, é descartado |
| `DescartarDiagnostico` | `DescartarDiagnostico` | `DiagnosticoDescartado` | reemite a resposta |
| `ReservarPecas` | `ReservarPecas` | `PecasReservadas` ou `ReservaDePecasFalhou{faltantes}` | reserva ativa ou recusada: reemite a mesma resposta, sem decidir de novo; liberada ou consumida: descartado |
| `LiberarReserva` | `LiberarReserva` | `ReservaLiberada`; reserva já consumida (execução finalizada) não volta e o comando é ignorado | reemite a resposta, sem devolver de novo |
| `AgendarExecucao` | `AgendarExecucao` | `ExecucaoAgendada{posicao_na_fila}`; só entra na fila a ordem com reserva `ATIVA` (RN-027) | na fila: reemite com a posição atual; iniciada, finalizada ou cancelada: descartado |
| `CancelarExecucao` | `CancelarExecucao` | `ExecucaoCancelada`; depois de iniciada (pivot), o comando é ignorado e o `ExecucaoIniciada` já publicado conta a história | reemite a resposta |
| `AnonimizarVeiculo` (LGPD, fora da saga) | `AnonimizarVeiculo` | nenhuma: a placa dos retratos do veículo (diagnóstico e cópia na execução) vira `ANONIMIZADO:{veiculo_id}` | não muda o que já foi anonimizado |

Regra de repetição: enquanto o desfecho vale, o comando repetido republica a resposta registrada; depois de compensado ou superado, é descartado com log, sem efeito e sem resposta. **Lápide:** a compensação que chega antes do comando original (passo em voo, RFC-004 §4.5) grava o agregado já no estado final (reserva `LIBERADA` sem peças, execução `CANCELADA`, diagnóstico `DESCARTADO` sem retrato) e responde; o original, quando chegar, encontra a lápide pela chave `ordem_id` e é descartado.

Ações do mecânico pela API: `DiagnosticoIniciado`, `DiagnosticoConcluido`, `ExecucaoIniciada` (pivot: daqui em diante a OS não cancela, por isso o início exige a reserva de peças ativa) e `ExecucaoFinalizada{pecas_consumidas}` (baixa do estoque na mesma transação). Só o mecânico que iniciou conclui o diagnóstico ou finaliza a execução; o admin pode fazê-lo em nome dele, com log de auditoria (quem, ação e ordem), como nas escritas do admin no estoque. Repetir a ação devolve o estado atual sem novo evento. O `motivo` dos comandos de compensação não é registrado aqui: o histórico da saga fica no OS Service.

## Mensageria

Comandos e eventos da saga pelo RabbitMQ ([ADR-036](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/036-mensageria-rabbitmq.md) e [RFC-004, seção 5](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md#5-mensageria)), com o usuário `execucao` do broker. A topologia (exchanges, filas, policies e permissões) é a do `platform`; o serviço só confere, por declaração passiva, a fila que lê e os exchanges em que escreve.

| Sentido | Onde | Mensagens |
|---|---|---|
| Consome | fila `execucao.comandos` (`pytstop.comandos`, `comando.execucao.#`) | `SolicitarDiagnostico`, `DescartarDiagnostico`, `ReservarPecas`, `LiberarReserva`, `AgendarExecucao`, `CancelarExecucao`, `AnonimizarVeiculo` (todas do OS Service) |
| Publica | exchange `pytstop.eventos`, routing key `evento.execucao.<tipo em snake_case>` | `DiagnosticoIniciado`, `DiagnosticoConcluido`, `DiagnosticoDescartado`, `PecasReservadas`, `ReservaDePecasFalhou`, `ReservaLiberada`, `ExecucaoAgendada`, `ExecucaoCancelada`, `ExecucaoIniciada`, `ExecucaoFinalizada` |

Envelope: `id`, `tipo`, `versao`, `origem` (`execution-service`), `correlation_id` (a ordem; no `AnonimizarVeiculo`, o veículo), `causation_id`, `ocorrido_em` (UTC) e `dados`; propriedades AMQP `message_id`, `correlation_id`, `type`, `user_id`, `delivery_mode=2` e os headers `traceparent`/`tracestate`. O `causation_id` é como o orquestrador casa cada evento: a resposta a um comando leva o `id` dele (inclusive a resposta republicada para um reenvio com `id` novo); o fato que o mecânico gera pela API leva o `id` do comando que abriu o fluxo, guardado com o diagnóstico (`SolicitarDiagnostico`) e com a execução (`AgendarExecucao`).

- **Relay** (`python -m src.relay`): acorda pelo `NOTIFY` da outbox ou pelo poll de 5 s, reivindica até 10 linhas com `FOR UPDATE SKIP LOCKED` e lease de 60 s, sem passar à frente de mensagem pendente da mesma ordem, e entrega cada linha na própria transação, que volta a travar a linha (duas réplicas nunca publicam a mesma). Publica com publisher confirms e `mandatory`, como filho do contexto de trace gravado na linha, e só então marca `entregue`. Mensagem sem rota, recusada (nack) ou barrada pelo broker conta tentativa, com os atrasos do relay do p3 (1, 4, 16 e 64 s), e a quinta falha vira `dead` (`ultimo_erro` só com a classe e o código do broker). Queda do broker não conta: sem conexão o relay não reivindica linhas, devolve o lease das que tinha em mãos e reconecta com backoff de até 30 s. Linhas entregues há mais de 7 dias são apagadas pelo próprio relay.
- **Consumidor** (`python -m src.consumidor`): para cada mensagem abre um span filho da publicação, confere o envelope e o `dados` contra o contrato e a origem pelo `user_id` (o produtor do tipo no AsyncAPI; a cópia de retry vem do próprio `execucao`), e roda o caso de uso com o `id` gravado em `mensagens_processadas` na mesma transação do efeito: `id` repetido recebe ack sem efeito. Comando que não corresponde ao estado (lápide, original atrasado, compensação depois do pivot) é ignorado com ack, nunca vai para a DLQ.
- **Retry e DLQ**: erro transitório (banco, rede, dependência fora) publica uma cópia, sem `expiration`, na fila de retry da tentativa (`execucao.comandos.retry.1s`, `.5s`, `.15s`, `.60s` e `.300s`, cujo TTL a devolve à fila), com `x-tentativa` incrementado e confirmação do broker antes do ack da original. A falha depois da quinta cópia, o erro permanente (contrato, tipo, versão, origem, dado recusado pelo domínio) e o erro não classificado vão direto para `execucao.comandos.dlq` (`basic_reject` sem requeue). Linhas de `mensagens_processadas` com mais de 30 dias são apagadas pelo consumidor.
- **Contratos** ([`contratos/`](contratos)): AsyncAPI, envelope, schemas e exemplos das mensagens que o serviço consome e produz, e a topologia do RabbitMQ (definitions, permissões, init de usuários), copiados do `platform` no commit registrado em [`contratos/ORIGEM`](contratos/ORIGEM). O CI baixa cada arquivo desse commit e compara byte a byte; o leitor é tolerante a campo novo e recusa `versao` desconhecida.
- **Trace**: o contexto W3C segue pela outbox e pelos headers AMQP sempre; a exportação OTLP para o Jaeger liga com `OTEL_ENABLED=true` ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md)). Os logs JSON levam `trace_id`, `span_id` e `correlation_id`, inclusive os do caso de uso chamado pelo consumidor.
- **Métricas** (porta `METRICS_PORT`, padrão 9100, em cada processo): `pytstop_mensagens_publicadas_total{tipo}` e `outbox_pendentes`/`outbox_dead` no relay; `pytstop_mensagens_consumidas_total{tipo,resultado}` (`processada`, `duplicada`, `ignorada`, `retry`, `dlq`) no consumidor.
- **Prontidão**: cada processo toca um heartbeat em `/tmp/<processo>-heartbeat` a cada volta do laço (liveness; dependência fora não reinicia o pod) e mantém `/tmp/<processo>-pronto` enquanto está conectado (readiness). SIGTERM termina a mensagem em curso e fecha as conexões.

Para rodar fora do compose, com o RabbitMQ e o PostgreSQL do `make compose-up` no ar:

```bash
set -a; . ./.env; set +a          # DATABASE_URL e RABBITMQ_URL de demonstracao
uv run python -m src.relay        # publica a outbox
uv run python -m src.consumidor   # consome execucao.comandos
```

## API

Swagger em `/docs`. Erros no envelope do p3, `{"erro": {"codigo", "mensagem", "id_requisicao"}}`; a exceção, também herdada do p3, é o 422 de validação de schema do FastAPI, que responde `{"detail": [{"type", "loc", "msg"}], "id_requisicao"}` (sem ecoar o valor recebido).

| Método e rota | Papéis | O que faz |
|---|---|---|
| `GET /api/v1/diagnosticos?status=` | mecânico, admin | Fila de diagnósticos por ordem de chegada |
| `POST /api/v1/diagnosticos/{ordem_id}/inicio` | mecânico, admin | Assume o diagnóstico |
| `POST /api/v1/diagnosticos/{ordem_id}/conclusao` | mecânico, admin | Registra `itens` (`servico`/`peca`, `codigo`, `quantidade`) e `observacoes` |
| `GET /api/v1/fila` | mecânico, atendente, admin | Fila de execução com a posição de cada ordem |
| `POST /api/v1/execucoes/{ordem_id}/inicio` | mecânico, admin | Inicia o reparo |
| `POST /api/v1/execucoes/{ordem_id}/finalizacao` | mecânico, admin | Finaliza e baixa as peças reservadas |
| `GET /api/v1/estoque`, `GET /api/v1/estoque/{sku}` | mecânico, atendente, admin | Consulta o estoque |
| `POST /api/v1/estoque`, `PUT /api/v1/estoque/{sku}`, `DELETE /api/v1/estoque/{sku}` | admin | Cadastro, nome/situação e desativação |
| `PATCH /api/v1/estoque/{sku}/quantidade` | admin | Ajuste do saldo físico (nunca abaixo do reservado) |
| `GET /api/v1/saude` | — | Liveness: processo de pé, sem tocar dependências (HEALTHCHECK da imagem) |
| `GET /api/v1/saude/pronto` | — | Readiness: 200 só com o banco respondendo `SELECT 1` em até 2 s, senão 503 |
| `GET /metrics` | — | Métricas Prometheus: `http_request_duration_seconds{method,rota,status}`, `pytstop_circuit_breaker_aberto{dependencia}` (1 aberto, 0,5 em meia-abertura, 0 fechado; `billing` e `jwks`) e `pytstop_jwks_falhas_total`; fica fora da borda |

Exemplos (com `TOKEN` emitido pelo OS Service; os códigos são os do seed do Billing):

```bash
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8003/api/v1/diagnosticos?status=AGUARDANDO"

curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"itens": [{"tipo": "servico", "codigo": "SRV-FREIOS", "quantidade": 1},
                 {"tipo": "peca", "codigo": "PEC-PASTILHA-FREIO", "quantidade": 1}],
       "observacoes": "pastilhas no limite"}' \
  http://localhost:8003/api/v1/diagnosticos/$ORDEM_ID/conclusao

curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"quantidade_disponivel": 12}' http://localhost:8003/api/v1/estoque/PEC-VELA/quantidade
```

## Configuração

| Variável | Obrigatória | Uso |
|---|---|---|
| `DATABASE_URL` | sim | PostgreSQL do serviço |
| `JWKS_URL` | sim | JWKS do OS Service (`/.well-known/jwks.json`) |
| `BILLING_URL` | sim | Base do Billing para a validação de preços |
| `RUN_MIGRATIONS_ON_STARTUP`, `RUN_SEED_ON_STARTUP` | não | Ligados no compose; no Kubernetes a migração roda em Job. Várias réplicas migrando juntas se serializam por `pg_advisory_lock` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_S`, `DB_CONNECT_TIMEOUT_S` | não | Pool e conexão (5 + 10, 5 s, 3 s) |
| `DB_LOCK_TIMEOUT_MS`, `DB_STATEMENT_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | não | Limites do servidor (5 s, 15 s, 30 s); o de lock vale também para a migração |
| `RABBITMQ_URL` | relay e consumidor | Broker com o usuário `execucao` (no kind, `amqp://execucao:<senha>@rabbitmq.pytstop-plataforma.svc.cluster.local:5672/`) |
| `METRICS_PORT` | não | Porta do `/metrics` do relay e do consumidor (9100) |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | não | Exportação OTLP/gRPC dos spans (desligada; `http://jaeger:4317`; `execution-service`) |

O boot falha com mensagem clara se faltar uma variável obrigatória ou se `JWKS_URL`/`BILLING_URL` não forem URL http(s) com host. Valores de demonstração em `.env.example`.

## Como rodar

Requisitos: [uv](https://docs.astral.sh/uv/) 0.11 (instala o Python 3.14 do `.python-version`) e Docker (compose e os testes de integração com testcontainers).

```bash
make compose-up    # API em http://127.0.0.1:8003, relay, consumidor, PostgreSQL (127.0.0.1:5433) e RabbitMQ (127.0.0.1:5673, console em 15673)
make compose-logs  # acompanha o log da API
make check         # uv.lock em dia, ruff, ruff format, import-linter, mypy strict, bandit e testes com gate de cobertura (90%)
make compose-down  # derruba e apaga o volume
```

Fora do compose, `.env.example` traz as variáveis com os valores de demonstração (`cp .env.example .env`, depois `set -a; . ./.env; set +a; make run`).

Sem o OS Service no ar, rota autenticada responde 401 sem token e 503 com `Retry-After` com um token bem formado (JWKS indisponível): aponte `JWKS_URL` e `BILLING_URL` para serviços acessíveis ou use a stack completa do repositório `platform`.

**Seed de demonstração** (roda no boot do compose; avulso: `DATABASE_URL=... make seed`, idempotente: só cria o que falta e nunca altera saldo existente). Os SKUs são os mesmos da tabela de preços do Billing:

| SKU | Nome | Saldo |
|---|---|---|
| `PEC-OLEO-5W30` | Oleo 5W30 (litro) | 40 |
| `PEC-FILTRO-OLEO` | Filtro de oleo | 20 |
| `PEC-PASTILHA-FREIO` | Jogo de pastilhas de freio | 10 |
| `PEC-DISCO-FREIO` | Disco de freio | 6 |
| `PEC-AMORTECEDOR` | Amortecedor dianteiro | 4 |
| `PEC-VELA` | Vela de ignicao | 0 (cenário de falta de peça da demo da saga) |

## Testes

`make test` (ou `uv run pytest`) roda os unitários e os de integração: estes sobem um PostgreSQL 16 e um RabbitMQ 4.3.6 efêmeros via testcontainers (o broker com as definitions, as permissões e o init de usuários copiados do `platform`, e o TTL das filas de retry reduzido para 100 ms), aplicam as migrações do Alembic e cobrem a mensageria de ponta a ponta (o teste publica o comando como o OS Service e lê o evento em `os.eventos`, com o schema, o `causation_id` e o trace encadeado conferidos), a reentrega e o reenvio de comando, a lápide, as cinco filas de retry, a DLQ, a origem forjada, a mensagem sem rota, o broker parado, a assinatura cancelada pelo broker, a retenção, repositórios, outbox (incluindo o `NOTIFY`), API com JWT real (chave RSA gerada no teste e JWKS servido por HTTP local, inclusive pendurado), Billing simulado com `respx` e concorrência real: a disputa pela última unidade de uma peça (uma reserva vence, a outra recebe `ReservaDePecasFalhou`, sem saldo negativo), cada lock pessimista (liberação, baixa, escrita do admin, início e cancelamento da execução, início e conclusão do diagnóstico e a reserva ativa exigida para agendar e iniciar), cópias simultâneas dos comandos da saga e réplicas migrando juntas. O gate de cobertura (ramos incluídos) é de 90% no `.coveragerc`.

## Integração contínua

Todo PR para a `main` roda dois workflows, com jobs de nome estável (são os checks obrigatórios da branch protection):

| Workflow | Jobs | O que garante |
|---|---|---|
| `CI` (`.github/workflows/ci.yml`) | `lint`, `type-check`, `security`, `test`, `sonarqube`, `build` | `uv.lock` em dia, ruff + import-linter, mypy strict, bandit, testes com gate de 90% (relatório por pacote no summary e `coverage.xml`/`htmlcov` como artefato), quality gate do SonarQube Community efêmero (`.sonar/quality-gate.json`) e smoke da imagem pelo compose (`make smoke`: entrypoint real com migração e seed, imagem como `1001:1001`, readiness 200, 401 sem token, sem header `server`, sem access log do uvicorn, contratos de mensageria na imagem, relay e consumidor saudáveis e o consumidor assinado na `execucao.comandos`) |
| `Security` (`.github/workflows/security.yml`) | `pip-audit`, `gitleaks`, `trivy` | CVE nas dependências de runtime, segredos na árvore e CVE HIGH/CRITICAL com correção na imagem; roda também toda segunda-feira |

`make check` roda localmente os mesmos gates de código do job `CI`, e `make smoke` o do job `build`.

## Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash.
