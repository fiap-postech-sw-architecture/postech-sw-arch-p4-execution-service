"""Instrumentacao automatica da API: FastAPI, SQLAlchemy e httpx (ADR-043).

Os tres recebem o provider que o processo instalou (``configurar_telemetria``:
amostrador e limpeza do texto de erro em ``mensageria/telemetria.py``). O
SQLAlchemy cobre o psycopg2: instrumentar o driver a parte duplicaria cada span
de banco. O cliente AMQP nao tem instrumentador: o relay e o consumidor abrem os
spans de publicacao e de consumo e levam o contexto no envelope.

O que a API nao exporta: ``/metrics`` e as sondas (``/api/v1/saude`` e
``/api/v1/saude/pronto``), os spans de ``receive`` e ``send`` do ASGI, o corpo
das requisicoes e os parametros dos comandos SQL, o caminho cru da requisicao e
o que o instrumentador grava de quem chamou (``redigir_url_da_span``).
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

# A instrumentacao testa estas regex contra `esquema://<Host><caminho>`, sem a
# query, e o Host vem do chamador: uma regex solta (`/api/v1/saude`) casaria com
# `Host: x/api/v1/saude` e tiraria do trace a requisicao inteira. Ancorada, o `$`
# e o fim do caminho de verdade: o Host nao tem `/`, `?` nem `#`, so segmentos de
# caminho (o prefixo do ROOT_PATH, `/execucao` na borda) podem vir antes da rota.
# /metrics e as sondas disparam a cada poucos segundos e nao contam historia.
_INICIO_DA_URL: Final = r"^https?://[^/?#]+(?:/[^/?#]+)*"
_URLS_SEM_TRACE: Final = (
    rf"{_INICIO_DA_URL}/metrics$,{_INICIO_DA_URL}/api/v1/saude(?:/pronto)?$"
)
# Os atributos que levam o caminho cru nas duas convencoes semanticas do
# OpenTelemetry (a antiga, que e a padrao, e a nova).
_ATRIBUTOS_DE_URL: Final = ("url.path", "http.target", "http.url", "url.full")
_REDIGIDO: Final = "REDACTED"
# O que o instrumentador grava de quem chamou, sem uso operacional, nas duas
# convencoes: o User-Agent (texto livre sem limite), o par da conexao (no cluster,
# o Kong ou o Prometheus) e o Host, que o chamador manda como quiser. O Jaeger
# guarda por contagem, sem como apagar um titular.
_TEXTOS_DO_CHAMADOR: Final = (
    "http.user_agent",
    "user_agent.original",
    "net.peer.ip",
    "client.address",
    "http.server_name",
    "http.host",
    "server.address",
)
_PORTAS: Final = ("net.peer.port", "client.port", "net.host.port", "server.port")


def redigir_url_da_span(span: Span, scope: Mapping[str, Any]) -> None:
    """``server_request_hook`` do FastAPI: o span guarda o template da rota.

    O caminho cru traz o que identifica a requisicao (``/execucoes/<ordem_id>/...``,
    ``/estoque/<sku>``), e uma query pode trazer filtro do cliente. O hook roda
    com o span ja criado e os atributos da instrumentacao gravados, e os troca
    pelo template da rota (``/api/v1/execucoes/{ordem_id}/inicio``, o
    ``http.route`` que o proprio instrumentador resolveu); requisicao sem rota
    casada leva ``nao_roteada``, como a metrica HTTP. ``url.query`` vira
    ``REDACTED`` quando ha query. O que o instrumentador grava do chamador
    (User-Agent, par da conexao, Host) vira ``REDACTED`` e as portas, ``0``, nos
    atributos que o span tiver. O hook nao decompoe a URL, entao um ``Host``
    malformado nao o derruba.
    """
    if not span.is_recording():
        return
    atributos = getattr(span, "attributes", None) or {}
    rota = atributos.get("http.route", ROTA_NAO_ROTEADA)
    for atributo in _ATRIBUTOS_DE_URL:
        span.set_attribute(atributo, rota)
    if scope.get("query_string"):
        span.set_attribute("url.query", _REDIGIDO)
    for atributo in _TEXTOS_DO_CHAMADOR:
        if atributo in atributos:
            span.set_attribute(atributo, _REDIGIDO)
    for atributo in _PORTAS:
        if atributo in atributos:
            span.set_attribute(atributo, 0)


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
    O instrumentador grava so o texto do comando, com os marcadores de parametro:
    os valores nunca entram no span, porque ele nao le os parametros. O
    ``hide_parameters`` do engine cuida da mensagem de erro e do log do
    SQLAlchemy, nao do span, e o texto do erro do driver sai pelo
    ``_SemTextoDeErro``.
    """
    SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provedor)


def instrumentar_cliente_http(cliente: httpx.Client, provedor: TracerProvider) -> None:
    """Um span CLIENT por chamada do cliente, com o ``traceparent`` na requisicao."""
    HTTPXClientInstrumentor.instrument_client(cliente, tracer_provider=provedor)
