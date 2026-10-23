# Como rodar: detalhes

Detalhe da seção [Como rodar](../README.md#como-rodar) do README, com os casos que ela não traz: fora do compose, sem o OS Service, o seed e o fluxo à mão pelo RabbitMQ. As variáveis estão em [`configuracao.md`](configuracao.md).

## Fora do compose

Fora do compose, `.env.example` traz as variáveis com os valores de demonstração (`cp .env.example .env`, depois `set -a; . ./.env; set +a; make run`).

## Sem o OS Service no ar

Sem o OS Service no ar, rota autenticada responde 401 sem token e 503 com `Retry-After` com um token bem formado (JWKS indisponível): aponte `JWKS_URL` e `BILLING_URL` para serviços acessíveis ou use a stack completa do repositório `platform`.

## Seed de demonstração

Roda no boot do compose; avulso: `DATABASE_URL=... make seed`, idempotente: só cria o que falta e nunca altera saldo existente. Os SKUs são os mesmos da tabela de preços do Billing:

| SKU | Nome | Saldo |
|---|---|---|
| `PEC-OLEO-5W30` | Oleo 5W30 (litro) | 40 |
| `PEC-FILTRO-OLEO` | Filtro de oleo | 20 |
| `PEC-PASTILHA-FREIO` | Jogo de pastilhas de freio | 10 |
| `PEC-DISCO-FREIO` | Disco de freio | 6 |
| `PEC-AMORTECEDOR` | Amortecedor dianteiro | 4 |
| `PEC-VELA` | Vela de ignicao | 0 (cenário de falta de peça da demo da saga) |

## Relay e consumidor fora do compose

Para rodar fora do compose, com o RabbitMQ e o PostgreSQL do `make compose-up` no ar, o primeiro comando carrega a `DATABASE_URL` e a `RABBITMQ_URL` de demonstração, o relay publica a outbox e o consumidor consome a `execucao.comandos` (cada processo no seu terminal):

```bash
set -a; . ./.env; set +a
uv run python -m src.relay
uv run python -m src.consumidor
```

## Fluxo à mão pelo RabbitMQ

Para ver o fluxo à mão com o `make compose-up` no ar, publique um comando como o OS Service (usuário `os`) e leia a resposta em `os.eventos` no console do RabbitMQ (`http://127.0.0.1:15673`, usuário `admin`, senha de demonstração em [`contratos/rabbitmq/rabbitmq-admin.json`](../contratos/rabbitmq/rabbitmq-admin.json); em Queues and Streams, `os.eventos`, Get messages). Um `LiberarReserva` de uma ordem nova grava a lápide e responde `ReservaLiberada`:

```bash
export RABBITMQ_OS_PASSWORD=pytstop-os-demo-2026
uv run python - <<'PY'
import json, os, uuid
from datetime import UTC, datetime
import pika
ordem = str(uuid.uuid4())
envelope = {"id": str(uuid.uuid4()), "tipo": "LiberarReserva", "versao": 1,
            "origem": "os-service", "correlation_id": ordem, "causation_id": None,
            "ocorrido_em": datetime.now(UTC).isoformat(),
            "dados": {"ordem_id": ordem, "motivo": "cancelamento"}}
credencial = pika.PlainCredentials("os", os.environ["RABBITMQ_OS_PASSWORD"])
conexao = pika.BlockingConnection(pika.ConnectionParameters("127.0.0.1", 5673, "/", credencial))
conexao.channel().basic_publish(
    "pytstop.comandos", "comando.execucao.liberar_reserva", json.dumps(envelope),
    pika.BasicProperties(message_id=envelope["id"], correlation_id=ordem,
                         type="LiberarReserva", user_id="os",
                         content_type="application/json", delivery_mode=2))
conexao.close()
PY
```
