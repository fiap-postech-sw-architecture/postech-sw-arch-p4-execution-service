#!/usr/bin/env bash
# Relatorio de CVE das imagens de terceiros dos manifestos (o PostgreSQL e o
# exporter) no summary do job trivy do security.yml. So relatorio, nunca gate: o
# --exit-code 0 cobre os achados, mas a falha da varredura em si (limite de pull
# do Docker Hub, tag removida, banco de vulnerabilidades fora do ar) sai com 1, e
# o script a registra como aviso no summary e segue para a proxima imagem.
# Reprova so quando o render nao traz nenhuma imagem de terceiro, porque ai o
# relatorio nao prova nada. As imagens vem do render do overlay (o PostgreSQL
# leva a tag do images: da kustomization, nao do YAML). Roda na raiz do
# repositorio, com o kubectl no PATH.
set -euo pipefail

overlay="${1:-k8s/overlays/kind}"
resumo="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

imagens=$(kubectl kustomize "$overlay" \
  | sed -nE 's/^[ -]*image: *"?([^" ]+)"? *$/\1/p' \
  | grep -Ev '^pytstop-execution-service(:|$)' | sort -u || true)
if [ -z "$imagens" ]; then
  echo "$overlay: nenhuma imagem de terceiro encontrada" >&2
  exit 1
fi

for imagem in $imagens; do
  if relatorio=$(trivy image --severity HIGH,CRITICAL --ignore-unfixed --no-progress \
    --exit-code 0 "$imagem"); then
    {
      echo "### ${imagem}: HIGH e CRITICAL com correcao disponivel"
      echo '```text'
      printf '%s\n' "$relatorio"
      echo '```'
    } >> "$resumo"
  else
    status=$?
    echo "::warning::A varredura do trivy em ${imagem} falhou (codigo ${status}); veja o log do passo"
    echo "### ${imagem}: varredura falhou (codigo ${status}), sem relatorio nesta execucao" >> "$resumo"
  fi
done
