"""OpenTelemetry da mensageria: contexto W3C na outbox e nos headers AMQP (ADR-043).

O relay e o consumidor instalam o ``TracerProvider`` do SDK sempre, para o
contexto seguir de mensagem em mensagem; a exportacao OTLP so liga com
``OTEL_ENABLED=true`` (endpoint em ``OTEL_EXPORTER_OTLP_ENDPOINT``). A API so le
e grava o contexto (pacote da API do OpenTelemetry): o SDK e o gRPC so carregam
em ``criar_provedor``, chamado pelos dois processos.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import TracerProvider

# So traceparent/tracestate: o que o envelope do contrato carrega (sem baggage).
_PROPAGADOR: Final = TraceContextTextMapPropagator()
_TRACESTATE_MAXIMO: Final = 512
_VERDADEIROS: Final = frozenset({"true", "1"})
# Mesmos padroes do compose da plataforma (Jaeger por OTLP/gRPC).
_ENDPOINT_PADRAO: Final = "http://jaeger:4317"
_SERVICO_PADRAO: Final = "execution-service"

# ProxyTracer: vale o provider instalado depois (configurar_telemetria ou testes).
tracer = trace.get_tracer("pytstop.mensageria")


def contexto_atual() -> dict[str, str]:
    """``traceparent``/``tracestate`` do span corrente; vazio fora de span."""
    portador: dict[str, str] = {}
    _PROPAGADOR.inject(portador)
    return portador


def contexto_de(portador: Mapping[str, object]) -> Context:
    """Contexto W3C lido de headers AMQP ou das colunas da outbox.

    ``tracestate`` acima de 512 caracteres (o que o W3C manda propagar) e
    descartado: viria do publicador para a outbox e para o proximo header.
    """
    textos = {
        chave: valor
        for chave, valor in portador.items()
        if isinstance(valor, str)
        and not (chave == "tracestate" and len(valor) > _TRACESTATE_MAXIMO)
    }
    return _PROPAGADOR.extract(textos)


def criar_provedor(ambiente: Mapping[str, str], *, processo: str) -> TracerProvider:
    """``TracerProvider`` do processo; com ``OTEL_ENABLED``, exporta por OTLP/gRPC.

    ``pytstop.processo`` (``relay`` ou ``consumidor``) vai no ``Resource``: os
    spans dos dois processos saem com o mesmo ``service.name``.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provedor = TracerProvider(
        resource=Resource.create(
            {
                "service.name": ambiente.get("OTEL_SERVICE_NAME", _SERVICO_PADRAO),
                "service.version": ambiente.get("PYTSTOP_GIT_SHA", "unknown")[:12],
                "pytstop.processo": processo,
            }
        )
    )
    if ambiente.get("OTEL_ENABLED", "").strip().lower() in _VERDADEIROS:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = ambiente.get("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT_PADRAO)
        exportador = OTLPSpanExporter(
            endpoint=endpoint, insecure=endpoint.startswith("http://")
        )
        provedor.add_span_processor(BatchSpanProcessor(exportador))
    return provedor


def configurar_telemetria(processo: str) -> None:
    """Instala o provider do processo (o SDK descarrega os spans na saida)."""
    trace.set_tracer_provider(criar_provedor(os.environ, processo=processo))
