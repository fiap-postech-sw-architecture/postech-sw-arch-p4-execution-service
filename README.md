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
- **Outbox transacional (padrão do p3, o código da fase 3):** cada caso de uso grava o evento do catálogo da saga na tabela `outbox`, na mesma transação do estado, já como o envelope do contrato (validado contra o JSON Schema na gravação), com o destino (`pytstop.eventos`, `evento.execucao.<tipo>`) e o `traceparent` de quem gravou, e emite `NOTIFY outbox_novo` (o aviso entre sessões do PostgreSQL) no commit. O relay a publica no RabbitMQ ([Mensageria](#mensageria)).
- **Única chamada síncrona entre serviços:** conclusão do diagnóstico → Billing `POST /api/v1/precos/validacao`, com timeout de 2 s, 2 retries com jitter só em erro transitório (timeout, rede, protocolo, 502/503/504) e circuit breaker (5 falhas abrem por 30 s; depois, uma chamada de prova). O token do mecânico é repassado no `Authorization`. Circuito aberto, 5xx ou timeout depois dos retries: 503 (com `Retry-After` quando o circuito está aberto); 4xx do Billing ou resposta fora do contrato: 502 `RESPOSTA_INVALIDA_DA_DEPENDENCIA`, sem retry. Códigos e SKUs têm no máximo 50 caracteres, o limite do Billing, dono dos códigos.
- **Autenticação:** JWT RS256 emitido pelo OS Service, validado pela chave pública do JWKS (`JWKS_URL`, timeout de 2 s, cópia fresca por 10 min; com o OS fora, a última cópia boa vale por até 1 h e 3 falhas seguidas abrem um circuit breaker por 30 s), conferindo assinatura, `iss=pytstop-os-service`, `aud=pytstop`, `exp`, `sub` e `type=access`, com 10 s de tolerância de relógio. Toda falha de credencial responde o mesmo 401; papel válido sem permissão, 403; JWKS indisponível e sem cópia, 503 com `Retry-After`. Nenhum segredo compartilhado entre serviços.

Proveniência: `src/compartilhado` e o contexto `estoque` partem do [p3 no commit `08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1) (base de entidades, UoW, outbox, logging com mascaramento de PII, handlers de erro, padrões de teste, Dockerfile), enxugados ao que este serviço usa.

## Participação na saga

| Comando (OS → Execução) | Caso de uso | Resposta (evento) | Repetição do comando |
|---|---|---|---|
| `SolicitarDiagnostico` | `RegistrarSolicitacaoDeDiagnostico` | — (o próximo fato é `DiagnosticoIniciado`) | devolve o diagnóstico existente; concluído ou descartado, é descartado |
| `DescartarDiagnostico` | `DescartarDiagnostico` | `DiagnosticoDescartado` | reemite a resposta |
| `ReservarPecas` | `ReservarPecas` | `PecasReservadas` ou `ReservaDePecasFalhou{faltantes}`; SKU que o contrato aceita mas fora do formato do Billing (minúsculas, `.` ou `_`) falta inteiro, com `disponivel` 0 | reserva ativa ou recusada: reemite a mesma resposta, sem decidir de novo; liberada ou consumida: descartado |
| `LiberarReserva` | `LiberarReserva` | `ReservaLiberada`; reserva já consumida (execução finalizada) não volta e o comando é ignorado | reemite a resposta, sem devolver de novo |
| `AgendarExecucao` | `AgendarExecucao` | `ExecucaoAgendada{posicao_na_fila}`; só entra na fila a ordem com reserva `ATIVA` (RN-027) | na fila: reemite com a posição atual; iniciada, finalizada ou cancelada: descartado |
| `CancelarExecucao` | `CancelarExecucao` | `ExecucaoCancelada`; depois de iniciada (pivot), o comando é ignorado e o `ExecucaoIniciada` já publicado conta a história | reemite a resposta |
| `AnonimizarVeiculo` (LGPD, fora da saga) | `AnonimizarVeiculo` | nenhuma: a placa dos retratos do veículo (diagnóstico e cópia na execução) vira `ANONIMIZADO:{veiculo_id}`, e os textos livres dos diagnósticos dele (descrição do problema e observações, que podem trazer nome ou endereço), inclusive as observações das mensagens ainda guardadas na outbox, viram o marcador da eliminação | não muda o que já foi anonimizado (descartado) |

Regra de repetição: enquanto o desfecho vale, o comando repetido republica a resposta registrada; depois de compensado ou superado, é descartado com log, sem efeito e sem resposta. **Lápide:** a compensação que chega antes do comando original (passo em voo, RFC-004 §4.5) grava o agregado já no estado final (reserva `LIBERADA` sem peças, execução `CANCELADA`, diagnóstico `DESCARTADO` sem retrato) e responde; o original, quando chegar, encontra a lápide pela chave `ordem_id` e é descartado.

Ações do mecânico pela API: `DiagnosticoIniciado`, `DiagnosticoConcluido`, `ExecucaoIniciada` (pivot: daqui em diante a OS não cancela, por isso o início exige a reserva de peças ativa) e `ExecucaoFinalizada{pecas_consumidas}` (baixa do estoque na mesma transação). Só o mecânico que iniciou conclui o diagnóstico ou finaliza a execução; o admin pode fazê-lo em nome dele, com log de auditoria (quem, ação e ordem), como nas escritas do admin no estoque. Repetir a ação devolve o estado atual sem novo evento. O `motivo` dos comandos de compensação não é registrado aqui: o histórico da saga fica no OS Service.

## Mensageria

Comandos e eventos da saga pelo RabbitMQ ([ADR-036](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/036-mensageria-rabbitmq.md) e [RFC-004, seção 5](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md#5-mensageria)), com o usuário `execucao` do broker. A topologia (exchanges, filas, policies e permissões) é a do `platform`; o serviço só confere, por declaração passiva, a fila que lê e os exchanges em que escreve.

| Sentido | Onde | Mensagens |
|---|---|---|
| Consome | fila `execucao.comandos` (`pytstop.comandos`, `comando.execucao.#`) | `SolicitarDiagnostico`, `DescartarDiagnostico`, `ReservarPecas`, `LiberarReserva`, `AgendarExecucao`, `CancelarExecucao`, `AnonimizarVeiculo` (todas do OS Service) |
| Publica | exchange `pytstop.eventos`, routing key `evento.execucao.<tipo em snake_case>` | `DiagnosticoIniciado`, `DiagnosticoConcluido`, `DiagnosticoDescartado`, `PecasReservadas`, `ReservaDePecasFalhou`, `ReservaLiberada`, `ExecucaoAgendada`, `ExecucaoCancelada`, `ExecucaoIniciada`, `ExecucaoFinalizada` |

Envelope: `id`, `tipo`, `versao`, `origem` (`execution-service`), `correlation_id` (a ordem; no `AnonimizarVeiculo`, o veículo), `causation_id`, `ocorrido_em` (UTC) e `dados`; propriedades AMQP (o protocolo do RabbitMQ) `message_id`, `correlation_id`, `type`, `user_id`, `delivery_mode=2` e os headers `traceparent`/`tracestate`. O `causation_id` é como o orquestrador casa cada evento: a resposta a um comando leva o `id` dele (inclusive a resposta republicada para um reenvio com `id` novo); o fato que o mecânico gera pela API leva o `id` do comando que abriu o fluxo, guardado com o diagnóstico (`SolicitarDiagnostico`) e com a execução (`AgendarExecucao`).

- **Relay** (`python -m src.relay`): acorda pelo `NOTIFY` da outbox ou pelo poll de 5 s e drena a outbox em transações curtas, sem transação aberta durante a publicação. O claim pega até 10 linhas com `FOR UPDATE SKIP LOCKED`, em ordem de gravação e sem passar à frente de mensagem pendente da mesma ordem, e grava um lease de 60 s. O fim do lease é o token de cada linha, renovado antes de publicar e conferido ao gravar o desfecho: a réplica que perdeu a linha não grava por cima da outra.
- **Publicação**: com publisher confirms e `mandatory`, como filho do contexto de trace gravado na linha; só a confirmação marca `entregue`. A espera pela confirmação não tem prazo próprio (o cliente AMQP não expõe um): um broker que segue vivo e não confirma segura o relay nessa linha, sem transação aberta no banco; o lease vence e outra réplica pode reivindicá-la, e, sem toque no heartbeat em arquivo, a sonda de liveness reinicia o processo. Mensagem sem rota, recusada (nack) ou barrada pelo broker conta tentativa, com os atrasos do relay do p3 (1, 4, 16 e 64 s), e a quinta falha vira `dead` (`ultimo_erro` só com a classe e o código do broker); envelope fora do contrato vira `dead` direto. Queda do broker não conta: sem conexão o relay não reivindica, devolve as linhas que tinha em mãos e reconecta. Com o broker em alarme de memória ou disco (`Connection.Blocked`), o relay para de reivindicar até o desbloqueio; passados 30 s bloqueado, a conexão cai como numa queda. Uma vez por hora apaga, em lotes de 1000, as entregues há mais de 7 dias e as `dead` mortas há mais de 30 (contados do fim do último lease, não da gravação: a linha que ficou pendente mais tempo que isso ainda ganha os 30 dias para conferir e republicar).
- **Consumidor** (`python -m src.consumidor`): uma mensagem em voo por vez (prefetch 1). Para cada uma, abre um span filho da publicação, confere o envelope e o `dados` contra o contrato e a origem pelo `user_id` (o produtor do tipo no AsyncAPI; a cópia de retry, com `x-tentativa`, só do próprio `execucao`) e roda o caso de uso na transação da mensagem: o consumidor grava o `id` em `mensagens_processadas`, entrega ao handler uma sessão presa a essa transação e comita uma vez só, com efeito, respostas na outbox e o `id` juntos. `id` repetido recebe ack sem efeito; comando que não corresponde ao estado (lápide, original atrasado, compensação depois do pivot) é ignorado com ack, com o `id` gravado e o log `command_ignored` com o código do motivo (`COMANDO_ATRASADO` no original atrasado), e nunca vai para a DLQ ([ADR-036](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/036-mensageria-rabbitmq.md)).
- **Retry e DLQ** (a fila de mensagens mortas): erro transitório (banco fora, conexão fechada, pool cheio ou timeout, rede, dependência fora, corrida que a releitura não resolveu) publica uma cópia, sem `expiration`, na fila de retry da tentativa (`execucao.comandos.retry.1s`, `.5s`, `.15s`, `.60s` e `.300s`, cujo TTL, o tempo de vida na fila, a devolve à fila de trabalho), com `x-tentativa` incrementado. A original só recebe ack depois da confirmação da cópia, e cópia sem rota ou com nack leva a original para a DLQ. A falha depois da quinta cópia, o erro permanente (contrato, tipo, versão, origem, dado recusado pelo domínio ou pela dependência) e o erro não classificado vão direto para `execucao.comandos.dlq` (`basic_reject` sem requeue). Mensagem que derruba a conexão a cada entrega (header que o cliente AMQP não decodifica) sai pela DLQ no `delivery-limit` 5 da fila, sem levar as que estão atrás.
- **Entrada hostil**: corpo acima de 64 KiB nem passa pelo parse, e qualquer erro do parse ou da validação vira DLQ sem derrubar o processo; os ids das propriedades AMQP só entram em log e span com forma de UUID. O log do cliente AMQP fica em ERROR (em WARNING ele registra o início do corpo devolvido pelo broker).
- **Tempo do handler**: o handler roda na thread da conexão, sem heartbeat do AMQP (60 s, explícito). A transação da mensagem tem tetos menores que os da API (comando de 5 s, lock de 3 s), as conexões derrubam o socket do banco sem resposta em 10 s, e o comando cortado pelo teto vira retry, sem reconexão. Linhas de `mensagens_processadas` com mais de 30 dias são apagadas pelo consumidor, em lotes.
- **Contratos** ([`contratos/`](contratos)): AsyncAPI, envelope, schemas e exemplos das mensagens que o serviço consome e produz, e a topologia do RabbitMQ (definitions, permissões, init de usuários), copiados do `platform` no commit da `main` registrado em [`contratos/ORIGEM`](contratos/ORIGEM). O CI baixa o tarball desse commit e compara cada arquivo byte a byte (offline: `-m "not rede"`); o leitor é tolerante a campo novo e recusa `versao` desconhecida. Só os schemas e o AsyncAPI entram na imagem.
- **Trace**: o contexto W3C segue pela outbox e pelos headers AMQP; a exportação para o Jaeger pelo protocolo do OpenTelemetry (OTLP) liga com `OTEL_ENABLED=true` ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md)), e cada processo marca `pytstop.processo` nos spans. Diagnóstico e execução guardam o contexto do comando que os pôs em espera, e a ação do mecânico pela API roda como filha dele, com span link para quem retomou: o fato que ela publica sai no mesmo trace da saga. A instrumentação automática de FastAPI, SQLAlchemy, httpx e do cliente AMQP ainda não está ligada; até lá a requisição HTTP não tem span próprio e o link fica vazio. Os logs JSON levam `trace_id`, `span_id` e `correlation_id`, inclusive os do caso de uso chamado pelo consumidor.
- **Métricas** (porta `METRICS_PORT`, padrão 9100, em cada processo): `pytstop_mensagens_publicadas_total{tipo}` e `outbox_pendentes`/`outbox_dead` no relay; `pytstop_mensagens_consumidas_total{tipo,resultado}` (`processada`, `duplicada`, `ignorada`, `retry`, `dlq`) no consumidor.
- **Prontidão**: cada processo toca um heartbeat em `/tmp/<processo>-heartbeat` a cada volta do laço (liveness; dependência fora não reinicia o pod) e mantém `/tmp/<processo>-pronto` enquanto está conectado (readiness). Na abertura da conexão com o broker, contam como broker fora o erro de conexão do cliente AMQP (`AMQPError`), o nome sem resolução no DNS (`socket.gaierror`, como no Service headless do RabbitMQ sem pod pronto) e o broker que aceita a conexão TCP e não responde o AMQP no prazo da pilha do cliente (15 s, `AMQPConnectorStackTimeout`); qualquer outro erro derruba o processo, para o Kubernetes reiniciá-lo e o defeito aparecer: falta de descritores, certificado TLS recusado e erro de disco ao tocar os arquivos de sinal. O cliente AMQP não põe prazo na resolução do nome, e a tentativa que falha toca o heartbeat de novo: a idade dele na espera é a da espera, não a soma das duas. A reconexão espera um sorteio entre 0 e o atraso da vez (1 a 30 s), para as réplicas não voltarem juntas. Depois de um erro de banco num handler, o consumidor confere o banco antes de consumir de novo: sem resposta, fecha a assinatura e sai da prontidão até o banco voltar, e as mensagens esperam na fila em vez de gastar a escada de retry. SIGTERM termina a mensagem em curso e fecha as conexões.

Para rodar fora do compose, com o RabbitMQ e o PostgreSQL do `make compose-up` no ar:

```bash
set -a; . ./.env; set +a          # DATABASE_URL e RABBITMQ_URL de demonstracao
uv run python -m src.relay        # publica a outbox
uv run python -m src.consumidor   # consome execucao.comandos
```

Para ver o fluxo à mão com o `make compose-up` no ar, publique um comando como o OS Service (usuário `os`) e leia a resposta em `os.eventos` no console do RabbitMQ (`http://127.0.0.1:15673`, usuário `admin`, senha de demonstração em [`contratos/rabbitmq/rabbitmq-admin.json`](contratos/rabbitmq/rabbitmq-admin.json); em Queues and Streams, `os.eventos`, Get messages). Um `LiberarReserva` de uma ordem nova grava a lápide e responde `ReservaLiberada`:

```bash
export RABBITMQ_OS_PASSWORD=pytstop-os-demo-2026  # gitleaks:allow (senha de demonstracao do compose)
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
| `DATABASE_URL` | sim | PostgreSQL do serviço (no Kubernetes, com o papel `execucao_app`; [Implantação](#implantação)) |
| `ENVIRONMENT` | não | `development` ou `test` aceitam as senhas de demonstração do compose e do `.env.example`; qualquer outro valor, inclusive a variável ausente, é produção, e a API, o relay e o consumidor não sobem com elas em `DATABASE_URL` ou `RABBITMQ_URL` |
| `ROOT_PATH` | não | Prefixo da borda (`/execucao` no Kubernetes), passado ao uvicorn como `--root-path`: o Swagger atrás do Kong busca o `openapi.json` sob ele, e as sondas e o `/metrics`, que chegam direto ao pod, seguem sem ele. Vazio no compose, que serve na raiz |
| `JWKS_URL` | sim | JWKS do OS Service (`/.well-known/jwks.json`) |
| `BILLING_URL` | sim | Base do Billing para a validação de preços |
| `RUN_MIGRATIONS_ON_STARTUP`, `RUN_SEED_ON_STARTUP` | não | Ligados no compose; no Kubernetes a migração roda em Job. Várias réplicas migrando juntas se serializam por `pg_advisory_lock` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_S`, `DB_CONNECT_TIMEOUT_S` | não | Pool e conexão (5 + 10, 5 s, 3 s) |
| `DB_LOCK_TIMEOUT_MS`, `DB_STATEMENT_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | não | Limites do servidor (5 s, 15 s, 30 s); o de lock vale também para a migração |
| `RABBITMQ_URL` | relay e consumidor | Broker com o usuário `execucao`, usada como vem, com o vhost explícito (no kind, `amqp://execucao:<senha>@rabbitmq.pytstop-plataforma.svc.cluster.local:5672/%2F`, do Secret `rabbitmq`) |
| `METRICS_PORT` | não | Porta do `/metrics` do relay e do consumidor (9100) |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | não | Exportação OTLP/gRPC dos spans (desligada; `http://jaeger:4317`; `execution-service`) |

O boot falha com mensagem clara se faltar uma variável obrigatória, se `JWKS_URL`/`BILLING_URL` não forem URL http(s) com host ou se uma URL de conexão trouxer a senha de demonstração fora de `development`/`test`. Valores de demonstração em `.env.example`.

## Como rodar

Requisitos: [uv](https://docs.astral.sh/uv/) 0.11 (instala o Python 3.14 do `.python-version`) e Docker (compose e os testes de integração com testcontainers).

```bash
make compose-up    # API em http://127.0.0.1:8003, relay, consumidor, PostgreSQL (127.0.0.1:5433) e RabbitMQ (127.0.0.1:5673, console em 15673, usuário admin)
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

## Implantação

Os manifestos do Kubernetes estão em [`k8s/`](k8s) ([ADR-042](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/042-cicd-e-deploy-kubernetes.md)): a base e os overlays `kind` (local), `kind-ci` (o do CD) e `k3s` (VM na Azure). Tudo fica no namespace `pytstop-execucao`, com o Pod Security `restricted` em `enforce`. O `make deploy` do repositório `platform` cria o namespace vazio, gera os Secrets e sobe o RabbitMQ, o Kong e a observabilidade de que o serviço depende.

| Objeto | O que é |
|---|---|
| Deployments `execution-service-api`, `execution-service-relay` e `execution-service-consumidor` | A mesma imagem, `pytstop-execution-service`, com o comando de cada processo. A API não declara réplicas: quem decide é o HPA, por CPU, até 2 no `kind`, 1 no `kind-ci` e 3 no `k3s`. Relay e consumidor têm uma réplica |
| StatefulSet `execucao-postgres` e o Service headless dele | PostgreSQL 16.15 com volume de 1 Gi (5 Gi no `k3s`) e o `postgres_exporter` como sidecar, na porta 9187 |
| Job `execucao-migracao` | `alembic upgrade head` e a semente do estoque de demonstração, com o dono do banco |
| Service `execution-service` | Endereço interno da API, na porta 8000 |
| [`borda.yaml`](k8s/base/borda.yaml) | Ingress e Services da borda no Kong, cópia sem mudança do exemplo `borda-execution-service.yaml` do `platform`: saem `/execucao/api/v1`, `/execucao/docs` e `/execucao/openapi.json`, e `/execucao/api/v1/admin` e `/execucao/metrics` respondem 404 no próprio Kong ([ADR-038](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/038-borda-e-comunicacao-sincrona.md)) |
| ConfigMap `execution-service` | A [configuração](#configuração) comum: `ENVIRONMENT=production`, `ROOT_PATH=/execucao`, os endereços do JWKS do OS e do Billing, o pool do banco, a exportação OTLP para o Jaeger e a porta de métricas |
| NetworkPolicies | Entrada negada a todo pod do namespace e liberada só para o Kong e o Prometheus na API (8000), para o Prometheus nas métricas do relay, do consumidor (9100) e do exporter (9187), e para o próprio namespace no banco (5432) |

**Processos e sondas.** A API tem liveness em `GET /api/v1/saude` e readiness em `GET /api/v1/saude/pronto`, e serve o `/metrics` na porta `http`. Relay e consumidor são sondados pelos arquivos em `/tmp` (Prontidão, em [Mensageria](#mensageria)): liveness pelo heartbeat com menos de 90 s, readiness pelo arquivo de pronto e startup pela existência do heartbeat, para que o broker fora no boot deixe o processo fora da prontidão, em vez de reiniciá-lo; os dois servem o `/metrics` na 9100. Cada pod espera, num initContainer (`aguarda-migracao`), que o `alembic current` do banco chegue ao `alembic heads` da imagem: num rolling update, os pods novos esperam o Job da versão nova, e os antigos seguem servindo (migração que expande e contrai). Todo container roda sem root (1001; 999 o PostgreSQL e 65534 o exporter), sem escalar privilégio, sem capability, com seccomp `RuntimeDefault` e raiz somente leitura (o `/tmp` num `emptyDir` de 16 Mi), nenhum pod monta token de ServiceAccount, e cada um leva no template as anotações do Prometheus ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md)).

**Segredos e papéis do banco.** Nenhum Secret fica em `k8s/`. O `make deploy` do `platform` grava o `rabbitmq`, com a `RABBITMQ_URL` do usuário `execucao`, e o `execucao-postgres`, com uma senha por papel; a `DATABASE_URL` se monta no pod, por expansão de variável, e o `kubectl describe` mostra o molde, não a senha. O superusuário `postgres` só inicializa o volume: o [`papeis.sql`](k8s/base/papeis.sql), que a imagem roda uma vez, na primeira inicialização, cria o dono `execucao` (DDL, só o Job), o `execucao_app` (só DML nas tabelas do dono, pelos privilégios padrão: API, relay e consumidor) e o `execucao_exporter` (`pg_monitor`). O script lê as senhas do ambiente do container do banco (`\getenv`) e desliga o log de comandos da própria sessão antes de usá-las: o `psql` troca a variável pelo valor antes de enviar o comando, e um `log_statement` em `all` ou um comando que falha gravariam a senha no log do servidor. O banco não aceita conexão sem senha nem no loopback e no socket (`POSTGRES_INITDB_ARGS` com `scram-sha-256`): a imagem do PostgreSQL confia neles, e o exporter, no mesmo pod, entraria como `postgres`. O [`test_papeis_do_banco.py`](tests/integracao/test_papeis_do_banco.py) sobe a imagem e o ambiente do StatefulSet com esse script e confere cada papel e o log do servidor.

**No kind**, com o `platform` clonado ao lado deste repositório:

```bash
make -C ../postech-sw-arch-p4-platform kind-up deploy                    # plataforma e Secrets
make kind-deploy                                                         # este checkout, pelo script do platform
../postech-sw-arch-p4-platform/scripts/ci/smoke-servicos.sh execution-service
```

O `make kind-deploy` chama o `scripts/ci/implantar-servicos.sh` do `platform` (o diretório vem de `PLATFORM_DIR`), o mesmo do CD: constrói a imagem deste checkout com o commit como tag, carrega-a no kind, gera sobre o overlay (`KIND_OVERLAY`, padrão `kind`) um com a imagem trocada, apaga o Job anterior (Job é imutável), aplica e espera o banco, o Job e os Deployments, nessa ordem. O smoke confere o Job, os rollouts, `https://localhost/execucao/api/v1/saude` com 200 e `/execucao/metrics` com 404 pela borda, `up = 1` de cada pod no Prometheus e a NetworkPolicy barrando o banco a quem vem de outro namespace. Sem o OS e o Billing implantados, a API sobe, mas rota autenticada com token responde 503 (JWKS indisponível). Sem cluster, `make manifests` valida os três overlays, como o job `build` do CI.

**Troca de senha do banco**, com janela até o restart. São os passos da [troca de senha do banco](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#troca-de-senha-do-banco) do `platform`; o exemplo troca a do `execucao_app`, e para outro papel muda a chave e o nome do papel (`POSTGRES_EXPORTER_PASSWORD` e `execucao_exporter`, `POSTGRES_OWNER_PASSWORD` e `execucao`, `POSTGRES_PASSWORD` e `postgres`):

1. Grave a senha nova no Secret, para que ela não fique só numa variável do shell:

   ```bash
   senha=$(openssl rand -hex 24)
   kubectl --context kind-pytstop-p4 -n pytstop-execucao get secret execucao-postgres -o json \
     | SENHA="$senha" jq '.data["POSTGRES_APP_PASSWORD"] = (env.SENHA | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Aplique-a no banco, como `postgres`, pela entrada padrão do `kubectl exec -i`, nunca por argumento; os dois `SET` a tiram do log do servidor. A saída esperada é `SET`, `SET` e `ALTER ROLE`:

   ```bash
   { printf '%s\n' "$senha"; cat <<'SQL'
   SET log_statement = none;
   SET log_min_error_statement = panic;
   \getenv senha SENHA_NOVA
   ALTER ROLE execucao_app PASSWORD :'senha';
   SQL
   } | kubectl --context kind-pytstop-p4 -n pytstop-execucao exec -i statefulset/execucao-postgres -c postgres -- \
       sh -c 'read -r SENHA_NOVA && [ -n "$SENHA_NOVA" ] && export SENHA_NOVA && PGPASSWORD=$POSTGRES_PASSWORD exec psql -w -U postgres -v ON_ERROR_STOP=1'
   ```

3. Reinicie quem usa o papel, que lê a senha só no start: `execucao_app`, os Deployments (`kubectl --context kind-pytstop-p4 -n pytstop-execucao rollout restart deployment`); `execucao_exporter` e `postgres`, o StatefulSet do banco (o exporter é sidecar dele, e o comando do passo 2 usa a senha do superusuário lida no start); `execucao`, ninguém: o Job a relê no deploy seguinte. Até o restart, conexão nova com a senha antiga é recusada.

## Testes

`make test` (ou `uv run pytest`) roda os unitários e os de integração. Os de integração sobem um PostgreSQL 16 e um RabbitMQ 4.3.6 efêmeros via testcontainers (o broker com as definitions, as permissões e o init de usuários copiados do `platform`, e o TTL das filas de retry reduzido para 100 ms) e aplicam as migrações do Alembic. Por área:

- **Mensageria**: de ponta a ponta, o teste publica o comando como o OS Service e lê o evento em `os.eventos`, com o schema, o `causation_id` e o trace encadeado conferidos (inclusive pela ação do mecânico); reentrega, reenvio, lápide dos três pares de compensação, cada cópia na fila de retry do próprio nível, DLQ, origem forjada, header que o cliente AMQP não lê, corpo hostil, falha depois do efeito, handler que tenta comitar e handler lento.
- **Relay**: duas réplicas em threads (lease, fencing e `SKIP LOCKED`), ordem por OS, mensagem sem rota, nack, recusa 403, alarme de memória e queda do broker no meio do lote, com o broker de verdade, e a retenção em lotes.
- **API e domínio**: repositórios, outbox (inclusive o `NOTIFY`), JWT real (chave RSA gerada no teste e JWKS servido por HTTP local, inclusive pendurado) e Billing simulado com `respx`.
- **Concorrência**: a disputa pela última unidade de uma peça, cada lock pessimista, cópias simultâneas dos comandos da saga e réplicas migrando juntas.
- **Implantação**: o PostgreSQL 16.15 com o ambiente do StatefulSet e o `papeis.sql` dos manifestos, como o usuário 999 e com a raiz somente leitura (o dono migra, a API sobe com `execucao_app`, que não faz DDL, o exporter não lê tabela, ninguém entra como `postgres` sem senha e nenhuma senha chega ao log do servidor, gravando todo comando); o `entrypoint.sh` com o `ROOT_PATH`; e o uvicorn com o prefixo da borda, servindo o Swagger sob ele e as sondas sem ele.

O gate de cobertura (ramos incluídos) é de 90%, no `.coveragerc`. O teste da cópia dos contratos precisa de rede (marcador `rede`).

## Integração contínua

Todo PR para a `main` roda dois workflows, com jobs de nome estável (são os checks obrigatórios da branch protection):

| Workflow | Jobs | O que garante |
|---|---|---|
| `CI` (`.github/workflows/ci.yml`) | `lint`, `type-check`, `security`, `test`, `sonarqube`, `build` | `uv.lock` em dia, ruff + import-linter, mypy strict, bandit, testes com gate de 90% (relatório por pacote no summary e `coverage.xml`/`htmlcov` como artefato), quality gate do SonarQube Community efêmero (`.sonar/quality-gate.json`), os manifestos do Kubernetes (`make manifests`: os três overlays no kubeconform, contra os schemas do Kubernetes 1.35 e com `Secret` recusado, e no `trivy config`, sem achado HIGH ou CRITICAL) e smoke da imagem pelo compose (`make smoke`: entrypoint real com migração e seed, imagem como `1001:1001`, readiness 200, 401 sem token, sem header `server`, sem access log do uvicorn, contratos de mensageria na imagem e a topologia do RabbitMQ fora dela, relay e consumidor saudáveis, o consumidor assinado na `execucao.comandos` e a senha de demonstração recusada com `ENVIRONMENT=production`) |
| `Security` (`.github/workflows/security.yml`) | `pip-audit`, `gitleaks`, `trivy` | CVE nas dependências de runtime, segredos em todo o histórico do commit testado (um segredo commitado e apagado no commit seguinte também reprova) e CVE HIGH/CRITICAL com correção na imagem; roda também toda segunda-feira |

`make check` roda localmente os mesmos gates de código do job `CI`, e `make manifests` e `make smoke`, os do job `build`.

## Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash.
