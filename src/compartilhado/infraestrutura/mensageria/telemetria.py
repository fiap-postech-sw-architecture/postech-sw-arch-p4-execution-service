"""OpenTelemetry dos tres processos: contexto W3C, amostrador e provider (ADR-043).

A API, o relay e o consumidor instalam o ``TracerProvider`` do SDK sempre, para o
contexto seguir de mensagem em mensagem; a exportacao OTLP so liga com
``OTEL_ENABLED=true`` (endpoint em ``OTEL_EXPORTER_OTLP_ENDPOINT``) e e a unica
que carrega o gRPC (``criar_provedor``).

Dois cuidados valem para todo span do provider, venha do codigo do servico ou do
instrumentador de FastAPI, SQLAlchemy e httpx:

- o amostrador descarta o span de cliente sem pai (``_RaizSemClienteOrfao``):
  o poll do relay, a raspagem do coletor e o ciclo ocioso so abrem esse tipo de
  span, e cada um virava um trace raiz;
- nenhum span leva a mensagem de uma excecao nem a descricao do status de erro
  (``_SemTextoDeErro``): o erro do driver traz o ``DETAIL`` do PostgreSQL com a
  linha inteira (placa, texto livre). O motivo de uma falha vai em atributo
  fixo do span, nao no texto do erro.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.semconv.attributes.exception_attributes import (
    EXCEPTION_MESSAGE,
    EXCEPTION_STACKTRACE,
)
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from opentelemetry.context import Context
    from opentelemetry.trace import Link, TraceState
    from opentelemetry.util.types import Attributes

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


class _RaizSemClienteOrfao(Sampler):
    """Raiz do ``ParentBased``: o span de cliente sem pai nao vira trace.

    So chega aqui o span sem pai. O de cliente (uma consulta do SQLAlchemy, uma
    chamada do httpx) sem span em volta e o poll do relay, a raspagem do coletor
    ou um ciclo ocioso: descartado. O de cliente com pai (a consulta de uma
    requisicao ou de uma mensagem) nao passa por aqui e segue a decisao do pai.
    Qualquer outro tipo de span sem pai (a requisicao, a publicacao, o consumo)
    abre o trace.
    """

    def should_sample(  # noqa: PLR0913 - assinatura do Sampler do SDK
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if kind is SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return ALWAYS_ON.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return "RaizSemClienteOrfao"


def _sem_texto_de_excecao(evento: Event) -> Event:
    if evento.name != "exception":
        return evento
    atributos = {
        chave: valor
        for chave, valor in (evento.attributes or {}).items()
        if chave not in {EXCEPTION_MESSAGE, EXCEPTION_STACKTRACE}
    }
    return Event(evento.name, atributos, evento.timestamp)


class _SemTextoDeErro(SpanProcessor):
    """Tira do span o texto de erro, que pode trazer dado do pedido.

    A mensagem e o stack trace saem de todo evento ``exception`` (fica o tipo), e
    o status de erro perde a descricao: o instrumentador do SQLAlchemy a preenche
    com o texto do driver, o do FastAPI e o ``with`` do SDK, com o da excecao.

    Primeiro processador do provider: roda antes de qualquer exportador, sobre a
    copia do span que todos recebem. O ``ReadableSpan`` nao tem setter, entao o
    processador troca os atributos protegidos ``_status`` e ``_events`` da copia
    (nao os do span vivo, que ja terminou); os testes de cada regra reprovam se o
    SDK mudar isso.
    """

    def on_end(self, span: ReadableSpan) -> None:
        if span.status.status_code is StatusCode.ERROR:
            span._status = Status(StatusCode.ERROR)
        span._events = tuple(_sem_texto_de_excecao(e) for e in span.events)


def criar_provedor(ambiente: Mapping[str, str], *, processo: str) -> TracerProvider:
    """``TracerProvider`` do processo; com ``OTEL_ENABLED``, exporta por OTLP.

    ``pytstop.processo`` (``api``, ``relay`` ou ``consumidor``) vai no
    ``Resource``: os spans dos tres processos saem com o mesmo ``service.name``.
    """
    provedor = TracerProvider(
        sampler=ParentBased(root=_RaizSemClienteOrfao()),
        resource=Resource.create(
            {
                "service.name": ambiente.get("OTEL_SERVICE_NAME", _SERVICO_PADRAO),
                "service.version": ambiente.get("PYTSTOP_GIT_SHA", "unknown")[:12],
                "pytstop.processo": processo,
            }
        ),
    )
    provedor.add_span_processor(_SemTextoDeErro())
    if ambiente.get("OTEL_ENABLED", "").strip().lower() in _VERDADEIROS:
        # So aqui o gRPC entra no processo.
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
