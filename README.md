# PytStop fase 4: Execution Service

Serviço de execução e produção da oficina: fila de diagnóstico, fila de execução, apontamentos do mecânico e estoque físico de peças (reserva, liberação e baixa). Banco próprio: PostgreSQL 16 (`execucao`). Participa da saga de atendimento orquestrada pelo OS Service.

Parte da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT): o PytStop, sistema de gestão de oficina mecânica das fases anteriores, refatorado em microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

Este repositório traz o domínio, os casos de uso, a API REST e os dois processos de mensageria do serviço: o relay da outbox para o RabbitMQ e o consumidor dos comandos da saga ([Mensageria](#mensageria)). A mesma imagem roda os três processos, com comandos diferentes.

Arquitetura da fase 4: [RFC-004 e ADRs 034 a 043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura) no repositório `platform` (divisão dos serviços, saga, catálogo de mensagens, rotas, dados e segurança). A arquitetura deste serviço, com o modelo de dados completo e os fluxos passo a passo em diagramas, está em [`docs/arquitetura.md`](docs/arquitetura.md); o OpenAPI e a coleção Postman, em [`docs/api/`](docs/api) ([API](#api)).

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
| `ReservarPecas` | `ReservarPecas` | `PecasReservadas` ou `ReservaDePecasFalhou{faltantes}`; SKU ausente do estoque ou inativo falta inteiro, com `disponivel` 0. SKU fora do formato do Billing: o contrato recusa, a mensagem vai para a `execucao.comandos.dlq` sem resposta e a saga compensa pelo prazo técnico (RFC-004, seção 4.6) | reserva ativa ou recusada: reemite a mesma resposta, sem decidir de novo; liberada ou consumida: descartado |
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
- **Contratos** ([`contratos/`](contratos)): AsyncAPI, envelope, schemas e exemplos das mensagens que o serviço consome e produz, e a topologia do RabbitMQ (definitions, permissões, init de usuários), copiados do `platform` no commit da `main` registrado em [`contratos/ORIGEM`](contratos/ORIGEM). O CI baixa o tarball desse commit e compara cada arquivo byte a byte (offline: `-m "not rede"`); o leitor é tolerante a campo novo e recusa `versao` desconhecida. O `sku` do `ReservarPecas` é mais estrito que o `codigo` dos orçamentos: só o formato do Billing (letras maiúsculas e dígitos separados por hífens, até 50 caracteres), o mesmo do `Sku` do estoque. Só os schemas e o AsyncAPI entram na imagem.
- **Trace**: o contexto W3C segue pela outbox e pelos headers AMQP; a exportação para o Jaeger pelo protocolo do OpenTelemetry (OTLP) liga com `OTEL_ENABLED=true` ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md)), e cada processo marca `pytstop.processo` nos spans. Diagnóstico e execução guardam o contexto do comando que os pôs em espera, e a ação do mecânico pela API roda como filha dele, com span link para quem retomou: o fato que ela publica sai no mesmo trace da saga. A requisição HTTP do mecânico tem span próprio, e o link do passo aponta para ele ([Observabilidade](#observabilidade)). Os logs JSON levam `trace_id`, `span_id` e `correlation_id`, inclusive os do caso de uso chamado pelo consumidor e os do relay, que saem dentro do span da publicação.
- **Métricas** (porta `METRICS_PORT`, padrão 9100, em cada processo): `pytstop_mensagens_publicadas_total{tipo}` e `outbox_pendentes`/`outbox_dead` no relay; `pytstop_mensagens_consumidas_total{tipo,resultado}` (`processada`, `duplicada`, `ignorada`, `retry`, `dlq`) no consumidor; `pytstop_reconexoes_ao_broker_total{componente}` nos dois. As séries de rótulo fechado nascem com zero no boot. O catálogo completo, com o da API, está em [Observabilidade](#observabilidade).
- **Prontidão**: cada processo toca um heartbeat em `/tmp/<processo>-heartbeat` a cada volta do laço (liveness; dependência fora não reinicia o pod) e mantém `/tmp/<processo>-pronto` enquanto está conectado (readiness). Na abertura da conexão com o broker, contam como broker fora o erro de conexão do cliente AMQP (`AMQPError`), o nome sem resolução no DNS (`socket.gaierror`, como no Service headless do RabbitMQ sem pod pronto) e o broker que aceita a conexão TCP e não responde o AMQP no prazo da pilha do cliente (15 s, `AMQPConnectorStackTimeout`); qualquer outro erro derruba o processo, para o Kubernetes reiniciá-lo e o defeito aparecer: falta de descritores, certificado TLS recusado e erro de disco ao tocar os arquivos de sinal. O cliente AMQP não põe prazo na resolução do nome, e a tentativa que falha toca o heartbeat de novo: a idade dele na espera é a da espera, não a soma das duas. A reconexão espera um sorteio entre 0 e o atraso da vez (1 a 30 s), para as réplicas não voltarem juntas. Depois de um erro de banco num handler, o consumidor confere o banco antes de consumir de novo: sem resposta, fecha a assinatura e sai da prontidão até o banco voltar, e as mensagens esperam na fila em vez de gastar a escada de retry. SIGTERM termina a mensagem em curso e fecha as conexões.

Para rodar fora do compose, com o RabbitMQ e o PostgreSQL do `make compose-up` no ar, o primeiro comando carrega a `DATABASE_URL` e a `RABBITMQ_URL` de demonstração, o relay publica a outbox e o consumidor consome a `execucao.comandos` (cada processo no seu terminal):

```bash
set -a; . ./.env; set +a
uv run python -m src.relay
uv run python -m src.consumidor
```

Para ver o fluxo à mão com o `make compose-up` no ar, publique um comando como o OS Service (usuário `os`) e leia a resposta em `os.eventos` no console do RabbitMQ (`http://127.0.0.1:15673`, usuário `admin`, senha de demonstração em [`contratos/rabbitmq/rabbitmq-admin.json`](contratos/rabbitmq/rabbitmq-admin.json); em Queues and Streams, `os.eventos`, Get messages). Um `LiberarReserva` de uma ordem nova grava a lápide e responde `ReservaLiberada`:

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

## Observabilidade

Métricas no `/metrics` de cada processo (a API na porta 8000; o relay e o consumidor, na `METRICS_PORT`, 9100), traces pelo protocolo do OpenTelemetry (OTLP) para o Jaeger e logs JSON com `trace_id`, `span_id` e `correlation_id` ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md) e [RFC-004, seção 9](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md#9-observabilidade)). O `/metrics` fica fora da borda: o Kong responde 404 em `/execucao/metrics`, e o Prometheus raspa cada pod direto, pelas anotações do template. Os dashboards, os alertas e o catálogo de métricas dos três serviços estão no [`observabilidade/README.md`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#catálogo-de-métricas) do `platform`; aqui estão as séries, os spans e os logs deste serviço, que eles leem.

### Métricas

| Série | Tipo e rótulos | Processo | O que mede |
|---|---|---|---|
| `http_request_duration_seconds` | histograma; `method`, `rota` (o template), `status` | api | Duração de cada requisição |
| `pytstop_circuit_breaker_aberto` | gauge; `dependencia` (`billing`, `jwks`) | api | 1 aberto, 0,5 em meia-abertura, 0 fechado |
| `pytstop_jwks_falhas_total` | contador | api | Buscas ao JWKS do OS Service que falharam |
| `pytstop_outbox_mais_antiga_segundos` | gauge calculada na raspagem | api | Idade da linha pendente mais antiga da outbox; 0 sem pendente. Continua certa com o relay parado, e é o que o alerta Outbox parada do `platform` lê (`pytstop-outbox-parada`: acima de 300 s por 5 min; no overlay `kind`, 30 s e 1 min) |
| `pytstop_execucao_duracao_media_segundos` | gauge calculada na raspagem; `servico` | api | Média, do início à finalização, das execuções finalizadas nos últimos 7 dias, uma vez por serviço do diagnóstico da ordem (RF-008). A ordem com dois serviços entra na média dos dois, com a duração inteira |
| `pytstop_pecas_em_falta` | gauge calculada na raspagem; `sku` | api | Reservas recusadas nos últimos 7 dias em que o SKU faltou |
| `pytstop_mensagens_publicadas_total` | contador; `tipo` | relay | Mensagens que o broker confirmou |
| `outbox_pendentes`, `outbox_dead` | gauge calculada na raspagem | relay | Linhas esperando o relay e linhas que esgotaram as tentativas |
| `pytstop_mensagens_consumidas_total` | contador; `tipo`, `resultado` | consumidor | Comandos resolvidos, depois do commit e do ack |
| `pytstop_reconexoes_ao_broker_total` | contador; `componente` (`relay` ou `consumidor`) | relay e consumidor | Esperas antes de reconectar ao RabbitMQ. A espera por causa do banco não conta. O rótulo não é `processo`, porque o Prometheus já põe esse em todo alvo e o da série viraria `exported_processo` |

- **Gauge calculada na raspagem** são as cinco que consultam o banco a cada raspagem: `outbox_pendentes` e `outbox_dead` no relay, e as três do coletor da API ([`src/coletor.py`](src/coletor.py)), que só a API registra. Relay e consumidor não leem as séries da API, e a API segue exportando com eles parados. Cada réplica repete o mesmo valor, e os painéis agregam com `max`. A janela de 7 dias é a constante `JANELA_DE_NEGOCIO`.
- **Quanto o coletor da API pode demorar.** Cada raspagem abre uma conexão crua do driver, sem pool nem engine (o engine só empresta a URL e o dialeto) e fora do pool das requisições. Nada fica dividido com a raspagem anterior: os hooks de primeira conexão de um engine consultam o banco sob um mutex dele, e uma primeira conexão calada ali prenderia toda raspagem seguinte, mesmo com o banco de volta. A conexão abre em até 1 s (`connect_timeout`; a resolução do nome do banco o driver faz sem prazo) e roda as três consultas em sequência, em autocommit, cada uma com `statement_timeout` de 2 s nas opções da conexão (a espera por um lock inclusive). A raspagem tem um prazo total de 5 s (`PRAZO_DA_RASPAGEM_S`) contado de fora, com as consultas numa thread à parte: o timeout do banco não cobre o banco que cala com o socket aberto, e a raspagem esperaria para sempre. No prazo, o coletor derruba o socket da conexão que a thread ainda usa: a thread sai e o servidor vê a conexão terminar, então nada fica preso depois. A gauge que falhou ou não voltou até lá sai ausente, nunca em zero, e o resto do `/metrics` (latência HTTP, JWKS, circuitos, mensageria) sai como sempre. O pior caso é de 5 s, abaixo dos 10 s do `scrape_timeout` padrão do Prometheus. Medido num uvicorn de verdade, com um proxy TCP na frente do banco: todas as conexões mudas, 1,0 s (o `connect_timeout`); banco a 300 ms por pacote, 1,0 s; conexão que cala no meio da raspagem, inclusive a primeira do processo, 5,0 s (o prazo total) e, com o banco de volta, a raspagem seguinte em 0,01 s; três tabelas travadas, 5,0 s; sempre sem as gauges que não vieram e com o resto do `/metrics`. Linha que o domínio não produz mas o banco aceita (execução finalizada sem início, item de serviço sem código, faltante sem SKU) fica fora das consultas, e não derruba a família.
- **Série de rótulo fechado nasce com zero no boot** (`tipo` e `resultado` do consumo, incluindo o `desconhecido`; `tipo` da publicação, os dez eventos do AsyncAPI; `componente`): sem isso o `increase()` dos painéis perde o primeiro evento de cada uma.
- **Rótulo aberto que vem do banco** (`servico`, `sku`) é código do catálogo, que o Billing e o estoque já validaram: nunca placa, documento, nome ou texto livre. O `rota` das requisições é o template (`/api/v1/execucoes/{ordem_id}/inicio`), e a requisição sem rota casada agrega em `nao_roteada`. O `method` do `http_request_duration_seconds` também é aberto (é o que o cliente mandou) e não vem do banco; fechá-lo num conjunto de métodos ainda está por fazer.

### Traces

Cada processo exporta com `service.name` `execution-service` (`OTEL_SERVICE_NAME`) e marca `pytstop.processo` (`api`, `relay` ou `consumidor`) no recurso.

| Span | Tipo | De onde vem |
|---|---|---|
| `GET /api/v1/estoque`, `POST /api/v1/execucoes/{ordem_id}/inicio` | SERVER | Uma por requisição, com o método e o template da rota |
| `SELECT execucao`, `INSERT execucao`, `UPDATE execucao` | CLIENT | Um por comando SQL do engine, filho do span em andamento (o SQLAlchemy cobre o psycopg2). O nome leva o comando e o banco, e `db.statement` o texto com os marcadores de parâmetro |
| `connect` | CLIENT | A obtenção da conexão do pool, dentro de um span (o instrumentador embrulha o `Engine.connect`) |
| `POST` | CLIENT | A chamada ao Billing na conclusão do diagnóstico, filha do passo, com o `traceparent` na requisição |
| `process <Comando>` | CONSUMER | Um por comando da saga, filho da publicação do OS Service; os comandos de banco do caso de uso são filhos dele |
| `publish <Evento>` | PRODUCER | Um por mensagem do relay, filho do contexto gravado na outbox; a marcação da linha entregue é filha dele |
| `iniciar diagnostico`, `concluir diagnostico`, `iniciar execucao`, `finalizar execucao` | INTERNAL | O passo do mecânico: filho do contexto guardado com o diagnóstico ou a execução, com span link para a requisição HTTP |

Os spans `publish` e `process` levam também `messaging.operation.type` (`send` e `process`), a tag que o Jaeger filtra nos três serviços. Os spans de mensagem e do passo levam `correlation_id` ou `messaging.message.conversation_id`, a ordem de serviço (ADR-043); é a única identificação do pedido que um span leva.

O amostrador (`AmostradorDeRaiz`) é o `ParentBased` do SDK com uma raiz que descarta o span de cliente sem pai: o poll de 5 s do relay, as contagens do `/metrics` e a limpeza da retenção só abrem esse tipo de span, e cada um viraria um trace raiz no Jaeger (as consultas do coletor da API correm numa conexão crua do driver, fora do SQLAlchemy, e não abrem span nenhum). A consulta de uma requisição ou de uma mensagem tem pai e segue a decisão dele (um `traceparent` do chamador sem o bit de amostragem derruba a requisição e as consultas dela). O amostrador é fixo no código, e `OTEL_TRACES_SAMPLER` não o troca. A API instala o mesmo provider do relay e do consumidor ([`telemetria.py`](src/compartilhado/infraestrutura/mensageria/telemetria.py)), passa-o ao FastAPI, ao engine e ao cliente do Billing em [`instrumentacao.py`](src/compartilhado/infraestrutura/instrumentacao.py) e o encerra no fim do `lifespan`, o que descarrega os spans que esperam no lote: o SDK só o faria no `atexit`, que não roda quando o uvicorn re-levanta o SIGTERM fora do PID 1.

Saída do pod com o Jaeger fora do ar: sem prazo próprio o SDK espera 30 s por lote (e 10 s por envio), e a API levaria os 30 s do `terminationGracePeriodSeconds` para sair. `OTEL_BSP_EXPORT_TIMEOUT=2000` e `OTEL_EXPORTER_OTLP_TIMEOUT=2`, no ConfigMap e no compose, levam a saída a cerca de 4 s (medido em contêiner, com o uvicorn como PID 1 e 4.000 requisições na fila do exportador).

### Logs

Uma linha JSON por evento no stdout de cada processo, com o nome do evento em inglês (`http_request`, `message_published`, `command_ignored`, `audit`), o `trace_id` e o `span_id` do span corrente, o `correlation_id` (a ordem de serviço; no `AnonimizarVeiculo`, o veículo) e, na API, o `request_id` (o `X-Request-ID` que o Kong gera, devolvido na resposta). O access log é o `http_request` do middleware, com método, caminho, status e duração; o do uvicorn fica desligado (`--no-access-log`). Antes de sair, toda linha passa pelo mascaramento de dados pessoais: as chaves sensíveis (placa, descrição do problema, observações, `motivo`, token, senha) viram `***`, e CPF, CNPJ, e-mail, telefone e placa são mascarados dentro de qualquer texto ([`logging.py`](src/compartilhado/infraestrutura/logging.py)). No cluster, o Promtail do `platform` leva o stdout dos pods ao Loki, que o Grafana consulta.

### O que não é exportado, e por quê

| Fica de fora | Por quê | Como |
|---|---|---|
| `/metrics` e as sondas (`/api/v1/saude`, `/api/v1/saude/pronto`) | Chegam a cada poucos segundos e não contam história: cada raspagem seria um trace | `excluded_urls` ancorado no fim do caminho (`/metrics$`, `/api/v1/saude(/pronto)?$`). O `Host` vem do chamador, e sem a âncora `Host: x/api/v1/saude` tiraria do trace qualquer requisição |
| O caminho cru e a query da requisição | O caminho traz a ordem e o SKU, e a query traz o filtro do cliente | O `server_request_hook` ([`trocar_url_pelo_template`](src/compartilhado/infraestrutura/instrumentacao.py)) grava o template da rota em `url.path`, `http.target`, `http.url` e `url.full` (`nao_roteada` sem rota casada) e `REDACTED` em `url.query`. Ele não decompõe a URL, então um `Host` malformado não o derruba |
| User-Agent, IP e porta do par da conexão, `Host` | Texto livre sem limite e dado de rede de quem chamou, sem uso operacional; o Jaeger guarda por contagem, sem como apagar um titular | O mesmo hook grava `REDACTED` em `http.user_agent`, `user_agent.original`, `net.peer.ip`, `client.address`, `http.server_name`, `http.host` e `server.address`, e `0` nas portas, nas convenções antiga e nova do OpenTelemetry (`OTEL_SEMCONV_STABILITY_OPT_IN`) |
| O `baggage` do chamador | O ADR-043 prevê só `traceparent` e `tracestate`; o padrão do SDK somaria o `baggage`, que a API aceitaria e o httpx repassaria ao Billing | O propagador global é só o `TraceContext` (`instalar_propagador`) |
| O `tracestate` acima de 512 caracteres (o teto do W3C) | O valor vem de fora e seria regravado na outbox, em cada cópia de retry e na chamada ao Billing | O propagador o descarta na leitura, venha do HTTP, do AMQP ou da outbox; o `traceparent` segue valendo |
| O corpo das requisições e os valores dos parâmetros SQL | Dado do pedido | O instrumentador do SQLAlchemy grava só o texto do comando em `db.statement`, com os marcadores, e nunca lê os parâmetros. O `hide_parameters` do engine cuida da mensagem de erro e do log do SQLAlchemy, não do span. O do FastAPI e o do httpx só capturam cabeçalho e corpo se configurados, e não são |
| A mensagem e o stack trace das exceções, e a descrição do status de erro | O erro do driver traz o `DETAIL` do PostgreSQL com a linha inteira (placa, descrição do problema, observações), e o instrumentador do SQLAlchemy o grava no status do span | O `_SemTextoDeErro` envolve o exportador e limpa todo span, do serviço ou do instrumentador (fica o tipo da exceção). O motivo de uma falha de publicação vai em `pytstop.falha`, texto fixo (a classe do erro ou o código do broker) |
| Os spans de `receive` e `send` do ASGI | Multiplicariam os spans de cada requisição sem dizer nada que o SERVER não diga | `exclude_spans=["receive", "send"]` |
| Spans do cliente AMQP e do psycopg2 | O `PikaInstrumentor` duplicaria os spans e injetaria o contexto do relay em vez do gravado na outbox, e o SQLAlchemy já cobre o psycopg2 | Os spans de publicação e de consumo são do próprio serviço |
| Métrica com placa, documento, nome ou texto livre em rótulo | Cardinalidade e dado pessoal | Os rótulos abertos que vêm do banco são só `servico` e `sku`, códigos do catálogo (e o `method`, que vem do cliente: ver [Métricas](#métricas)) |

O que só os testes provam, e não um cluster: a continuação do `traceparent` no SERVER do Billing e o CLIENT do Billing ao Mercado Pago (o ensaio final de observabilidade, depois do merge dos três serviços, fecha), e o prazo de saída do pod pelo kubelet (provado em contêiner, com o uvicorn como PID 1).

### Como ver

```bash
make -C ../postech-sw-arch-p4-platform port-forward
```

Abre o Jaeger em `http://localhost:16686`, o Grafana em `http://localhost:3000` e o Prometheus em `http://localhost:9090` (repositório `platform`, irmão deste, com o kind no ar). Sem tráfego o Jaeger fica vazio: o E2E da saga (`make -C ../postech-sw-arch-p4-platform e2e`) ou o `make -C ../postech-sw-arch-p4-platform demo` gera os traces. No Jaeger escolha o serviço `execution-service`, filtre por `pytstop.processo` para um processo só, ou busque pelo atributo `messaging.message.conversation_id` com o `ordem_id` para ver a saga inteira, do `publish` do OS Service ao `process` e ao `publish` daqui, com os comandos de banco do consumidor e do relay. No Grafana (Explore) e no Prometheus, por exemplo:

```text
max by (servico) (pytstop_execucao_duracao_media_segundos)
max by (sku) (pytstop_pecas_em_falta)
max by (namespace) (pytstop_outbox_mais_antiga_segundos)
sum by (namespace, componente) (increase(pytstop_reconexoes_ao_broker_total[1h]))
```

O cluster liga `OTEL_ENABLED=true` ([`k8s/base/kustomization.yaml`](k8s/base/kustomization.yaml)); o compose deixa `false`, e nada exporta nele. Para ver um trace sem cluster, suba um Jaeger (`docker run --rm -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:1.76.0`) e rode a API fora do compose, como em [Como rodar](#como-rodar), com `OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 make run`.

A raspagem direta, com o `make compose-up` no ar, é `curl http://127.0.0.1:8003/metrics` para a API; para o relay e o consumidor, `docker compose exec relay python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())"`.

## API

Swagger em `/docs` (pela borda, `/execucao/docs`). O OpenAPI exportado ([`docs/api/openapi.json`](docs/api/openapi.json)) e a coleção Postman v2.1 ([`docs/api/execucao.postman_collection.json`](docs/api/execucao.postman_collection.json): uma requisição por rota, com corpo e resposta de exemplo e as variáveis `baseUrl`, `token`, `ordem_id` e `sku`) saem do próprio app pelo `make docs-api`, que não sobe servidor nem banco; o `make test` reprova se ficarem atrás do código ou se a coleção perder o token bearer, o `noauth` das sondas, o `Content-Type` dos corpos, o `{{baseUrl}}` das URLs ou a resposta de exemplo de uma requisição. Erros no envelope do p3, `{"erro": {"codigo", "mensagem", "id_requisicao"}}`; a exceção, também herdada do p3, é o 422 de validação de schema do FastAPI, que responde `{"detail": [{"type", "loc", "msg"}], "id_requisicao"}` (sem ecoar o valor recebido).

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
| `GET /metrics` | — | Métricas Prometheus: `http_request_duration_seconds{method,rota,status}`, `pytstop_circuit_breaker_aberto{dependencia}` (1 aberto, 0,5 em meia-abertura, 0 fechado; `billing` e `jwks`), `pytstop_jwks_falhas_total` e as três gauges do coletor da [Observabilidade](#observabilidade); fica fora da borda |

### Exemplos com `curl`

Um bloco por fluxo, com o método e a rota do OpenAPI. Os comandos rodam contra a stack completa no kind, pela borda (o Kong do `platform`, com certificado autoassinado, daí o `-k`), porque as rotas autenticadas validam o token no JWKS do OS Service; com o `make compose-up` sozinho só as sondas respondem, em `http://127.0.0.1:8003`.

Preparação: o token do admin, que pode agir em todas as rotas, pelo login do OS Service. O e-mail do admin é a chave `ADMIN_EMAIL` do ConfigMap do OS Service, achado pela chave e não pelo nome, como no [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md) do `platform`; a senha fica no Secret `os-admin`, e o `printf`, embutido no shell, a entrega ao `curl` pela entrada padrão, fora da lista de processos. Um mecânico cadastrado pelo admin no OS Service (`POST /api/v1/autenticacao/registrar`) usa o próprio token nas mesmas rotas de diagnóstico e execução.

```bash
export BASE_URL=https://localhost/execucao
email=$(kubectl --context kind-pytstop-p4 -n pytstop-os get configmaps -o json | jq -r 'first(.items[].data.ADMIN_EMAIL // empty)')
senha=$(kubectl --context kind-pytstop-p4 -n pytstop-os get secret os-admin -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d)
export TOKEN=$(printf '{"email": "%s", "senha": "%s"}' "$email" "$senha" \
  | curl -sk https://localhost/os/api/v1/autenticacao/login -H 'Content-Type: application/json' --data @- \
  | jq -r .access_token)
```

**Saúde**, sem token (o `/metrics` não está no OpenAPI e fica fora da borda: [Observabilidade](#observabilidade)):

```bash
curl -sk "$BASE_URL/api/v1/saude"
curl -sk "$BASE_URL/api/v1/saude/pronto"
```

**Estoque**: escrita só do admin; leitura também do mecânico e do atendente. O SKU novo precisa existir também na tabela de preços do Billing, dono dos códigos, e o `DELETE` desativa a peça sem apagar o histórico:

```bash
export SKU=PEC-CORREIA-DENTADA
curl -sk -X POST "$BASE_URL/api/v1/estoque" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sku": "PEC-CORREIA-DENTADA", "nome": "Correia dentada", "quantidade_disponivel": 8}'
curl -sk "$BASE_URL/api/v1/estoque?limit=20" -H "Authorization: Bearer $TOKEN"
curl -sk "$BASE_URL/api/v1/estoque/$SKU" -H "Authorization: Bearer $TOKEN"
curl -sk -X PATCH "$BASE_URL/api/v1/estoque/$SKU/quantidade" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"quantidade_disponivel": 12}'
curl -sk -X PUT "$BASE_URL/api/v1/estoque/$SKU" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"nome": "Correia dentada (kit)", "ativo": true}'
curl -sk -X DELETE "$BASE_URL/api/v1/estoque/$SKU" -H "Authorization: Bearer $TOKEN"
```

**Diagnóstico**, pelo mecânico ou pelo admin. A ordem chega pela saga: uma OS aberta no OS Service (`POST /os/api/v1/ordens-de-servico`, no [README dele](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service#readme)) aparece na fila do mecânico, de onde sai o `ORDEM_ID`. Os códigos da conclusão são os do seed do Billing:

```bash
export ORDEM_ID=$(curl -sk "$BASE_URL/api/v1/diagnosticos?status=AGUARDANDO" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.items[0].ordem_id')
curl -sk -X POST "$BASE_URL/api/v1/diagnosticos/$ORDEM_ID/inicio" -H "Authorization: Bearer $TOKEN"
curl -sk -X POST "$BASE_URL/api/v1/diagnosticos/$ORDEM_ID/conclusao" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"itens": [{"tipo": "servico", "codigo": "SRV-FREIOS", "quantidade": 1},
                 {"tipo": "peca", "codigo": "PEC-PASTILHA-FREIO", "quantidade": 1}],
       "observacoes": "Pastilhas dianteiras no limite"}'
```

**Execução**, pelo mecânico ou pelo admin (a fila, também pelo atendente). Com o orçamento aprovado, a saga reserva as peças e, com o pagamento confirmado, agenda a execução, e a ordem entra na fila. O início é o pivot da saga e exige a reserva ativa; a finalização dá baixa nas peças:

```bash
curl -sk "$BASE_URL/api/v1/fila" -H "Authorization: Bearer $TOKEN"
curl -sk -X POST "$BASE_URL/api/v1/execucoes/$ORDEM_ID/inicio" -H "Authorization: Bearer $TOKEN"
curl -sk -X POST "$BASE_URL/api/v1/execucoes/$ORDEM_ID/finalizacao" -H "Authorization: Bearer $TOKEN"
```

O `make test` confere que cada método e rota do OpenAPI tem um `curl` aqui, com o token nas rotas autenticadas e, nas rotas com corpo, o `Content-Type: application/json` e um corpo que o schema da rota aceita ([`test_documentacao.py`](tests/unitarios/test_documentacao.py)).

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
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | não | Exportação OTLP/gRPC dos spans da API, do relay e do consumidor (desligada; `http://jaeger:4317`; `execution-service`). Desligada, os spans existem no processo (o contexto de trace segue pela outbox e pelo AMQP e os logs levam o `trace_id`), mas não saem dele |
| `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_EXPORTER_OTLP_TIMEOUT` | não | Prazo de cada lote (ms) e de cada envio ao Jaeger (s); o SDK as lê, e o ConfigMap e o compose as fixam em 2000 e 2. Sem elas, com o Jaeger fora do ar o pod espera 30 s para sair |

O boot falha com mensagem clara se faltar uma variável obrigatória, se `JWKS_URL`/`BILLING_URL` não forem URL http(s) com host ou se uma URL de conexão trouxer a senha de demonstração fora de `development`/`test`. Valores de demonstração em `.env.example`.

## Como rodar

Requisitos: [uv](https://docs.astral.sh/uv/) 0.11 ou mais novo (instala o Python 3.14 do `.python-version`) e Docker (compose e os testes de integração com testcontainers).

```bash
make compose-up
make compose-logs
make check
make compose-down
```

O `make compose-up` sobe a API em http://127.0.0.1:8003, o relay, o consumidor, o PostgreSQL (127.0.0.1:5433) e o RabbitMQ (127.0.0.1:5673, console em 15673, usuário `admin`); o `make compose-logs` acompanha o log da API; o `make check` roda `uv.lock` em dia, ruff, ruff format, import-linter, mypy strict, bandit, os testes com gate de cobertura (95%) e o `diff-cover` (90% no código que a branch muda em relação à `origin/main`); o `make compose-down` derruba a stack e apaga o volume.

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
| Job `execucao-migracao` | Com o dono do banco: `alembic upgrade head`, o `python -m src.banco restringir` (tira da aplicação a escrita na `alembic_version`) e a semente do estoque de demonstração |
| Service `execution-service` | Endereço interno da API, na porta 8000 |
| [`borda.yaml`](k8s/base/borda.yaml) | Ingress e Services da borda no Kong, cópia sem mudança do exemplo `borda-execution-service.yaml` do `platform`: saem `/execucao/api/v1`, `/execucao/docs` e `/execucao/openapi.json`, e `/execucao/api/v1/admin` e `/execucao/metrics` respondem 404 no próprio Kong ([ADR-038](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/038-borda-e-comunicacao-sincrona.md)) |
| ConfigMap `execution-service` | A [configuração](#configuração) comum: `ENVIRONMENT=production`, `ROOT_PATH=/execucao`, os endereços do JWKS do OS e do Billing, o pool do banco, a exportação OTLP para o Jaeger e a porta de métricas |
| NetworkPolicies | Entrada negada a todo pod do namespace e liberada só para o Kong e o Prometheus na API (8000), para o Prometheus nas métricas do relay, do consumidor (9100) e do exporter (9187), e para o próprio namespace no banco (5432) |

**Processos e sondas.** A API tem startup (`GET /api/v1/saude`, 2 s × 30), liveness (`GET /api/v1/saude`, só depois da startup) e readiness em `GET /api/v1/saude/pronto`, e serve o `/metrics` na porta `http`; a startup existe porque, com a CPU apertada, o boot passa de 20 s e três falhas da liveness matariam o pod no meio da subida. Relay e consumidor são sondados pelos arquivos em `/tmp` (Prontidão, em [Mensageria](#mensageria)): liveness pelo heartbeat com menos de 90 s, readiness pelo arquivo de pronto e startup pelo mesmo comando da liveness (2 s × 30), e não pelo pronto, para que o broker fora no boot deixe o processo fora da prontidão, em vez de reiniciá-lo, e não pela existência do arquivo, que o `emptyDir` mantém depois do restart do container, de qualquer idade (pela idade a janela encurta e não fecha: o heartbeat de menos de 90 s do container morto ainda satisfaz a startup até o processo novo girar o laço); os dois servem o `/metrics` na 9100. Cada pod espera, num initContainer (`aguarda-migracao`, `python -m src.banco aguardar`), que o banco chegue à última revisão do Alembic da imagem: num rolling update, os pods novos esperam o Job da versão nova, e os antigos seguem servindo (migração que expande e contrai). O banco à frente da imagem não é espera: depois de um `kubectl rollout undo`, o pod da imagem anterior sobe sobre o banco que o Job novo migrou, e o rollback não desce a versão do banco. O initContainer só lê a `alembic_version` e, a cada espera, loga por que ainda espera (banco fora do ar, com a primeira linha do erro do driver, que separa conexão recusada, senha errada e nome que não resolve; sem a tabela; ou atrás da imagem, com as duas revisões). Só o erro do driver do banco (conexão recusada, senha, DNS) é espera; erro de configuração e imagem sem nenhuma migração derrubam o initContainer na hora, em vez de virar laço com a mesma linha no log. Todo container roda sem root (1001; 999 o PostgreSQL e 65534 o exporter), sem escalar privilégio, sem capability, com seccomp `RuntimeDefault` e raiz somente leitura (o `/tmp` num `emptyDir` de 16 Mi, só nos que gravam nele: o exporter não grava nada), nenhum pod monta token de ServiceAccount, e cada um leva no template as anotações do Prometheus ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md)).

**Segredos e papéis do banco.** Nenhum Secret fica em `k8s/`. O `make deploy` do `platform` grava o `rabbitmq`, com a `RABBITMQ_URL` do usuário `execucao`, e o `execucao-postgres`, com uma senha por papel; a `DATABASE_URL` se monta no pod, por expansão de variável, e o `kubectl describe` mostra o molde, não a senha. O superusuário `postgres` só inicializa o volume: o [`papeis.sql`](k8s/base/papeis.sql), que a imagem roda uma vez, na primeira inicialização, cria o dono `execucao` (DDL, só o Job), o `execucao_app` (só DML nas tabelas do dono, pelos privilégios padrão, e só leitura na `alembic_version`, de que o Job tira a escrita depois de migrar: API, relay e consumidor) e o `execucao_exporter` (`pg_monitor`). O script tira do `PUBLIC` o `CONNECT` e o `TEMPORARY` dos bancos `execucao` e `postgres` (o padrão de todo banco novo), e cada papel fica só com o `CONNECT` de que precisa: a aplicação e o exporter não entram no banco `postgres` nem criam tabela temporária. O script lê as senhas do ambiente do container do banco (`\getenv`) e desliga, na própria sessão, todo parâmetro que grava o texto do comando antes de usá-las (`log_statement`, `log_min_error_statement`, `log_min_duration_statement`, `log_min_duration_sample` e `log_transaction_sample_rate`): o `psql` troca a variável pelo valor antes de enviar o comando, e um `log_statement` em `all`, um comando que falha ou o log por duração e por amostragem gravariam a senha no log do servidor. O banco não aceita conexão sem senha nem no loopback e no socket (`POSTGRES_INITDB_ARGS` com `scram-sha-256`): a imagem do PostgreSQL confia neles, e o exporter, no mesmo pod, entraria como `postgres`. O [`test_papeis_do_banco.py`](tests/integracao/test_papeis_do_banco.py) sobe a imagem e o ambiente do StatefulSet com esse script e confere cada papel e o log do servidor.

**Imagens de terceiros.** `postgres:16.15` e `prometheuscommunity/postgres-exporter:v0.20.1`, por tag fixa e as da tabela de versões do `platform`. O job `trivy` do `security.yml` lista no summary de cada PR e da execução semanal os HIGH e CRITICAL com correção disponível das duas ([`scripts/ci/relatorio-trivy-terceiros.sh`](scripts/ci/relatorio-trivy-terceiros.sh)), sem reprovar o job: nem o achado nem a falha da varredura (pull recusado, banco do trivy fora) reprovam, e a falha vira aviso no summary. Risco aceito, medido em 12/10/2026: na `postgres`, os 25 achados (1 CRITICAL e 24 HIGH) estão no `gosu`, binário Go que o pod não executa (ele roda como 999, e o entrypoint da imagem só chama o `gosu` como root); no exporter, os 16 HIGH estão no binário dele (`stdlib`, `x/crypto`, `x/net` e `x/text`), mas só o Prometheus alcança a porta 9187 (NetworkPolicy) e ele só fala com o PostgreSQL do próprio pod. Fora desses números, a varredura de segredos da `postgres` marca 1 HIGH (`AsymmetricPrivateKey`) em `/etc/ssl/private/ssl-cert-snakeoil.key`: é a chave de teste pública do pacote `ssl-cert` do Debian, igual em toda imagem que o traz, e o banco não a usa (os manifestos não ligam TLS). A correção depende de um build novo do fornecedor, então a política é subir a versão a cada release do serviço, junto da tabela do `platform`.

**No kind**, com o `platform` clonado ao lado deste repositório:

```bash
make -C ../postech-sw-arch-p4-platform kind-up deploy
make kind-deploy
../postech-sw-arch-p4-platform/scripts/ci/smoke-servicos.sh execution-service
```

O primeiro comando cria o cluster, sobe a plataforma e gera os Secrets. O `make kind-deploy` chama o `scripts/ci/implantar-servicos.sh` do `platform` (o diretório vem de `PLATFORM_DIR`), o mesmo do CD: constrói a imagem deste checkout com o commit como tag, carrega-a no kind, gera sobre o overlay (`KIND_OVERLAY`, padrão `kind`) um com a imagem trocada, apaga o Job anterior (Job é imutável), aplica e espera o banco, o Job e os Deployments, nessa ordem. O smoke confere o Job, os rollouts, `https://localhost/execucao/api/v1/saude` com 200 e `/execucao/metrics` com 404 pela borda, `up = 1` de cada pod no Prometheus e a NetworkPolicy barrando o banco a quem vem de outro namespace. Sem o OS e o Billing implantados, a API sobe, mas rota autenticada com token responde 503 (JWKS indisponível). Sem cluster, `make manifests` valida os três overlays, como o job `build` do CI.

**Troca de senha do banco**, com janela até o restart. São os passos da [troca de senha do banco](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#troca-de-senha-do-banco) do `platform`; o exemplo troca a do `execucao_app`, e para outro papel muda a chave e o nome do papel (`POSTGRES_EXPORTER_PASSWORD` e `execucao_exporter`, `POSTGRES_OWNER_PASSWORD` e `execucao`, `POSTGRES_PASSWORD` e `postgres`):

1. Grave a senha nova no Secret, para que ela não fique só numa variável do shell:

   ```bash
   senha=$(openssl rand -hex 24)
   kubectl --context kind-pytstop-p4 -n pytstop-execucao get secret execucao-postgres -o json \
     | SENHA="$senha" jq '.data["POSTGRES_APP_PASSWORD"] = (env.SENHA | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Aplique-a no banco, como `postgres`, pela entrada padrão do `kubectl exec -i`, nunca por argumento; os cinco `SET` a tiram do log do servidor, que de outro modo a gravaria pelo `log_statement`, pela linha `STATEMENT` de um comando que falha ou pelo log por duração e por amostragem. A saída esperada é cinco `SET` e um `ALTER ROLE`:

   ```bash
   { printf '%s\n' "$senha"; cat <<'SQL'
   SET log_statement = 'none';
   SET log_min_error_statement = 'panic';
   SET log_min_duration_statement = -1;
   SET log_min_duration_sample = -1;
   SET log_transaction_sample_rate = 0;
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
- **Observabilidade**: o provider dos testes é o de produção, com o amostrador e a limpeza do texto de erro. Uma requisição gera o span SERVER com os comandos de banco como filhos no mesmo trace, e a conclusão do diagnóstico leva o `traceparent` ao Billing (`httpx.MockTransport` e `respx`); a raspagem do `/metrics`, as sondas e os ciclos ociosos do relay, com o engine instrumentado, exportam zero span; nenhum span da instrumentação automática (requisição, banco, httpx) leva o caminho com a ordem, a query, a placa ou o texto livre, nem o `DETAIL` de um erro do PostgreSQL, em nenhum atributo, evento, link, status ou recurso (`tests/spans.py`); o `baggage` e o `tracestate` longo do chamador não entram nem saem, e o User-Agent, o `Host` e o par da conexão saem redigidos nas três convenções semânticas; cada série fechada vale zero no boot de um interpretador novo; e as gauges do coletor saem de uma fixture gravada pelos mapeamentos dos contextos (janela de 7 dias com a linha de dentro e a de fora, SKU e serviço repetidos, linhas incoerentes), com o banco atrás de um proxy TCP que cala, atrasa ou cala no meio da consulta (inclusive a primeira conexão do processo, com o banco de volta na raspagem seguinte), e com as tabelas travadas: o `/metrics` volta no prazo total, sem a gauge e com o resto, e o proxy e o processo ficam sem conexão nem thread do coletor.
- **Concorrência**: a disputa pela última unidade de uma peça, cada lock pessimista, cópias simultâneas dos comandos da saga e réplicas migrando juntas.
- **Implantação**: o PostgreSQL 16.15 com o ambiente do StatefulSet e o `papeis.sql` dos manifestos, como o usuário 999 e com a raiz somente leitura (o dono migra, a API sobe com `execucao_app`, que não faz DDL, o exporter não lê tabela, ninguém entra como `postgres` sem senha e nenhuma senha chega ao log do servidor, gravando todo comando); o `entrypoint.sh` com o `ROOT_PATH`; e o uvicorn com o prefixo da borda, servindo o Swagger sob ele e as sondas sem ele.

O gate de cobertura (ramos incluídos) é de 95% no total, no `.coveragerc`, e de 90% no código novo, no `make diff-cover`, que o job `test` roda em todo pull request ([Cobertura](#cobertura)). O teste da cópia dos contratos precisa de rede (marcador `rede`).

## Cobertura

O gate é de 95% no total, com linhas e ramos somados (`fail_under` no `.coveragerc`), e o código novo precisa de 90% (`make diff-cover`), como pede o ADR-041. Ficam fora da medição só o `if __name__ == "__main__"` dos processos (`exclude_also`), os `__init__.py` dos pacotes (o único não vazio só tem a docstring) e os `Protocol` puros dos repositórios, das portas do diagnóstico e da unidade de trabalho (`omit` do `.coveragerc`).

A tabela abaixo, por contexto e camada, não é digitada: o [`scripts/cobertura_resumo.py`](scripts/cobertura_resumo.py) a escreve entre os marcadores a partir do `coverage.xml` de um run do CI (artefato `relatorios-de-teste` do job `test`) e põe o link desse run no fim, e o `make test` reprova o trecho que sair do formato do script ou com as contas erradas ([`test_cobertura_resumo.py`](tests/unitarios/test_cobertura_resumo.py)). O CI repete a mesma tabela no summary do job `test` de cada run ([Actions → CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/workflows/ci.yml)), com o `diff-cover` do pull request logo abaixo.

<!-- cobertura:inicio -->
### Cobertura de testes: 100.0% no gate (linhas e ramos)

100.0% de linhas e 100.0% de ramos.

| Pacote | Linhas | Cobertas | Cobertura |
|---|---:|---:|---:|
| `src` | 318 | 318 | 100.0% |
| `src/compartilhado/aplicacao` | 59 | 59 | 100.0% |
| `src/compartilhado/dominio` | 144 | 144 | 100.0% |
| `src/compartilhado/infraestrutura` | 1324 | 1324 | 100.0% |
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
| **total** | 3681 | 3681 | 100.0% |

Tabela gerada por `scripts/cobertura_resumo.py` a partir do `coverage.xml` do [run 39106764174 do CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/runs/39106764174).
<!-- cobertura:fim -->

Para refazer a tabela com o `coverage.xml` de outro run, com o número dele em `RUN`:

```bash
destino=$(mktemp -d)
gh run download "$RUN" -n relatorios-de-teste -D "$destino"
uv run python scripts/cobertura_resumo.py "$destino/coverage.xml" README.md \
  "https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/runs/$RUN"
```

O `make diff-cover` lê o `coverage.xml` do `make test` e exige que ele exista e seja mais novo que o código de `src/`: com o XML ausente ou velho recusa e manda rodar o `make test` antes, em vez de passar sem medir nada. Mede também o arquivo novo que ainda não foi para o git (`--include-untracked`). O `make check` já roda os dois na ordem certa.

## Mutação

Cobertura diz que a linha rodou; mutação diz se a suíte percebe quando ela está errada (ADR-041). Não é gate de CI e não tem pontuação mínima.

O `mutmut` não roda em pacote chamado `src`, o caso deste serviço: a versão 3.8.0 espera o pacote em `src/<pacote>` e tira o prefixo `src.` do nome do módulo, e a primeira função mutada que os testes chamam reprova com

```text
AssertionError: Failed trampoline hit. Module name starts with `src.`, which is invalid
```

(`mutmut/stats.py`, `record_trampoline_hit`; visto no Billing, que tem o mesmo layout). Renomear o pacote ou remendar a ferramenta mudaria o que se mede, então a alternativa são mutantes escritos à mão: [`scripts/mutantes.toml`](scripts/mutantes.toml) lista 95 defeitos plausíveis nos módulos críticos, e [`scripts/mutacao_manual.py`](scripts/mutacao_manual.py), o mesmo script do Billing, aplica um por vez numa cópia do código (`mutants/`, fora do git) e roda só os testes que deveriam pegá-lo.

**Resultado** em 18/10/2026: 94 de 94 mutantes mortos (100,0%), com 1 equivalente de fora da conta.

| Área | Mutantes | Mortos | Equivalentes |
|---|---:|---:|---:|
| Status da execução | 9 | 9 | 0 |
| Status do diagnóstico | 10 | 10 | 0 |
| Reserva e saldo de peça | 15 | 15 | 0 |
| Saga (guardas, lápide e repetição do comando) | 10 | 10 | 0 |
| Idempotência e concorrência | 6 | 6 | 0 |
| Placa do veículo | 8 | 8 | 0 |
| Validação do JWT | 14 | 13 | 1 |
| Consumidor | 13 | 13 | 0 |
| Relay e outbox | 10 | 10 | 0 |
| **total** | **95** | **94** | **1** |

- **Limites que só a mutação mostrou, hoje com teste:**
  - corpo de 64 KiB cravados tratado como grande demais (`test_corpo_no_teto_exato_passa_do_limite_e_chega_ao_parse`);
  - recusa permanente do domínio que, sem o `ValorInvalidoError` em `_PERMANENTES`, ia para a DLQ do mesmo jeito, mas com o traceback e a mensagem do erro no log (`test_recusa_permanente_loga_so_a_classe_do_erro`);
  - JWK Set ainda fresco na hora exata dos 10 min e cópia velha valendo na hora exata de 1 h (`test_jwk_set_fica_fresco_10_minutos_e_renova_na_hora_exata` e `test_copia_velha_vale_ate_1_hora_e_nao_na_hora_exata`, que prendem as duas constantes);
  - memória da falha do JWKS diferente de 5 s e `Retry-After` do 503 arredondado para baixo (`test_falha_memorizada_responde_na_hora_sem_buscar_de_novo`, que prende a constante e espera 2,5 s);
  - placa aceita com um dígito a mais ou a menos, nos dois formatos, porque o regex casava só o começo ou um formato mais curto (casos em `test_placa_invalida_sem_ecoar_o_valor`);
  - execução iniciada fora de "em andamento", que dispara o aviso da eliminação LGPD com registro aberto (`test_em_andamento_vale_na_fila_e_em_execucao`).
- **Equivalente:** `HS256` na lista de algoritmos do JWT (J01), e só enquanto valem duas condições: o PyJWT amarra a chave ao algoritmo dela (na 2.15, o `decode` com um `PyJWK` recusa `Token algorithm 'HS256' does not match the key's algorithm 'RS256'`) e o validador passa um `PyJWK` ao `decode`. Com elas, a lista é a segunda barreira, e `test_algoritmo_simetrico_e_recusado` guarda o desfecho; sem elas, a lista `RS256` do ADR-039 é a única barreira, e nenhum teste a prende. O motivo fica no campo `equivalente` do `mutantes.toml`.
- **O que o percentual não é:** a lista é curada, então ele diz que a suíte pega esses 95 defeitos, e não é comparável ao de uma ferramenta que gera os mutantes sozinha.

Para refazer:

```bash
make mutacao
make mutacao MUTANTES="E01 C03"
```

Como o alvo se comporta:

- Para de iniciar mutantes depois de `MUTACAO_PRAZO` segundos (900); os que sobram saem `nao rodado`. A tabela vai para `reports/mutacao.md`.
- Antes do primeiro mutante de cada grupo de testes, roda o grupo na cópia sem mutante. Se ele já reprova ou estoura o tempo, os mutantes do grupo saem `erro`: um teste que já falha mataria qualquer um.
- A cópia leva também o `alembic.ini`, as `migrations/` e os `contratos/` (`[config]` do `mutantes.toml`), que os testes de integração leem a partir da raiz dela.
- Em cada grupo o teste unitário vem antes do de integração: o mutante que o unitário mata não sobe container. O que chega à integração sobe um PostgreSQL, e na mensageria também um RabbitMQ. Com `TEST_DATABASE_URL` apontando para um banco `*_test` no ar, o PostgreSQL não sobe.
- Sai com 2 se um id de `MUTANTES` não existir (a mensagem lista os válidos) ou se o `.toml` não bater com o código.
- Sai com 1 se algum mutante sair `erro`, que é medida inválida, como o Docker fora. Sobrevivente não reprova, porque não há gate (ADR-041).
- Com `erro` ou `nao rodado`, o percentual vira `n/d`.
- Um mutante novo entra com o trecho exato (`de`, uma ocorrência no arquivo), e `tests/unitarios/test_mutacao_manual.py` reprova se a lista envelhecer.

## Integração contínua

Todo PR para a `main` roda dois workflows, com jobs de nome estável (são os checks obrigatórios da branch protection):

| Workflow | Jobs | O que garante |
|---|---|---|
| `CI` (`.github/workflows/ci.yml`) | `lint`, `type-check`, `security`, `test`, `sonarqube`, `build` | `uv.lock` em dia, ruff + import-linter, mypy strict, bandit, testes com gate de 95% e, no pull request, 90% no código novo (`make diff-cover`; o summary mostra o número do gate, que soma linhas e ramos, o detalhe por pacote e o resultado do `diff-cover`; `coverage.xml`/`htmlcov` como artefato), quality gate do SonarQube Community efêmero (`.sonar/quality-gate.json`), os manifestos do Kubernetes (`make manifests`: os três overlays no kubeconform, contra os schemas do Kubernetes 1.35 e com `Secret` recusado, e no `trivy config`, sem achado HIGH ou CRITICAL, com a única exceção e o motivo em [`k8s/trivy-ignore.rego`](k8s/trivy-ignore.rego) e a conferência de que todo `PASSWORD` do `papeis.sql` vem de variável do `psql` lida por `\getenv`, que a exceção do trivy não enxerga) e smoke da imagem pelo compose (`make smoke`: entrypoint real com migração e seed, imagem como `1001:1001`, readiness 200, 401 sem token, sem header `server`, sem access log do uvicorn, sem o aviso da telemetria automática do FastAPI no boot (o compose leva o endpoint OTLP no ambiente da API, como o cluster), contratos de mensageria na imagem e a topologia do RabbitMQ fora dela, relay e consumidor saudáveis, o consumidor assinado na `execucao.comandos`, as famílias e as séries do `/metrics` de cada processo logo depois do boot (`scripts/smoke_metricas.py`: as gauges da API; os dez tipos publicados, as reconexões e as gauges da outbox do relay; os sete comandos e o `desconhecido` vezes os cinco resultados do consumidor) e a senha de demonstração recusada com `ENVIRONMENT=production`) |
| `Security` (`.github/workflows/security.yml`) | `pip-audit`, `gitleaks`, `trivy` | CVE nas dependências de runtime, segredos em todo o histórico do commit testado (um segredo commitado e apagado no commit seguinte também reprova; a regra de URI de banco e de broker com senha tem teste negativo, `scripts/ci/testa-gitleaks.sh`, que reprova a senha gerada num manifesto) e CVE HIGH/CRITICAL com correção na imagem (as imagens de terceiros dos manifestos entram só como relatório no summary); o resultado do `pip-audit` e o do `trivy` da imagem vão também para o summary do job (`scripts/ci/registra-no-summary.sh`, que mantém o código de saída da ferramenta); roda também toda segunda-feira |

`make check` roda localmente os mesmos gates de código do job `CI`, e `make manifests` e `make smoke`, os do job `build`.

## Dependências automatizadas

O Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml)) abre pull requests toda segunda às 06:00 (São Paulo), em três blocos:

- **uv**: `pyproject.toml` e `uv.lock`, com o prefixo `chore(deps)`.
- **Actions** dos workflows, com o prefixo `ci`, como no OS e no Billing.
- **Docker**: as imagens base do `Dockerfile`, com o prefixo `chore(deps)`.

Regras dos três:

- Patch e minor chegam agrupados num PR só por bloco; major abre PR próprio, para ser lido com o changelog.
- No máximo 3 PRs abertos por bloco.
- O PR leva os rótulos padrão do Dependabot (`dependencies` e o do ecossistema), que ele cria no repositório se faltarem.
- Cada PR passa pelos mesmos nove checks dos dois workflows, inclusive o `pip-audit`, o trivy e o gate de cobertura.

Exceções:

- **uv**: o `required-version` do `pyproject.toml` só tem piso (`>=0.11`), como no Billing. O atualizador do Dependabot roda um uv mais novo (0.12 em outubro de 2026), e com teto o `uv lock` dele recusaria o projeto. A imagem do uv no `Dockerfile` sobe pelo bloco do Docker; o piso e os Requisitos de [Como rodar](#como-rodar) sobem juntos, à mão, quando o serviço passar a exigir um uv mais novo.
- **OpenTelemetry**: sem grupo próprio. O Dependabot põe as bibliotecas `opentelemetry-*` no grupo de patch e minor do bloco uv, que ele trata como mais específico que um grupo por nome, e os patches e minors da família sobem juntos nele: o SDK fixa `opentelemetry-api` e `opentelemetry-semantic-conventions` na mesma versão, e subir só uma parte não resolve. A major fica fora do grupo.
- **Teto do OpenTelemetry**: o SDK e o exportador têm teto `<1.46` no `pyproject.toml`, e as instrumentações `<0.67`, como no OS e no Billing. O `mensageria/telemetria.py` troca `_status` e `_events` de um span, atributos privados do SDK que um minor novo pode mudar.
- **Python**: no bloco do Docker, major e minor ficam de fora. A versão sobe à mão, de uma vez, em `.python-version`, no `python-version` do `security.yml`, no `requires-python` e no `python_version` (mypy) do `pyproject.toml`, no `sonar.python.version` do `sonar-project.properties` e nas duas imagens do `Dockerfile`. Sem isso, o 3.14 → 3.15 chegaria como minor e trocaria só a imagem do runtime. O patch não passa pelo Dependabot: vem do rebuild da tag móvel `python:3.14-slim-trixie`.

Depois do congelamento do código em 06/11/2026, o `open-pull-requests-limit` de cada bloco passa de 3 para 0. O 0 desliga só as atualizações de versão: os PRs de segurança do Dependabot, habilitados no repositório, não entram no limite e seguem abrindo. Sobem à mão, porque o Dependabot não os enxerga ou a versão está fixada de propósito: `postgres` e `prometheuscommunity/postgres-exporter` (a cada release do serviço, junto da tabela do `platform`, seção [Implantação](#implantação)), o PostgreSQL e o RabbitMQ do compose e dos testes de integração, o kubeconform, o trivy, o gitleaks, o pip-audit, o SonarQube e o diff-cover.

## Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash.
