# Configuração

Detalhe da seção [Como rodar](../README.md#como-rodar) do README: as variáveis de ambiente da API, do relay e do consumidor.

| Variável | Obrigatória | Uso |
|---|---|---|
| `DATABASE_URL` | sim | PostgreSQL do serviço (no Kubernetes, com o papel `execucao_app`; [Segredos e papéis do banco](seguranca.md#segredos-e-papéis-do-banco)) |
| `ENVIRONMENT` | não | `development` ou `test` aceitam as senhas de demonstração do compose e do `.env.example`; qualquer outro valor, inclusive a variável ausente, é produção, e a API, o relay e o consumidor não sobem com elas em `DATABASE_URL` ou `RABBITMQ_URL` |
| `ROOT_PATH` | não | Prefixo da borda (`/execucao` no Kubernetes), passado ao uvicorn como `--root-path`: o Swagger atrás do Kong busca o `openapi.json` sob ele, e as sondas e o `/metrics`, que chegam direto ao pod, seguem sem ele. Vazio no compose, que serve na raiz |
| `JWKS_URL` | sim | JWKS do OS Service (`/.well-known/jwks.json`) |
| `BILLING_URL` | sim | Base do Billing para a validação de preços |
| `RUN_MIGRATIONS_ON_STARTUP`, `RUN_SEED_ON_STARTUP` | não | Ligados no compose; no Kubernetes a migração roda em Job. Várias réplicas migrando juntas se serializam por `pg_advisory_lock` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_S`, `DB_CONNECT_TIMEOUT_S` | não | Pool e conexão (5 + 10, 5 s, 3 s) |
| `DB_LOCK_TIMEOUT_MS`, `DB_STATEMENT_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | não | Limites do servidor (5 s, 15 s, 30 s); o de lock vale também para a migração |
| `RABBITMQ_URL` | relay e consumidor | Broker com o usuário `execucao`, usada como vem, com o vhost explícito (no kind, `amqp://execucao:<senha>@rabbitmq.pytstop-plataforma.svc.cluster.local:5672/%2F`, do Secret `rabbitmq`) |
| `METRICS_PORT` | não | Porta do `/metrics` do relay e do consumidor (9100) |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | não | Exportação OTLP/gRPC dos spans da API, do relay e do consumidor (desligada; `http://jaeger:4317`; `execution-service`). Desligada, os spans existem no processo (o contexto de trace segue pela outbox e pelo AMQP e os logs levam o `trace_id`), mas não saem dele |
| `OTEL_BSP_EXPORT_TIMEOUT` | não | Valor de 2000 ms mantido no ConfigMap e no compose; o SDK 1.45.1 o lê, mas não o usa na exportação nem para encurtar o desligamento |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | não | Prazo de 2 s por exportação ao Jaeger, incluindo tentativas, fixado no ConfigMap e no compose. O padrão do exportador é 10 s; separadamente, o SDK aguarda até 30 s pelo worker no desligamento |

O boot falha com mensagem clara se faltar uma variável obrigatória, se `JWKS_URL`/`BILLING_URL` não forem URL http(s) com host ou se uma URL de conexão trouxer a senha de demonstração fora de `development`/`test`. Valores de demonstração em `.env.example`.
