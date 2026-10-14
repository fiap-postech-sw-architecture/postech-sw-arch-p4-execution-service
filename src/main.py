from __future__ import annotations

from collections.abc import AsyncGenerator  # noqa: TC003 - lifespan anotado em runtime
from contextlib import asynccontextmanager
from importlib.metadata import version

import httpx
import structlog
from fastapi import Depends, FastAPI
from prometheus_client import REGISTRY

from src.coletor import ColetorDaApi
from src.compartilhado.infraestrutura.ambiente import (
    url_de_conexao,
    url_http_obrigatoria,
)
from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker
from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_engine_de_metricas,
    criar_session_factory,
)
from src.compartilhado.infraestrutura.instrumentacao import (
    instrumentar_api,
    instrumentar_banco,
    instrumentar_cliente_http,
)
from src.compartilhado.infraestrutura.jwks import ValidadorDeTokenJWKS
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    configurar_telemetria,
)
from src.compartilhado.infraestrutura.metrics import configurar_metricas
from src.compartilhado.interfaces.dependencies import correlacionar_pela_ordem
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Cria engine, validador, cliente do Billing e coletor; descarta no shutdown."""
    url_do_banco = url_de_conexao("DATABASE_URL")
    engine = criar_engine(url_do_banco)
    instrumentar_banco(engine)
    app.state.session_factory = criar_session_factory(engine)
    app.state.validador_token = ValidadorDeTokenJWKS(url_http_obrigatoria("JWKS_URL"))
    app.state.billing_client = httpx.Client(
        base_url=url_http_obrigatoria("BILLING_URL"), timeout=_TIMEOUT_BILLING_S
    )
    instrumentar_cliente_http(app.state.billing_client)
    app.state.billing_breaker = CircuitBreaker("billing")
    # Os gauges por consulta (outbox, execucao e estoque) saem so da API, por uma
    # conexao propria, nova a cada raspagem, que nao disputa o pool das requisicoes.
    coletor = ColetorDaApi(criar_engine_de_metricas(url_do_banco))
    REGISTRY.register(coletor)
    _log.info("execution_service_started")
    try:
        yield
    finally:
        REGISTRY.unregister(coletor)
        app.state.billing_client.close()
        engine.dispose()


def criar_app() -> FastAPI:
    # Log JSON antes da primeira linha do servidor: o uvicorn importa este modulo
    # (e monta o app) antes de "Started server process"; configurado so no
    # lifespan, as linhas de boot do uvicorn saiam em texto no meio do JSON.
    configurar_logging()
    # O provider do SDK antes do instrumentador, que o le do global: a API monta
    # o mesmo provider do relay e do consumidor, com o amostrador e a limpeza do
    # texto de erro (mensageria/telemetria.py).
    configurar_telemetria("api")
    application = FastAPI(
        title="PytStop Execution Service",
        version=version("pytstop-execution-service"),
        description=_DESCRICAO,
        lifespan=lifespan,
        dependencies=[Depends(correlacionar_pela_ordem)],
        # Sem o export automatico do FastAPI: com o endpoint OTLP no ambiente
        # ele soma exportadores http/protobuf na partida, que pedem o extra
        # fastapi[opentelemetry] (o endpoint do cluster e gRPC) e a partida
        # avisaria no log. A telemetria de cada processo e a de
        # mensageria/telemetria.py, e a da API, a de instrumentacao.py.
        telemetry={"auto_configure": False},
    )
    application.include_router(router_saude)
    application.include_router(router_estoque)
    application.include_router(router_diagnosticos)
    application.include_router(router_execucao)
    application.add_middleware(SecurityHeadersMiddleware)
    registrar_error_handlers(application)
    # Por ultimo: o middleware de metricas fica por fora e mede a request inteira.
    configurar_metricas(application)
    instrumentar_api(application)
    return application


app = criar_app()
