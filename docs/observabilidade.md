# Observabilidade

Detalhe da seção [Observabilidade](../README.md#observabilidade) do README.

Métricas no `/metrics` de cada processo (a API na porta 8000; o relay e o consumidor, na `METRICS_PORT`, 9100), traces pelo protocolo do OpenTelemetry (OTLP) para o Jaeger e logs JSON com `trace_id`, `span_id` e `correlation_id` ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md) e [RFC-004, seção 9](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md#9-observabilidade)). O `/metrics` fica fora da borda: o Kong responde 404 em `/execucao/metrics`, e o Prometheus raspa cada pod direto, pelas anotações do template. Os dashboards, os alertas e o catálogo de métricas dos três serviços estão no [`observabilidade/README.md`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#catálogo-de-métricas) do `platform`; aqui estão as séries, os spans e os logs deste serviço, que eles leem.

Dashboards relacionados no Grafana: [Execução e estoque](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-negocio-execucao.json), [Mensageria](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-mensageria.json), [Serviços](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-servicos.json), [Bancos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-bancos.json). Consulte a documentação dos [painéis de negócio](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/negocio.md) e dos [técnicos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md).

O Jaeger do platform usa `MEMORY_MAX_TRACES=250`: o trace mais antigo sai quando entra o 251º, sem TTL; reiniciar o pod apaga todos. Capture o trace logo após gerar o fluxo ([retenção e limites](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#retenção-e-prazos)).

## Métricas

| Série | Tipo e rótulos | Processo | O que mede |
|---|---|---|---|
| `http_request_duration_seconds` | histograma; `method` (um dos sete métodos conhecidos ou `OUTRO`), `rota` (o template), `status` | api | Duração de cada requisição |
| `pytstop_circuit_breaker_aberto` | gauge; `dependencia` (`billing`, `jwks`) | api | 1 aberto, 0,5 em meia-abertura, 0 fechado |
| `pytstop_jwks_falhas_total` | contador | api | Buscas ao JWKS do OS Service que falharam |
| `pytstop_outbox_mais_antiga_segundos` | gauge calculada na raspagem | api | Idade da linha pendente mais antiga da outbox; 0 sem pendente. Continua certa com o relay parado, e é o que o alerta Outbox parada do `platform` lê (`pytstop-outbox-parada`: acima de 300 s por 5 min; no overlay `kind`, 30 s e 1 min) |
| `pytstop_execucao_duracao_media_segundos` | gauge calculada na raspagem; `servico` | api | Média, do início à finalização, das execuções finalizadas nos últimos 7 dias, uma vez por serviço do diagnóstico da ordem (RF-008). A ordem com dois serviços entra na média dos dois, com a duração inteira |
| `pytstop_pecas_em_falta` | gauge calculada na raspagem; `sku` | api | Reservas recusadas nos últimos 7 dias em que o SKU faltou |
| `pytstop_mensagens_publicadas_total` | contador; `tipo` | relay | Mensagens que o broker confirmou |
| `outbox_pendentes`, `outbox_dead` | gauge calculada na raspagem | relay | Linhas esperando o relay e linhas que esgotaram as tentativas |
| `pytstop_mensagens_consumidas_total` | contador; `tipo`, `resultado` | consumidor | Comandos resolvidos, depois do commit e do ack |
| `pytstop_reconexoes_ao_broker_total` | contador; `componente` (`relay` ou `consumidor`) | relay e consumidor | Esperas antes de reconectar ao RabbitMQ. A espera por causa do banco não conta. O rótulo não é `processo`, porque o Prometheus já põe esse em todo alvo e o da série viraria `exported_processo` |

- **Gauge calculada na raspagem** são as cinco que consultam o banco a cada raspagem: `outbox_pendentes` e `outbox_dead` no relay, e as três do coletor da API ([`src/coletor.py`](../src/coletor.py)), que só a API registra. Relay e consumidor não leem as séries da API, e a API segue exportando com eles parados. Cada réplica repete o mesmo valor, e os painéis agregam com `max`. A janela de 7 dias é a constante `JANELA_DE_NEGOCIO`.
- **Quanto o coletor da API pode demorar.** Cada raspagem abre uma conexão crua do driver, sem pool nem engine (o engine só empresta a URL e o dialeto) e fora do pool das requisições. Nada fica dividido com a raspagem anterior: os hooks de primeira conexão de um engine consultam o banco sob um mutex dele, e uma primeira conexão calada ali prenderia toda raspagem seguinte, mesmo com o banco de volta. A conexão abre em até 1 s (`connect_timeout`; a resolução do nome do banco o driver faz sem prazo) e roda as três consultas em sequência, em autocommit, cada uma com `statement_timeout` de 2 s nas opções da conexão (a espera por um lock inclusive). A raspagem tem um prazo total de 5 s (`PRAZO_DA_RASPAGEM_S`) contado de fora, com as consultas numa thread à parte: o timeout do banco não cobre o banco que cala com o socket aberto, e a raspagem esperaria para sempre. No prazo, o coletor derruba o socket da conexão que a thread ainda usa: a thread sai e o servidor vê a conexão terminar, então nada fica preso depois. A gauge que falhou ou não voltou até lá sai ausente, nunca em zero, e o resto do `/metrics` (latência HTTP, JWKS, circuitos, mensageria) sai como sempre. O pior caso é de 5 s, abaixo dos 10 s do `scrape_timeout` padrão do Prometheus. Medido num uvicorn de verdade, com um proxy TCP na frente do banco: todas as conexões mudas, 1,0 s (o `connect_timeout`); banco a 300 ms por pacote, 1,0 s; conexão que cala no meio da raspagem, inclusive a primeira do processo, 5,0 s (o prazo total) e, com o banco de volta, a raspagem seguinte em 0,01 s; três tabelas travadas, 5,0 s; sempre sem as gauges que não vieram e com o resto do `/metrics`. Linha que o domínio não produz mas o banco aceita (execução finalizada sem início, item de serviço sem código, faltante sem SKU) fica fora das consultas, e não derruba a família.
- **Série de rótulo fechado nasce com zero no boot** (`tipo` e `resultado` do consumo, incluindo o `desconhecido`; `tipo` da publicação, os dez eventos do AsyncAPI; `componente`): sem isso o `increase()` dos painéis perde o primeiro evento de cada uma.
- **Rótulo aberto que vem do banco** (`servico`, `sku`) é código do catálogo, que o Billing e o estoque já validaram: nunca placa, documento, nome ou texto livre. O `rota` das requisições é o template (`/api/v1/execucoes/{ordem_id}/inicio`), e a requisição sem rota casada agrega em `nao_roteada`. O `method` do `http_request_duration_seconds` é o que o cliente mandou e não vem do banco, então só GET, POST, PUT, PATCH, DELETE, HEAD e OPTIONS viram rótulo, e qualquer outro método agrega em `OUTRO`, o mesmo rótulo do OS Service.

## Traces

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

O amostrador (`AmostradorDeRaiz`) é o `ParentBased` do SDK com uma raiz que descarta o span de cliente sem pai: o poll de 5 s do relay, as contagens do `/metrics` e a limpeza da retenção só abrem esse tipo de span, e cada um viraria um trace raiz no Jaeger (as consultas do coletor da API correm numa conexão crua do driver, fora do SQLAlchemy, e não abrem span nenhum). A consulta de uma requisição ou de uma mensagem tem pai e segue a decisão dele (um `traceparent` do chamador sem o bit de amostragem derruba a requisição e as consultas dela). O amostrador é fixo no código, e `OTEL_TRACES_SAMPLER` não o troca. A API instala o mesmo provider do relay e do consumidor ([`telemetria.py`](../src/compartilhado/infraestrutura/mensageria/telemetria.py)), passa-o ao FastAPI, ao engine e ao cliente do Billing em [`instrumentacao.py`](../src/compartilhado/infraestrutura/instrumentacao.py) e o encerra no fim do `lifespan`, o que descarrega os spans que esperam no lote: o SDK só o faria no `atexit`, que não roda quando o uvicorn re-levanta o SIGTERM fora do PID 1.

Saída do pod com o Jaeger fora do ar: o SDK aguarda até 30 s no desligamento para o worker drenar a fila, e o exportador gRPC tem prazo padrão de 10 s por exportação, incluindo tentativas. A espera pode consumir os 30 s do `terminationGracePeriodSeconds`. `OTEL_EXPORTER_OTLP_TIMEOUT=2`, no ConfigMap e no compose, limita cada exportação e reduz essa espera. O ensaio registrado mediu cerca de 4 s em contêiner, com o uvicorn como PID 1 e 4.000 requisições na fila do exportador. `OTEL_BSP_EXPORT_TIMEOUT=2000` permanece configurado, mas o SDK 1.45.1 do lock lê esse valor sem usá-lo na exportação; ele não causa a redução do prazo. Os 30 s são o prazo de espera pelo worker no desligamento, não um prazo por lote.

## Logs

Uma linha JSON por evento no stdout de cada processo, com o nome do evento em inglês (`http_request`, `message_published`, `command_ignored`, `audit`), o `trace_id` e o `span_id` do span corrente, o `correlation_id` (a ordem de serviço; no `AnonimizarVeiculo`, o veículo) e, na API, o `request_id` (o `X-Request-ID` que o Kong gera, devolvido na resposta). O access log é o `http_request` do middleware, com método, caminho, status e duração; o do uvicorn fica desligado (`--no-access-log`). Antes de sair, toda linha passa pelo mascaramento de dados pessoais: as chaves sensíveis (placa, descrição do problema, observações, `motivo`, token, senha) viram `***`, e CPF, CNPJ, e-mail, telefone e placa são mascarados dentro de qualquer texto ([`logging.py`](../src/compartilhado/infraestrutura/logging.py)). No cluster, o Promtail do `platform` leva o stdout dos pods ao Loki, que o Grafana consulta.

## O que não é exportado, e por quê

| Fica de fora | Por quê | Como |
|---|---|---|
| `/metrics` e as sondas (`/api/v1/saude`, `/api/v1/saude/pronto`) | Chegam a cada poucos segundos e não contam história: cada raspagem seria um trace | `excluded_urls` ancorado no fim do caminho (`/metrics$`, `/api/v1/saude(/pronto)?$`). O `Host` vem do chamador, e sem a âncora `Host: x/api/v1/saude` tiraria do trace qualquer requisição |
| O caminho cru e a query da requisição | O caminho traz a ordem e o SKU, e a query traz o filtro do cliente | O `server_request_hook` ([`trocar_url_pelo_template`](../src/compartilhado/infraestrutura/instrumentacao.py)) grava o template da rota em `url.path`, `http.target`, `http.url` e `url.full` (`nao_roteada` sem rota casada) e `REDACTED` em `url.query`. Ele não decompõe a URL, então um `Host` malformado não o derruba |
| User-Agent, IP e porta do par da conexão, `Host` | Texto livre sem limite e dado de rede de quem chamou, sem uso operacional; o Jaeger guarda por contagem, sem como apagar um titular | O mesmo hook grava `REDACTED` em `http.user_agent`, `user_agent.original`, `net.peer.ip`, `client.address`, `http.server_name`, `http.host` e `server.address`, e `0` nas portas, nas convenções antiga e nova do OpenTelemetry (`OTEL_SEMCONV_STABILITY_OPT_IN`) |
| O `baggage` do chamador | O ADR-043 prevê só `traceparent` e `tracestate`; o padrão do SDK somaria o `baggage`, que a API aceitaria e o httpx repassaria ao Billing | O propagador global é só o `TraceContext` (`instalar_propagador`) |
| O `tracestate` acima de 512 caracteres (o teto do W3C) | O valor vem de fora e seria regravado na outbox, em cada cópia de retry e na chamada ao Billing | O propagador o descarta na leitura, venha do HTTP, do AMQP ou da outbox; o `traceparent` segue valendo |
| O corpo das requisições e os valores dos parâmetros SQL | Dado do pedido | O instrumentador do SQLAlchemy grava só o texto do comando em `db.statement`, com os marcadores, e nunca lê os parâmetros. O `hide_parameters` do engine cuida da mensagem de erro e do log do SQLAlchemy, não do span. O do FastAPI e o do httpx só capturam cabeçalho e corpo se configurados, e não são |
| A mensagem e o stack trace das exceções, e a descrição do status de erro | O erro do driver traz o `DETAIL` do PostgreSQL com a linha inteira (placa, descrição do problema, observações), e o instrumentador do SQLAlchemy o grava no status do span | O `_SemTextoDeErro` envolve o exportador e limpa todo span, do serviço ou do instrumentador (fica o tipo da exceção). O motivo de uma falha de publicação vai em `pytstop.falha`, texto fixo (a classe do erro ou o código do broker) |
| Os spans de `receive` e `send` do ASGI | Multiplicariam os spans de cada requisição sem dizer nada que o SERVER não diga | `exclude_spans=["receive", "send"]` |
| Spans do cliente AMQP e do psycopg2 | O `PikaInstrumentor` duplicaria os spans e injetaria o contexto do relay em vez do gravado na outbox, e o SQLAlchemy já cobre o psycopg2 | Os spans de publicação e de consumo são do próprio serviço |
| Métrica com placa, documento, nome ou texto livre em rótulo | Cardinalidade e dado pessoal | Os rótulos abertos que vêm do banco são só `servico` e `sku`, códigos do catálogo. O `method`, que vem do cliente, só aceita sete métodos e `OUTRO` (ver [Métricas](#métricas)) |

A continuação do `traceparent` no SERVER do Billing já foi vista num cluster: o ensaio de observabilidade do platform guardou um trace com os três serviços, inclusive o CLIENT da Execução e o SERVER do Billing em `/api/v1/precos/validacao` ([evidência](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/entrega/fase4/evidencias/observabilidade.md)). O que só os testes provam, e não um cluster: o CLIENT do Billing ao Mercado Pago real, que depende da prova na sandbox do provedor, e o prazo de saída do pod pelo kubelet (provado em contêiner, com o uvicorn como PID 1).

## Como ver

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

O cluster liga `OTEL_ENABLED=true` ([`k8s/base/kustomization.yaml`](../k8s/base/kustomization.yaml)); o compose deixa `false`, e nada exporta nele. Para ver um trace sem cluster, suba um Jaeger (`docker run --rm -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:1.76.0`) e rode a API fora do compose, como em [Como rodar](como-rodar.md#fora-do-compose), com `OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 make run`.

A raspagem direta, com o `make compose-up` no ar, é `curl http://127.0.0.1:8003/metrics` para a API; para o relay e o consumidor, `docker compose exec relay python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())"`.
