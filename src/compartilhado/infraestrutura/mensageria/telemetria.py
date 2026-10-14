"""OpenTelemetry dos tres processos: contexto W3C, amostrador e provider (ADR-043).

A API, o relay e o consumidor instalam o ``TracerProvider`` do SDK sempre, para o
contexto seguir de mensagem em mensagem; a exportacao OTLP so liga com
``OTEL_ENABLED=true`` (endpoint em ``OTEL_EXPORTER_OTLP_ENDPOINT``) e e a unica
que carrega o gRPC (``criar_provedor``).

Dois cuidados valem para todo span do provider, venha do codigo do servico ou do
instrumentador de FastAPI, SQLAlchemy e httpx:

- o amostrador descarta o span de cliente sem pai (``AmostradorDeRaiz``):
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
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.textmap import CarrierT, Getter, default_getter
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
    from opentelemetry.sdk.trace import Span as SpanDoSdk
    from opentelemetry.trace import Link, Span, TraceState
    from opentelemetry.util.types import Attributes

_TRACESTATE_MAXIMO: Final = 512
_TRACESTATE: Final = "tracestate"
# Motivo da falha de um span: texto fixo (a classe da excecao, o codigo do broker),
# nunca o texto da excecao.
ATRIBUTO_FALHA: Final = "pytstop.falha"
_VERDADEIROS: Final = frozenset({"true", "1"})
# Mesmos padroes do compose da plataforma (Jaeger por OTLP/gRPC).
_ENDPOINT_PADRAO: Final = "http://jaeger:4317"
_SERVICO_PADRAO: Final = "execution-service"


class _SemTracestateLongo(Getter[CarrierT]):
    """Esconde do propagador o ``tracestate`` acima do teto do W3C (512 caracteres)."""

    def __init__(self, getter: Getter[CarrierT]) -> None:
        self._getter = getter

    def get(self, carrier: CarrierT, key: str) -> list[str] | None:
        valores = self._getter.get(carrier, key)
        if (
            key == _TRACESTATE
            and valores
            and len(",".join(valores)) > _TRACESTATE_MAXIMO
        ):
            return None
        return valores

    def keys(self, carrier: CarrierT) -> list[str]:
        return self._getter.keys(carrier)


class _TraceContext(TraceContextTextMapPropagator):
    """``traceparent`` e ``tracestate``, o que o ADR-043 propaga: sem ``baggage``.

    O ``tracestate`` acima de 512 caracteres e descartado na leitura, venha do
    cabecalho HTTP (o instrumentador do FastAPI le o cabecalho sem passar por
    ``contexto_de``) ou dos headers AMQP e das colunas da outbox: o valor vem de
    fora e seria regravado na outbox, em cada copia de retry e na chamada ao
    Billing. O ``traceparent`` segue valendo.
    """

    def extract(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        return super().extract(carrier, context, _SemTracestateLongo(getter))


_PROPAGADOR: Final = _TraceContext()

# ProxyTracer: vale o provider instalado depois (configurar_telemetria ou testes).
tracer = trace.get_tracer("pytstop.mensageria")


def contexto_atual() -> dict[str, str]:
    """``traceparent``/``tracestate`` do span corrente; vazio fora de span."""
    portador: dict[str, str] = {}
    _PROPAGADOR.inject(portador)
    return portador


def contexto_de(portador: Mapping[str, object]) -> Context:
    """Contexto W3C lido de headers AMQP ou das colunas da outbox."""
    textos = {
        chave: valor for chave, valor in portador.items() if isinstance(valor, str)
    }
    return _PROPAGADOR.extract(textos)


class AmostradorDeRaiz(Sampler):
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
        return "AmostradorDeRaiz{descarta span de cliente sem pai}"


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

    Recebe o processador que exporta e o chama so depois de limpar o span, entao
    a ordem nao depende de quem monta o provider: nenhum exportador alcanca o
    span antes da limpeza. O ``ReadableSpan`` nao tem setter, entao o processador
    troca os atributos protegidos ``_status`` e ``_events`` da copia que todos os
    processadores recebem (o span vivo ja terminou); os testes de cada regra
    reprovam se o SDK mudar isso.
    """

    def __init__(self, exportador: SpanProcessor) -> None:
        self._exportador = exportador

    def on_start(self, span: SpanDoSdk, parent_context: Context | None = None) -> None:
        self._exportador.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if span.status.status_code is StatusCode.ERROR:
            span._status = Status(StatusCode.ERROR)
        span._events = tuple(_sem_texto_de_excecao(e) for e in span.events)
        self._exportador.on_end(span)

    def shutdown(self) -> None:
        self._exportador.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exportador.force_flush(timeout_millis)


def marcar_falha(span: Span, falha: str) -> None:
    """Span com status de erro e o motivo em ``pytstop.falha``.

    O motivo e texto fixo, a classe da excecao ou o codigo do broker; o status vai
    sem descricao (o ``_SemTextoDeErro`` tiraria o texto de qualquer jeito).
    """
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute(ATRIBUTO_FALHA, falha)


def criar_provedor(ambiente: Mapping[str, str], *, processo: str) -> TracerProvider:
    """``TracerProvider`` do processo; com ``OTEL_ENABLED``, exporta por OTLP.

    ``pytstop.processo`` (``api``, ``relay`` ou ``consumidor``) vai no
    ``Resource``: os spans dos tres processos saem com o mesmo ``service.name``.
    O exportador entra por dentro do ``_SemTextoDeErro``, nunca ao lado dele.
    """
    provedor = TracerProvider(
        sampler=ParentBased(root=AmostradorDeRaiz()),
        resource=Resource.create(
            {
                "service.name": ambiente.get("OTEL_SERVICE_NAME", _SERVICO_PADRAO),
                "service.version": ambiente.get("PYTSTOP_GIT_SHA", "unknown")[:12],
                "pytstop.processo": processo,
            }
        ),
    )
    # Sem exportacao, o SpanProcessor base (que nao faz nada) fecha a cadeia.
    exportador: SpanProcessor = SpanProcessor()
    if ambiente.get("OTEL_ENABLED", "").strip().lower() in _VERDADEIROS:
        # So aqui o gRPC entra no processo.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = ambiente.get("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT_PADRAO)
        exportador = BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://"))
        )
    provedor.add_span_processor(_SemTextoDeErro(exportador))
    return provedor


def instalar_propagador() -> None:
    """So ``traceparent`` e ``tracestate`` entram e saem do processo.

    O padrao do SDK soma o ``baggage``, que a API aceitaria do chamador e o httpx
    repassaria ao Billing; o ``tracestate`` longo cai na leitura (``_TraceContext``).
    """
    set_global_textmap(_PROPAGADOR)


def configurar_telemetria(processo: str) -> TracerProvider:
    """Instala o provider e o propagador do processo; devolve o provider.

    Quem instala o provider o encerra. O ``shutdown`` descarrega os spans que
    esperam no lote. O SDK o registra em ``atexit``, que nao roda se o processo
    morrer por sinal: o uvicorn re-levanta o SIGTERM depois do desligamento
    gracioso, e fora do PID 1 (o container usa ``exec``) isso mata o processo.
    Por isso a API o chama no fim do lifespan.
    """
    provedor = criar_provedor(os.environ, processo=processo)
    trace.set_tracer_provider(provedor)
    instalar_propagador()
    return provedor
