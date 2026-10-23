# CI/CD

Detalhe de [CI/CD](../README.md#cicd) do README.

## Integração contínua

Todo PR para a `main` roda dois workflows, com jobs de nome estável (são os checks obrigatórios da branch protection):

| Workflow | Jobs | O que garante |
|---|---|---|
| `CI` (`.github/workflows/ci.yml`) | `lint`, `type-check`, `security`, `test`, `sonarqube`, `build` | `uv.lock` em dia, ruff + import-linter, mypy strict, bandit, testes com gate de 95% e, no pull request, 90% no código novo (`make diff-cover`; o summary mostra o número do gate, que soma linhas e ramos, o detalhe por pacote e o resultado do `diff-cover`; `coverage.xml`/`htmlcov` como artefato), quality gate do SonarQube Community efêmero (`.sonar/quality-gate.json`), os manifestos do Kubernetes (`make manifests`: os três overlays no kubeconform, contra os schemas do Kubernetes 1.35 e com `Secret` recusado, e no `trivy config`, sem achado HIGH ou CRITICAL, com a única exceção e o motivo em [`k8s/trivy-ignore.rego`](../k8s/trivy-ignore.rego) e a conferência de que todo `PASSWORD` do `papeis.sql` vem de variável do `psql` lida por `\getenv`, que a exceção do trivy não enxerga) e smoke da imagem pelo compose (`make smoke`: entrypoint real com migração e seed, imagem como `1001:1001`, readiness 200, 401 sem token, sem header `server`, sem access log do uvicorn, sem o aviso da telemetria automática do FastAPI no boot (o compose leva o endpoint OTLP no ambiente da API, como o cluster), contratos de mensageria na imagem e a topologia do RabbitMQ fora dela, relay e consumidor saudáveis, o consumidor assinado na `execucao.comandos`, as famílias e as séries do `/metrics` de cada processo logo depois do boot (`scripts/smoke_metricas.py`: as gauges da API; os dez tipos publicados, as reconexões e as gauges da outbox do relay; os sete comandos e o `desconhecido` vezes os cinco resultados do consumidor) e a senha de demonstração recusada com `ENVIRONMENT=production`) |
| `Security` (`.github/workflows/security.yml`) | `pip-audit`, `gitleaks`, `trivy` | CVE nas dependências de runtime, segredos em todo o histórico do commit testado (um segredo commitado e apagado no commit seguinte também reprova; a regra de URI de banco e de broker com senha tem teste negativo, `scripts/ci/testa-gitleaks.sh`, que reprova a senha gerada num manifesto) e CVE HIGH/CRITICAL com correção na imagem (as imagens de terceiros dos manifestos entram só como relatório no summary); o resultado do `pip-audit` e o do `trivy` da imagem vão também para o summary do job (`scripts/ci/registra-no-summary.sh`, que mantém o código de saída da ferramenta); roda também toda segunda-feira |

`make check` roda localmente os mesmos gates de código do job `CI`, e `make manifests` e `make smoke`, os do job `build`.

## Dependências automatizadas

O Dependabot ([`.github/dependabot.yml`](../.github/dependabot.yml)) abre pull requests toda segunda às 06:00 (São Paulo), em três blocos:

- **uv**: `pyproject.toml` e `uv.lock`, com o prefixo `chore(deps)`.
- **Actions** dos workflows, com o prefixo `ci`, como no OS e no Billing.
- **Docker**: as imagens base do `Dockerfile`, com o prefixo `chore(deps)`.

Regras dos três:

- Patch e minor chegam agrupados num PR só por bloco; major abre PR próprio, para ser lido com o changelog.
- No máximo 3 PRs abertos por bloco.
- O PR leva os rótulos padrão do Dependabot (`dependencies` e o do ecossistema), que ele cria no repositório se faltarem.
- Cada PR passa pelos mesmos nove checks dos dois workflows, inclusive o `pip-audit`, o trivy e o gate de cobertura.

Exceções:

- **uv**: o `required-version` do `pyproject.toml` só tem piso (`>=0.11`), como no Billing. O atualizador do Dependabot roda um uv mais novo (0.12 em outubro de 2026), e com teto o `uv lock` dele recusaria o projeto. A imagem do uv no `Dockerfile` sobe pelo bloco do Docker; o piso e os Requisitos de [Como rodar](../README.md#como-rodar) sobem juntos, à mão, quando o serviço passar a exigir um uv mais novo.
- **OpenTelemetry**: sem grupo próprio. O Dependabot põe as bibliotecas `opentelemetry-*` no grupo de patch e minor do bloco uv, que ele trata como mais específico que um grupo por nome, e os patches da família sobem juntos nele: o SDK fixa `opentelemetry-api` e `opentelemetry-semantic-conventions` na mesma versão, e subir só uma parte não resolve. A minor e a major ficam atrás do teto, abaixo.
- **Teto do OpenTelemetry**: o SDK e o exportador têm teto `<1.46` no `pyproject.toml`, e as instrumentações `<0.67`, como no OS e no Billing. O teto barra também a próxima minor, porque o `mensageria/telemetria.py` troca `_status` e `_events` de um span, atributos privados do SDK que uma minor nova pode mudar. Uma minor nova só entra com o teto subido, depois de ler o changelog.
- **Python**: no bloco do Docker, major e minor ficam de fora. A versão sobe à mão, de uma vez, em `.python-version`, no `python-version` do `security.yml`, no `requires-python` e no `python_version` (mypy) do `pyproject.toml`, no `sonar.python.version` do `sonar-project.properties` e nas duas imagens do `Dockerfile`. Sem isso, o 3.14 → 3.15 chegaria como minor e trocaria só a imagem do runtime. O patch não passa pelo Dependabot: vem do rebuild da tag móvel `python:3.14-slim-trixie`.

Depois do congelamento do código em 06/11/2026, o `open-pull-requests-limit` de cada bloco passa de 3 para 0 (e o valor esperado em `tests/unitarios/test_scripts_ci.py` com ele). O 0 desliga só as atualizações de versão: os PRs de segurança do Dependabot, habilitados no repositório, não entram no limite e seguem abrindo. Sobem à mão, porque o Dependabot não os enxerga ou a versão está fixada de propósito: `postgres` e `prometheuscommunity/postgres-exporter` (a cada release do serviço, junto da tabela do `platform`, seção [Imagens de terceiros](seguranca.md#imagens-de-terceiros)), o PostgreSQL e o RabbitMQ do compose e dos testes de integração, o kubeconform, o trivy, o gitleaks, o pip-audit, o SonarQube, o scanner dele (`scripts/sonar/analisar.sh`) e o diff-cover.

## CD

O [caller `cd.yml`](../.github/workflows/cd.yml) conserva gatilhos, concorrência e os nove checks locais de CI e segurança. Depois dos dois gates verdes, o job `cd` chama o [workflow reutilizável do Platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/.github/workflows/cd-servico.yml), fixado em `3f7cca2eb555a038b0d2780058ef0e692f93ee8a` (S), com `servico: execution-service` e quatro inputs opcionais: `sha_platform`, `sha_os_service`, `sha_billing_service` e `sha_execution_service`. S escolhe a definição do workflow; `sha_platform` escolhe separadamente os scripts e manifests do deploy (P). O próprio serviço é sempre o commit do run, e seu input só aceita vazio ou esse SHA.

O push na `main` e o dispatch iniciam o CD; fora da `main`, o reutilizável reprova antes de publicar imagem. Só há um run por repositório em curso, sem cancelá-lo; o GitHub mantém um único pendente, que o disparo seguinte pode substituir. O caller concede `contents: read`, `actions: read` e `packages: write`, sem herdar segredos; os jobs chamados reduzem essas permissões. A imagem, o kind e o gancho remoto são testados uma única vez no [teste compartilhado do Platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/tests/test_cd_servico_workflow.py).

No reutilizável, `image` publica a imagem com tag do commit, SBOM SPDX e proveniência mínima sem payload do evento, confere o digest e salva o tar `linux/amd64`. `deploy-kind` sobe a plataforma e os três serviços num kind efêmero do runner, implanta esta imagem pelo tar, executa smoke e E2E, varre os relatórios e só então os publica. Os vizinhos e P usam o último CD verde de push/main; ausência de run verde ou workflow 404 usa a main com aviso, e demais erros da API reprovam. O artefato `imagem` dura 1 dia; `sbom-execution-service` e `e2e-kind` (somente `platform/reports/e2e/`, após varredura) duram 90 dias. O cluster some ao terminar o job.

A prova deste caller na `main` inclui imagem, SBOM, digest, kind, smoke, E2E, varredura e `remote-skipped`; o [histórico do CD](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/workflows/cd.yml) registra as execuções e seus resultados. O deploy remoto permanece desligado por `DEPLOY_REMOTO_HABILITADO`, sem provedor, `environment`, `id-token` ou `secrets: inherit` no caller. Uma habilitação futura exige alterar deliberadamente o reutilizável e seu teste, seguindo [Nuvem opcional](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/docs/operacao/nuvem-opcional.md), e atualizar S; não cabe adicionar steps ao job que usa `uses`. O script remoto executa a partir do checkout do serviço, implanta o overlay por digest e exige a plataforma previamente implantada; o smoke ocorre no kind do runner.

Antes de publicar: `uv run pytest tests/unitarios/test_cd.py`, `make check`, actionlint com shellcheck nos workflows e `make smoke`. O contrato local exige caminho literal e SHA completo minúsculo, serviço/inputs, needs, permissões, gatilhos, concorrência e os nove checks. O Dependabot `github-actions` já cobre a raiz e o pin reutilizável; até haver prova de um bump reconhecido, a atualização de S é supervisionada. Para reexecutar pela main com a resolução padrão dos commits:

```bash
gh workflow run cd.yml --ref main
```
