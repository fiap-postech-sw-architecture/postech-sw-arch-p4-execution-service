# Testes e cobertura

Detalhe de [Como testar](../README.md#como-testar) e de [Cobertura](../README.md#cobertura) do README.

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

A tabela do [README](../README.md#cobertura), por contexto e camada, não é digitada: o [`scripts/cobertura_resumo.py`](../scripts/cobertura_resumo.py) a escreve entre os marcadores a partir do `coverage.xml` de um run do CI (artefato `relatorios-de-teste` do job `test`) e põe o link desse run no fim, e o `make test` reprova o trecho que sair do formato do script, com as contas erradas, com a taxa de linhas diferente da do total, com o gate fora do intervalo entre as duas taxas ou com um pacote que não existe ([`test_cobertura_resumo.py`](../tests/unitarios/test_cobertura_resumo.py)). O CI repete a mesma tabela no summary do job `test` de cada run ([Actions → CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/workflows/ci.yml)), com o `diff-cover` do pull request logo abaixo.

Para refazer a tabela com o `coverage.xml` de outro run, com o número dele em `RUN`:

```bash
destino=$(mktemp -d)
gh run download "$RUN" -n relatorios-de-teste -D "$destino"
uv run python scripts/cobertura_resumo.py "$destino/coverage.xml" README.md \
  "https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/runs/$RUN"
```

O `make diff-cover` lê o `coverage.xml` do `make test` e exige que ele exista e seja mais novo que o código de `src/`: com o XML ausente ou velho recusa e manda rodar o `make test` antes, em vez de passar sem medir nada. Mede também o arquivo novo que ainda não foi para o git (`--include-untracked`). O `make check` já roda os dois na ordem certa.

### Oscilação

A cobertura não oscila neste serviço: o run 39536414057 (o CD da `main` de 21/10/2026, que a entrega cita) e o 39937419103 (o da tabela) medem 100,0% de linhas e de ramos. O que oscilou foi um teste de integração do relay: o CD da `main` no commit do #21 (`ec4721b`, run 39531912471) reprovou no job `ci / test` porque `test_bloqueio_no_meio_do_lote_devolve_as_linhas_que_sobraram` esperava `status` e `tentativas` das linhas que sobraram, que o claim não muda, e a espera passava antes de o relay devolvê-las. O [#23](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/pull/23) passou a esperar pela contagem das vencidas, o efeito que o teste confere, e o CD do commit seguinte (`0ec76fe`) passou ([registro de cobertura do `platform`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/entrega/fase4/evidencias/coverage.md#runs-citados)).
