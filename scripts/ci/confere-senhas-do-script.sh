#!/usr/bin/env bash
# A excecao do trivy (KSV-0109, k8s/trivy-ignore.rego) vale para o ConfigMap do
# papeis.sql inteiro, e o gitleaks nao enxerga um PASSWORD 'literal' no SQL. Este
# script estreita a excecao ao que ela precisa: todo PASSWORD do script recebe
# uma variavel do psql (PASSWORD :'var'), e a variavel vem de \getenv, o
# ambiente do container do banco. Literal reprova, na mesma linha ou na de
# baixo, em minuscula tambem, e a variavel dada por \set ou \gset tambem.
# Comentario (--) fica de fora da conferencia. Roda no make manifests, na raiz
# do repositorio.
set -euo pipefail

arquivo="${1:-k8s/base/papeis.sql}"
shopt -s nocasematch

# Sem os comentarios: o PASSWORD de um comentario nao e comando.
sql=$(sed -E 's/--.*$//' "$arquivo")
# Cada PASSWORD (palavra inteira, nao POSTGRES_APP_PASSWORD) ate o ';', em
# qualquer quebra de linha.
ocorrencias=$(printf '%s\n' "$sql" | tr '\n' ' ' | grep -oiE '(^|[^A-Za-z0-9_])password[^;]*' \
  | sed -E 's/^[^A-Za-z]+//' || true)
if [ -z "$ocorrencias" ]; then
  echo "$arquivo: nenhum PASSWORD encontrado" >&2
  exit 1
fi

variavel_psql="^password[[:space:]]+:'([A-Za-z_][A-Za-z0-9_]*)'([[:space:]]|\$)"
ruins=()
while IFS= read -r ocorrencia; do
  if [[ $ocorrencia =~ $variavel_psql ]]; then
    variavel="${BASH_REMATCH[1]}"
    # \gset (no fim de uma consulta, com ou sem prefixo) grava variavel a partir
    # de qualquer coluna: nenhum \gset no script.
    if ! grep -qE "^[[:space:]]*\\\\getenv[[:space:]]+${variavel}[[:space:]]+[A-Z_]+[[:space:]]*\$" <<< "$sql" \
      || grep -qE "^[[:space:]]*\\\\set[[:space:]]+${variavel}([[:space:]]|\$)" <<< "$sql" \
      || grep -qE '\\gset([[:space:]]|$)' <<< "$sql"; then
      ruins+=("${ocorrencia} (a variavel nao vem so de \\getenv)")
    fi
  else
    ruins+=("$ocorrencia")
  fi
done <<< "$ocorrencias"

if [ "${#ruins[@]}" -gt 0 ]; then
  echo "$arquivo: PASSWORD sem a variavel do psql lida por \\getenv:" >&2
  printf '  %s\n' "${ruins[@]}" >&2
  exit 1
fi
echo "$arquivo: todo PASSWORD vem de variavel do psql lida por \\getenv"
