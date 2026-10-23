# Cobertura

Detalhe de [Cobertura](../README.md#cobertura) do README.

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

## Oscilação

A cobertura não oscila neste serviço: o run 39536414057 (o CD da `main` de 21/10/2026, que a entrega cita) e o 39937419103 (o da tabela) medem 100,0% de linhas e de ramos. O que oscilou foi um teste de integração do relay: o CD da `main` no commit do #21 (`ec4721b`, run 39531912471) reprovou no job `ci / test` porque `test_bloqueio_no_meio_do_lote_devolve_as_linhas_que_sobraram` esperava `status` e `tentativas` das linhas que sobraram, que o claim não muda, e a espera passava antes de o relay devolvê-las. O [#23](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/pull/23) passou a esperar pela contagem das vencidas, o efeito que o teste confere, e o CD do commit seguinte (`0ec76fe`) passou ([registro de cobertura do `platform`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/entrega/fase4/evidencias/coverage.md#runs-citados)).
