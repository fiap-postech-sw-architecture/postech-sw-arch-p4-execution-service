from __future__ import annotations

import os
from collections.abc import AsyncGenerator  # noqa: TC003 - lifespan anotado em runtime
from contextlib import asynccontextmanager
from importlib.metadata import version

import httpx
import structlog
from fastapi import FastAPI

from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker
from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)
from src.compartilhado.infraestrutura.jwks import ValidadorDeTokenJWKS
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.metrics import configurar_metricas
from src.compartilhado.interfaces.error_handler import registrar_error_handlers
from src.compartilhado.interfaces.middleware import SecurityHeadersMiddleware
from src.compartilhado.interfaces.router_saude import router as router_saude
from src.diagnostico.interfaces.router import router as router_diagnosticos
from src.estoque.interfaces.router import router as router_estoque
from src.execucao.interfaces.router import router as router_execucao

_log = structlog.get_logger(__name__)

# Timeout da unica chamada sincrona entre servicos (Execucao -> Billing).
_TIMEOUT_BILLING_S = 2.0

_DESCRICAO = (
    "Execucao e Producao do PytStop (fase 4): fila de diagnostico, fila de "
    "execucao e estoque de pecas (reserva, liberacao e baixa). Participante da "
    "saga orquestrada pelo OS Service; os eventos da saga saem pela outbox."
)


def _variavel_obrigatoria(nome: str) -> str:
    valor = os.environ.get(nome, "").strip()
    if not valor:
        msg = f"Variavel de ambiente obrigatoria nao definida: {nome}"
        raise RuntimeError(msg)
    return valor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Cria engine, validador de token e cliente do Billing; descarta no shutdown."""
    configurar_logging()
    engine = criar_engine(_variavel_obrigatoria("DATABASE_URL"))
    app.state.session_factory = criar_session_factory(engine)
    app.state.validador_token = ValidadorDeTokenJWKS(_variavel_obrigatoria("JWKS_URL"))
    app.state.billing_client = httpx.Client(
        base_url=_variavel_obrigatoria("BILLING_URL"), timeout=_TIMEOUT_BILLING_S
    )
    app.state.billing_breaker = CircuitBreaker("billing")
    _log.info("execution_service_started")
    try:
        yield
    finally:
        app.state.billing_client.close()
        engine.dispose()


def criar_app() -> FastAPI:
    application = FastAPI(
        title="PytStop Execution Service",
        version=version("pytstop-execution-service"),
        description=_DESCRICAO,
        lifespan=lifespan,
    )
    application.include_router(router_saude)
    application.include_router(router_estoque)
    application.include_router(router_diagnosticos)
    application.include_router(router_execucao)
    application.add_middleware(SecurityHeadersMiddleware)
    registrar_error_handlers(application)
    # Por ultimo: o middleware de metricas fica por fora e mede a request inteira.
    configurar_metricas(application)
    return application


app = criar_app()
