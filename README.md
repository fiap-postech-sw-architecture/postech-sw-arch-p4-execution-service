# PytStop fase 4: Execution Service

Serviço de execução e produção da oficina: fila de diagnóstico, fila de execução, apontamentos do mecânico e estoque físico de peças (reserva, liberação e baixa). Banco próprio: PostgreSQL 16 (`execucao`). Participa da saga de atendimento orquestrada pelo OS Service.

Parte da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT): o PytStop, sistema de gestão de oficina mecânica das fases anteriores, refatorado em microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

Este repositório traz o domínio, os casos de uso, a API REST e a outbox transacional do serviço. O relay da outbox para o RabbitMQ e o consumidor dos comandos da saga ficam no repositório a partir da onda de mensageria; os casos de uso que eles chamam já estão aqui e são exercitados pelos testes.

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
- **Outbox transacional (padrão do p3):** cada caso de uso grava o evento do catálogo da saga na tabela `outbox`, na mesma transação do estado, já no formato do envelope (`mensagem_id`, `tipo`, `correlation_id = ordem_id`, `ocorrido_em`, `dados`), e emite `NOTIFY outbox_novo` no commit.
- **Única chamada síncrona entre serviços:** conclusão do diagnóstico → Billing `POST /api/v1/precos/validacao`, com timeout de 2 s, 2 retries com jitter só em erro transitório (timeout, rede, protocolo, 502/503/504) e circuit breaker (5 falhas abrem por 30 s; depois, uma chamada de prova). O token do mecânico é repassado no `Authorization`. Circuito aberto, 5xx ou timeout depois dos retries: 503 (com `Retry-After` quando o circuito está aberto); 4xx do Billing ou resposta fora do contrato: 502 `RESPOSTA_INVALIDA_DA_DEPENDENCIA`, sem retry. Códigos e SKUs têm no máximo 50 caracteres, o limite do Billing, dono dos códigos.
- **Autenticação:** JWT RS256 emitido pelo OS Service, validado pela chave pública do JWKS (`JWKS_URL`, timeout de 2 s, cópia fresca por 10 min; com o OS fora, a última cópia boa vale por até 1 h e 3 falhas seguidas abrem um circuit breaker por 30 s), conferindo assinatura, `iss=pytstop-os-service`, `aud=pytstop`, `exp`, `sub` e `type=access`, com 10 s de tolerância de relógio. Toda falha de credencial responde o mesmo 401; papel válido sem permissão, 403; JWKS indisponível e sem cópia, 503 com `Retry-After`. Nenhum segredo compartilhado entre serviços.

Proveniência: `src/compartilhado` e o contexto `estoque` partem do [p3 em `08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1) (base de entidades, UoW, outbox, logging com mascaramento de PII, handlers de erro, padrões de teste, Dockerfile), enxugados ao que este serviço usa.

## Participação na saga

| Comando (OS → Execução) | Caso de uso | Resposta (evento) | Repetição do comando |
|---|---|---|---|
| `SolicitarDiagnostico` | `RegistrarSolicitacaoDeDiagnostico` | — (o próximo fato é `DiagnosticoIniciado`) | devolve o diagnóstico existente; concluído ou descartado, é descartado |
| `DescartarDiagnostico` | `DescartarDiagnostico` | `DiagnosticoDescartado` | reemite a resposta |
| `ReservarPecas` | `ReservarPecas` | `PecasReservadas` ou `ReservaDePecasFalhou{faltantes}` | reserva ativa ou recusada: reemite a mesma resposta, sem decidir de novo; liberada ou consumida: descartado |
| `LiberarReserva` | `LiberarReserva` | `ReservaLiberada` (409 se a reserva já foi consumida) | reemite a resposta, sem devolver de novo |
| `AgendarExecucao` | `AgendarExecucao` | `ExecucaoAgendada{posicao_na_fila}`; só entra na fila a ordem com reserva `ATIVA` (RN-027) | na fila: reemite com a posição atual; iniciada, finalizada ou cancelada: descartado |
| `CancelarExecucao` | `CancelarExecucao` | `ExecucaoCancelada` (409 depois de iniciada) | reemite a resposta |

Regra de repetição: enquanto o desfecho vale, o comando repetido republica a resposta registrada; depois de compensado ou superado, é descartado com log, sem efeito e sem resposta. **Lápide:** a compensação que chega antes do comando original (passo em voo, RFC-004 §4.5) grava o agregado já no estado final (reserva `LIBERADA` sem peças, execução `CANCELADA`, diagnóstico `DESCARTADO` sem retrato) e responde; o original, quando chegar, encontra a lápide pela chave `ordem_id` e é descartado.

Ações do mecânico pela API: `DiagnosticoIniciado`, `DiagnosticoConcluido`, `ExecucaoIniciada` (pivot: daqui em diante a OS não cancela, por isso o início exige a reserva de peças ativa) e `ExecucaoFinalizada{pecas_consumidas}` (baixa do estoque na mesma transação). Só o mecânico que iniciou conclui o diagnóstico ou finaliza a execução; o admin pode fazê-lo em nome dele, com log de auditoria (quem, ação e ordem), como nas escritas do admin no estoque. Repetir a ação devolve o estado atual sem novo evento. O `motivo` dos comandos de compensação não é registrado aqui: o histórico da saga fica no OS Service.

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
| `GET /metrics` | — | Métricas Prometheus (`http_request_duration_seconds`, `pytstop_circuit_breaker_aberto`); fica fora da borda |

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

O boot falha com mensagem clara se faltar uma variável obrigatória ou se `JWKS_URL`/`BILLING_URL` não forem URL http(s) com host. Valores de demonstração em `.env.example`.

## Como rodar

Requisitos: [uv](https://docs.astral.sh/uv/) 0.11 (instala o Python 3.14 do `.python-version`) e Docker (compose e os testes de integração com testcontainers).

```bash
make compose-up    # API em http://127.0.0.1:8003 + PostgreSQL (127.0.0.1:5433), migrações e seed no boot
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

`make test` (ou `uv run pytest`) roda os unitários e os de integração: estes sobem um PostgreSQL 16 efêmero via testcontainers, aplicam as migrações do Alembic e cobrem repositórios, outbox (incluindo o `NOTIFY`), API com JWT real (chave RSA gerada no teste e JWKS servido por HTTP local, inclusive pendurado), Billing simulado com `respx` e concorrência real: a disputa pela última unidade de uma peça (uma reserva vence, a outra recebe `ReservaDePecasFalhou`, sem saldo negativo), cada lock pessimista (liberação, baixa, escrita do admin), cópias simultâneas dos comandos da saga e réplicas migrando juntas. O gate de cobertura (ramos incluídos) é de 90% no `.coveragerc`.

## Integração contínua

Todo PR para a `main` roda dois workflows, com jobs de nome estável (são os checks obrigatórios da branch protection):

| Workflow | Jobs | O que garante |
|---|---|---|
| `CI` (`.github/workflows/ci.yml`) | `lint`, `type-check`, `security`, `test`, `sonarqube`, `build` | `uv.lock` em dia, ruff + import-linter, mypy strict, bandit, testes com gate de 90% (relatório por pacote no summary e `coverage.xml`/`htmlcov` como artefato), quality gate do SonarQube Community efêmero (`.sonar/quality-gate.json`) e smoke da imagem pelo compose (`make smoke`: entrypoint real com migração e seed, usuário 1001, readiness 200 e 401 sem token) |
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
