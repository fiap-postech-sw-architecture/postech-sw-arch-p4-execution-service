# syntax=docker/dockerfile:1.7
# Builder e runtime na MESMA distro (Debian 13, trixie) e na mesma minor do
# Python (3.14): o venv copiado carrega bytecode e wheels compilados contra a
# glibc e o Python do builder. O uv e o mesmo do `required-version` do
# pyproject (0.11), o que o CI usa.
FROM ghcr.io/astral-sh/uv:0.11-python3.14-trixie-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Manifests primeiro: a camada de dependencias so refaz quando o lock muda.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.14-slim-trixie AS runtime

ARG GIT_SHA=unknown
ARG GIT_DATE=unknown

LABEL org.opencontainers.image.title="pytstop-execution-service" \
      org.opencontainers.image.source="https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service" \
      org.opencontainers.image.description="PytStop fase 4: Execution Service (diagnostico, fila de execucao e estoque)." \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${GIT_DATE}"

# A tag movel python:3.14-slim-trixie fica semanas sem rebuild; o upgrade puxa
# os fixes do Debian que o trivy cobra (licao do p3, 09/2026).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Usuario de sistema sem shell de login; UID/GID numericos porque o
# securityContext do k8s (runAsNonRoot) so verifica UID.
RUN groupadd -r -g 1001 pytstop \
    && useradd -r -u 1001 -g pytstop -s /usr/sbin/nologin pytstop

# Sem pip no runtime: o app roda pelo venv do uv e nunca instala nada; o pip da
# base traz pacotes vendorizados que o trivy acusa sem fix possivel aqui.
RUN python -m pip uninstall -y pip

WORKDIR /app
# Codigo e venv ficam do root: o usuario 1001 le e executa, mas nao altera.
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTSTOP_GIT_SHA="${GIT_SHA}" \
    PYTSTOP_GIT_DATE="${GIT_DATE}"

# Unico healthcheck (o compose usa este): liveness, sem dependencias. A imagem
# slim nao tem curl, entao a probe e em Python. O k8s usa as proprias probes
# (liveness em /api/v1/saude, readiness em /api/v1/saude/pronto).
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/saude', timeout=2).status==200 else 1)"]

USER 1001:1001
EXPOSE 8000
CMD ["./entrypoint.sh"]
