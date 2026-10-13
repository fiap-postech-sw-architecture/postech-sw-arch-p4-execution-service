#!/usr/bin/env bash
# Teste negativo das regras do .gitleaks.toml (job gitleaks do CI; roda tambem
# na maquina local, com o gitleaks do PATH): a URI do PostgreSQL (postgresql:// e
# postgres://) e a URL do AMQP (amqp:// e amqps://) com a senha que o make deploy
# gera reprovam num manifesto, a forma dos manifestos ($(POSTGRES_APP_PASSWORD))
# passa, a senha de demonstracao so passa onde a allowlist diz (o compose e o
# .env.example, ou com o marcador gitleaks:allow na linha), e senha apagada no
# commit seguinte ainda reprova no historico. Sem ele, uma regra que nao casa
# mais nada, ou uma allowlist larga, passaria verde.
set -euo pipefail

raiz=$(cd "$(dirname "$0")/../.." && pwd)
dir=$(mktemp -d "${TMPDIR:-/tmp}/testa-gitleaks.XXXXXX")
trap 'rm -rf "$dir"' EXIT
mkdir -p "$dir/k8s" "$dir/gerada"

# pg <senha> [esquema] e amqp <senha> [esquema]: as linhas que os manifestos montam.
pg() { printf 'value: %s://execucao:%s@execucao-postgres.pytstop-execucao.svc.cluster.local:5432/execucao\n' "${2:-postgresql}" "$1"; }
amqp() { printf 'RABBITMQ_URL: "%s://execucao:%s@rabbitmq.pytstop-plataforma.svc.cluster.local:5672/%%2F"\n' "${2:-amqp}" "$1"; }
pg "$(openssl rand -hex 24)" > "$dir/k8s/pg-senha-gerada.yaml"
pg "$(openssl rand -hex 24)" postgres > "$dir/k8s/pg-esquema-postgres.yaml"
amqp "$(openssl rand -hex 24)" > "$dir/k8s/amqp-senha-gerada.yaml"
amqp "$(openssl rand -hex 24)" amqps > "$dir/k8s/amqps-senha-gerada.yaml"
# A allowlist do compose e so da senha de demonstracao: a gerada reprova ali tambem.
pg "$(openssl rand -hex 24)" > "$dir/gerada/docker-compose.yml"
pg "$(openssl rand -hex 24)" > "$dir/gerada/.env.example"
# A senha de demonstracao nao vale num manifesto.
pg execucao-demo > "$dir/k8s/pg-senha-de-demonstracao.yaml"
amqp pytstop-execucao-demo-2026 > "$dir/k8s/amqp-senha-de-demonstracao.yaml"
# shellcheck disable=SC2016 # o $(...) e literal: a expansao e do kubelet
pg '$(POSTGRES_APP_PASSWORD)' > "$dir/k8s/pg-expansao-do-pod.yaml"
{ pg execucao-demo; amqp pytstop-execucao-demo-2026; } > "$dir/docker-compose.yml"
{ pg execucao-demo; amqp pytstop-execucao-demo-2026; } > "$dir/.env.example"
{ pg execucao-demo | sed 's/$/  # gitleaks:allow/'; } > "$dir/teste-com-marcador.yml"

(cd "$dir" && gitleaks dir . --config "$raiz/.gitleaks.toml" --no-banner --redact --exit-code 0 \
  --report-format json --report-path "$dir/achados.json")
achados=$(jq -r '[.[] | .RuleID + " " + .File] | sort | join(", ")' "$dir/achados.json")
esperado="pytstop-url-amqp-com-senha k8s/amqp-senha-de-demonstracao.yaml, pytstop-url-amqp-com-senha k8s/amqp-senha-gerada.yaml, pytstop-url-amqp-com-senha k8s/amqps-senha-gerada.yaml, pytstop-url-postgresql-com-senha gerada/.env.example, pytstop-url-postgresql-com-senha gerada/docker-compose.yml, pytstop-url-postgresql-com-senha k8s/pg-esquema-postgres.yaml, pytstop-url-postgresql-com-senha k8s/pg-senha-de-demonstracao.yaml, pytstop-url-postgresql-com-senha k8s/pg-senha-gerada.yaml"
if [ "$achados" != "$esperado" ]; then
  echo "gitleaks found [$achados], expected [$esperado]" >&2
  exit 1
fi
# Historico: senha commitada e apagada no commit seguinte continua reprovando
# com o mesmo escopo do CI (--log-opts="HEAD").
repo="$dir/historico"
git init -q "$repo"
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q --allow-empty -m inicio
mkdir -p "$repo/k8s"
pg "$(openssl rand -hex 24)" > "$repo/k8s/vazou.yaml"
git -C "$repo" add k8s/vazou.yaml
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q -m vaza
git -C "$repo" rm -q k8s/vazou.yaml
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q -m apaga
if gitleaks git "$repo" --log-opts="HEAD" --config "$raiz/.gitleaks.toml" --no-banner --redact >/dev/null 2>&1; then
  echo "gitleaks passed a password committed and deleted in the next commit" >&2
  exit 1
fi
echo "gitleaks rules hold: generated passwords flagged (postgresql, postgres, amqp, amqps; in k8s/ and in a compose), demo passwords flagged in k8s/ and allowed only in the compose, .env.example and with the inline marker, the pod expansion allowed, deleted password still flagged in history"
