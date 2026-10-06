# Project Memory -- postech-sw-arch-p4-execution-service

<!-- last-consolidated: 2026-10-06 -->

Add-only log of project-specific learnings. New entries go to the top of each section. Never edit historical entries -- add a contradicting entry above instead.

Updated by AI agents at task end per `postech-ai-helper/ai/canonical/task-end-review.md`. The `last-consolidated` marker above is updated only when `/consolidate-memory` runs, not on every append.

## Recent decisions

- 2026-10-06 - Eventos do catalogo da saga sao montados pela camada de aplicacao e registrados com `uow.registrar_evento` (gravados na `outbox` no commit, mesma transacao): replies de comando sem mudanca de estado (`ReservaDePecasFalhou`, reemissao em comando repetido) e eventos com dado de fora do agregado (`posicao_na_fila`, `pecas_consumidas`) nao cabem num agregado. Agregados ficam so com estado e invariantes. `tipo` = nome da classe sem `Event`, travado por `tests/unitarios/test_catalogo_eventos.py` - PR feat/execution-service-dominio-api
- 2026-10-06 - Outbox: colunas de controle do relay do p3 (`status`, `tentativas`, `proxima_tentativa_em`, `entregue_em`, `ultimo_erro`) + envelope (`mensagem_id`, `tipo`, `correlation_id` = ordem_id, `ocorrido_em`, `dados`); ordem por saga no indice `(correlation_id, id, status)`. `causation_id`, `versao` e `origem` ficaram para o PR de mensageria (quem conhece a mensagem de origem e o consumidor)
- 2026-10-06 - Idempotencia: comando repetido (reenvio do orquestrador) reemite a MESMA resposta sem decidir de novo, inclusive a recusa: `ReservarPecas` sem estoque grava a reserva `RECUSADA` com os faltantes (sem ela, um reenvio apos reposicao reservaria pecas de ordem ja compensada). A existencia da reserva e conferida depois do `FOR UPDATE` nos itens (copia simultanea ve a decisao da outra). Acao de API repetida pelo mesmo mecanico devolve o estado atual sem evento; compensacao para ordem sem agregado responde 404 (o orquestrador so compensa passo concluido); o `motivo` das compensacoes nao e usado nem logado (texto livre pode ter PII; historico fica no OS)
- 2026-10-06 - Pivot e responsavel: `IniciarExecucao` exige reserva ATIVA (409 sem ela), senao a execucao cruzaria o pivot sem conseguir finalizar. So o mecanico que iniciou conclui/finaliza; o admin (pode tudo) age em nome dele (`pelo_admin`), sem trocar o `mecanico_id`
- 2026-10-06 - 422 so para `ValorInvalidoError` (subclasse de `ValueError` levantada pelos VOs/agregados); `ValueError` de biblioteca (ex.: `ValidationError` do Pydantic ao montar resposta) vira 500 sem ecoar o valor
- 2026-10-06 - Saldo de peca: `quantidade_disponivel` = fisico (inclui reservado), `quantidade_livre` = disponivel - reservada; `faltantes[].disponivel` = livre. SKU desconhecido ou inativo vira faltante com disponivel 0 (nao erro), para a saga seguir para a compensacao
- 2026-10-06 - Repo criado na fase 4 com branch protection na `main` desde o commit inicial (PR obrigatorio, admins incluidos, historico linear, conversas resolvidas, squash only). Motivo: a fase 3 perdeu ponto por commits diretos na main (29 no app, 11 na lambda) - spec `postech-sw-arch-p4/docs/superpowers/specs/2026-10-06-fase-4-bootstrap-design.md`

## Discovered conventions

- 2026-10-06 - SonarQube do CI (gate em `.sonar/quality-gate.json`) conta "bug" de teste: `assert X() == X()` com a mesma expressao dos dois lados (python:S5863) derrubou a confiabilidade para C. Em teste, compare variaveis (`a, b = X(), X()`) e deixe so a chamada testada dentro do `pytest.raises` (S5778). Rodar `scripts/sonar/analisar.sh` contra um `sonarqube` local antes do push pega isso

- 2026-10-06 - `uv run pytest` sozinho e o gate: roda unitarios + integracao (testcontainers Postgres 16, schema pelas migracoes do Alembic, nao `create_all`) e aplica o `fail_under = 90` do `.coveragerc` via `--cov` no `addopts`. No colima o `tests/conftest.py` exporta `DOCKER_HOST`/`TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE` sozinho
- 2026-10-06 - JWT nos testes: chave RSA gerada na sessao e JWKS servido por um `ThreadingHTTPServer` local (o `PyJWKClient` usa urllib com opener proprio, nao `urlopen` nem httpx, entao respx e monkeypatch de `urlopen` nao o alcancam). Billing simulado com `respx`

## Gotchas

- 2026-10-06 - Dependency do FastAPI com `Request` importado so sob `TYPE_CHECKING` (com `from __future__ import annotations`) vira query param obrigatorio `request` e toda rota responde 422 `loc: [query, request]`. Teste com app de brinquedo nao pega; so o teste contra o app real pegou. `Request` e `Session` das dependencies ficam em import de runtime
- 2026-10-06 - FastAPI 0.142 embrulha routers incluidos em `_IncludedRouter`: `app.routes` nao lista mais as `APIRoute` dos routers incluidos; inspecione pelo `/openapi.json`
- 2026-10-06 - PyJWT 2.13.x acumulou 27 advisories em out/2026: comecar em `pyjwt>=2.15.1` e `anyio>=4.15.1`. PyJWT 2.15 exige base64url valido na assinatura mesmo com `verify_signature=False` (JWT falso de teste precisa de segmento valido)

## Tech debt / TODO

- 2026-10-06 - MEDIUM - Contrato entre servicos a alinhar com OS e Billing: (a) envelope de erro usa `id_requisicao` (p3), o brief fala em `request_id`; (b) corpo de `POST /api/v1/precos/validacao` assumido como `{servicos: [str], pecas: [str]} -> 200 {invalidos: [str]}`; (c) token sem `type` e aceito como access, `type != access` e recusado
- 2026-10-06 - MEDIUM - Saga: `SolicitarDiagnostico` nao tem resposta tecnica no catalogo (o proximo fato, `DiagnosticoIniciado`, depende do mecanico), entao o prazo tecnico do orquestrador (120 s x 5) compensaria OS esperando mecanico. E passo com prazo esgotado pode ter efeito tardio (reserva ATIVA ou execucao na fila de ordem compensada) se a compensacao so incluir passos concluidos
- 2026-10-06 - LOW - Metricas do brief secao 9 (`outbox_pendentes`, `mensagens_*`) e tracing OTel ficam para a onda de observabilidade; hoje: `http_request_duration_seconds` e `pytstop_circuit_breaker_aberto{dependencia}`

## Review lessons

- 2026-10-06 - Idempotencia de comando precisa cobrir o desfecho negativo: recusa nao persistida deixa um reenvio decidir diferente (reserva tardia apos a compensacao). Procure "falha que nao grava nada" em todo caso de uso que responde a comando - PR feat/execution-service-dominio-api (revisao single-shot)
- 2026-10-06 - Handler global `ValueError -> 422` captura `ValidationError` do Pydantic e `JSONDecodeError` de biblioteca (ex.: corpo do JWKS): erro do servidor vira 422 ecoando dado interno. Use excecao propria do dominio - PR feat/execution-service-dominio-api
