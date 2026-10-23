# Dependências automatizadas

Detalhe de [CI/CD](../README.md#cicd) do README.

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
- **FastAPI**: minor e major ficam de fora (`ignore` no bloco uv, que o teste do arquivo confere) e sobem à mão; o patch sobe pelo grupo. O FastAPI é 0.x e traz mudança incompatível em minor: oito das treze minors entre a 0.144 e a 0.156 têm Breaking Changes nas notas de release (a 0.151, por exemplo, passou a distinguir `null` de campo omitido no corpo JSON). A subida fica segurada até a entrega da fase 4 (17/11/2026), com os três serviços na mesma versão, e é reavaliada depois. O pydantic não entra no `ignore`: a minor dele segue pelo grupo e pelo CI.
- **Python**: no bloco do Docker, major e minor ficam de fora. A versão sobe à mão, de uma vez, em `.python-version`, no `python-version` do `security.yml`, no `requires-python` e no `python_version` (mypy) do `pyproject.toml`, no `sonar.python.version` do `sonar-project.properties` e nas duas imagens do `Dockerfile`. Sem isso, o 3.14 → 3.15 chegaria como minor e trocaria só a imagem do runtime. O patch não passa pelo Dependabot: vem do rebuild da tag móvel `python:3.14-slim-trixie`.

Depois do congelamento do código em 06/11/2026, o `open-pull-requests-limit` de cada bloco passa de 3 para 0 (e o valor esperado em `tests/unitarios/test_scripts_ci.py` com ele). O 0 desliga só as atualizações de versão: os PRs de segurança do Dependabot, habilitados no repositório, não entram no limite e seguem abrindo. Sobem à mão, porque o Dependabot não os enxerga ou a versão está fixada de propósito: `postgres` e `prometheuscommunity/postgres-exporter` (a cada release do serviço, junto da tabela do `platform`, seção [Imagens de terceiros](seguranca.md#imagens-de-terceiros)), o PostgreSQL e o RabbitMQ do compose e dos testes de integração, o kubeconform, o trivy, o gitleaks, o pip-audit, o SonarQube, o scanner dele (`scripts/sonar/analisar.sh`) e o diff-cover.
