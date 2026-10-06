#!/bin/bash
set -euo pipefail

echo ">>> execution-service | commit ${PYTSTOP_GIT_SHA:0:12} | ${PYTSTOP_GIT_DATE:-unknown}"

# Ligados no compose (container unico). No Kubernetes a migracao roda num Job
# antes do rollout e estes passos ficam desligados.
if [ "${RUN_MIGRATIONS_ON_STARTUP:-false}" = "true" ]; then
  echo "Running database migrations..."
  alembic upgrade head
fi

if [ "${RUN_SEED_ON_STARTUP:-false}" = "true" ]; then
  echo "Running demo stock seed..."
  python -m src.estoque.infraestrutura.seed
fi

# --no-proxy-headers: o X-Forwarded-For so vale atras de proxy configurado
# explicitamente (o uvicorn confiaria no XFF de peers loopback por padrao).
# --no-access-log: o access log estruturado (com request_id) sai do
# SecurityHeadersMiddleware; o do uvicorn e uma linha de texto sem campos.
exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers --no-access-log
