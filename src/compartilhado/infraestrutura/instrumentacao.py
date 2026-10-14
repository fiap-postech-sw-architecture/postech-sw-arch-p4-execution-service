"""Instrumentacao automatica da API: FastAPI, SQLAlchemy e httpx (ADR-043).

Os tres usam o provider global que ``configurar_telemetria`` instala (amostrador
e limpeza do texto de erro em ``mensageria/telemetria.py``). O SQLAlchemy cobre o
psycopg2: instrumentar o driver a parte duplicaria cada span de banco. O cliente
AMQP nao tem instrumentador: o relay e o consumidor abrem os spans de publicacao
e de consumo e levam o contexto no envelope.

O que a API nao exporta: ``/metrics`` e as sondas (``/api/v1/saude`` e
``/api/v1/saude/pronto``), os spans de ``receive`` e ``send`` do ASGI, o corpo
das requisicoes e os parametros dos comandos SQL, e o caminho cru da requisicao
(``redigir_url_da_span``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

from src.compartilhado.infraestrutura.metrics import ROTA_NAO_ROTEADA

if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Span
    from sqlalchemy import Engine

# Regex sobre a URL da requisicao (sem a query). /metrics e as sondas disparam a
# cada poucos segundos e nao contam nenhuma historia; /api/v1/saude cobre /pronto.
_URLS_SEM_TRACE: Final = "/metrics$,/api/v1/saude"
# Os atributos que levam o caminho cru nas duas convencoes semanticas do
# OpenTelemetry (a antiga, que e a padrao, e a nova).
_ATRIBUTOS_DE_URL: Final = ("url.path", "http.target", "http.url", "url.full")
_QUERY_REDIGIDA: Final = "REDACTED"


def redigir_url_da_span(span: Span, scope: Mapping[str, Any]) -> None:
    """``server_request_hook`` do FastAPI: o span guarda o template da rota.

    O caminho cru traz o que identifica a requisicao (``/execucoes/<ordem_id>/...``,
    ``/estoque/<sku>``), e uma query pode trazer filtro do cliente. O hook roda
    com o span ja criado e os atributos da instrumentacao gravados, e os troca
    pelo template da rota (``/api/v1/execucoes/{ordem_id}/inicio``, o
    ``http.route`` que o proprio instrumentador resolveu); requisicao sem rota
    casada leva ``nao_roteada``, como a metrica HTTP. ``url.query`` vira
    ``REDACTED`` quando ha query.
    """
    if not span.is_recording():
        return
    atributos = getattr(span, "attributes", None) or {}
    rota = atributos.get("http.route", ROTA_NAO_ROTEADA)
    for atributo in _ATRIBUTOS_DE_URL:
        span.set_attribute(atributo, rota)
    if scope.get("query_string"):
        span.set_attribute("url.query", _QUERY_REDIGIDA)


def instrumentar_api(app: FastAPI, provedor: TracerProvider) -> None:
    """Um span SERVER por requisicao, com o template da rota no lugar do caminho."""
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provedor,
        server_request_hook=redigir_url_da_span,
        excluded_urls=_URLS_SEM_TRACE,
        exclude_spans=["receive", "send"],
    )


def instrumentar_banco(engine: Engine, provedor: TracerProvider) -> None:
    """Um span CLIENT por comando SQL do engine, filho do span em andamento.

    Sem span em andamento (poll, raspagem, ciclo ocioso) o amostrador descarta.
    O span leva o texto do comando (so com os marcadores de parametro, o engine
    esconde os valores) e nunca o texto do erro do driver.
    """
    SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provedor)


def instrumentar_cliente_http(cliente: httpx.Client, provedor: TracerProvider) -> None:
    """Um span CLIENT por chamada do cliente, com o ``traceparent`` na requisicao."""
    HTTPXClientInstrumentor.instrument_client(cliente, tracer_provider=provedor)
