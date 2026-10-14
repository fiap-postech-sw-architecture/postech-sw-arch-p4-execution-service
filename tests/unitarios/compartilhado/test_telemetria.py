"""Provider dos tres processos: o amostrador real e a limpeza do texto de erro.

Cada teste monta o proprio provider por ``criar_provedor`` (o mesmo da API, do
relay e do consumidor) com um exportador em memoria, sem tocar o global.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import Decision
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    StatusCode,
    TraceFlags,
)

from src.compartilhado.infraestrutura.mensageria import telemetria

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

_PLACA = "ABC1D23"


class _Provedor:
    """Provider de producao com os spans terminados em memoria."""

    def __init__(self) -> None:
        self.exportados = InMemorySpanExporter()
        self.provedor: TracerProvider = telemetria.criar_provedor({}, processo="teste")
        self.provedor.add_span_processor(SimpleSpanProcessor(self.exportados))

    def tracer(self, escopo: str = "pytstop.teste") -> Tracer:
        return self.provedor.get_tracer(escopo)

    def spans(self) -> dict[str, ReadableSpan]:
        return {s.name: s for s in self.exportados.get_finished_spans()}


@pytest.fixture
def provedor() -> _Provedor:
    return _Provedor()


def _pai_remoto(*, amostrado: bool) -> trace.Span:
    return NonRecordingSpan(
        SpanContext(
            trace_id=0x0AF7651916CD43DD8448EB211C80319C,
            span_id=0xB7AD6B7169203331,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED if amostrado else 0),
        )
    )


# --- amostrador -------------------------------------------------------------


@pytest.mark.parametrize(
    ("tipo", "decisao"),
    [
        pytest.param(SpanKind.CLIENT, Decision.DROP, id="cliente"),
        pytest.param(SpanKind.SERVER, Decision.RECORD_AND_SAMPLE, id="servidor"),
        pytest.param(SpanKind.CONSUMER, Decision.RECORD_AND_SAMPLE, id="consumo"),
        pytest.param(SpanKind.PRODUCER, Decision.RECORD_AND_SAMPLE, id="publicacao"),
        pytest.param(SpanKind.INTERNAL, Decision.RECORD_AND_SAMPLE, id="interno"),
    ],
)
def test_raiz_descarta_so_o_span_de_cliente(tipo: SpanKind, decisao: Decision) -> None:
    amostrador = telemetria._RaizSemClienteOrfao()

    resultado = amostrador.should_sample(None, 1, "x", tipo)

    assert resultado.decision is decisao
    assert amostrador.get_description() == "RaizSemClienteOrfao"


def test_span_de_cliente_sem_pai_nao_gera_trace(provedor: _Provedor) -> None:
    # A consulta do poll do relay, do coletor e do ciclo ocioso: span de cliente
    # com nada em volta.
    with provedor.tracer().start_as_current_span("SELECT outbox", kind=SpanKind.CLIENT):
        pass

    assert provedor.spans() == {}


def test_span_de_cliente_com_pai_entra_no_trace_do_pai(provedor: _Provedor) -> None:
    tracer = provedor.tracer()
    with (
        tracer.start_as_current_span("POST /x", kind=SpanKind.SERVER),
        tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT),
    ):
        pass

    spans = provedor.spans()
    assert set(spans) == {"POST /x", "SELECT"}
    assert spans["SELECT"].context.trace_id == spans["POST /x"].context.trace_id
    assert spans["SELECT"].parent.span_id == spans["POST /x"].context.span_id


@pytest.mark.parametrize(
    "tipo", [SpanKind.SERVER, SpanKind.CONSUMER, SpanKind.PRODUCER, SpanKind.INTERNAL]
)
def test_span_que_nao_e_de_cliente_abre_o_trace(
    provedor: _Provedor, tipo: SpanKind
) -> None:
    with provedor.tracer().start_as_current_span("raiz", kind=tipo):
        pass

    assert set(provedor.spans()) == {"raiz"}


def test_o_pai_remoto_decide_o_filho_de_cliente(provedor: _Provedor) -> None:
    amostrado = trace.set_span_in_context(_pai_remoto(amostrado=True))
    ignorado = trace.set_span_in_context(_pai_remoto(amostrado=False))
    tracer = provedor.tracer()

    with tracer.start_as_current_span("com", context=amostrado, kind=SpanKind.CLIENT):
        pass
    with tracer.start_as_current_span("sem", context=ignorado, kind=SpanKind.CLIENT):
        pass

    assert set(provedor.spans()) == {"com"}


# --- texto de erro ------------------------------------------------------------

# Escopo do instrumentador do SQLAlchemy e o deste servico: a limpeza vale igual.
_ESCOPOS = ["opentelemetry.instrumentation.sqlalchemy", "pytstop.mensageria"]


def _erro_de_driver() -> Exception:
    return RuntimeError(f'DETAIL: Failing row contains ("{_PLACA}", livre)')


@pytest.mark.parametrize("escopo", _ESCOPOS)
def test_span_perde_a_mensagem_e_a_descricao_do_erro(
    provedor: _Provedor, escopo: str
) -> None:
    with provedor.tracer(escopo).start_as_current_span("INSERT") as span:
        erro = _erro_de_driver()
        span.record_exception(erro)
        span.set_status(StatusCode.ERROR, str(erro))

    (exportado,) = provedor.exportados.get_finished_spans()
    assert exportado.status.status_code is StatusCode.ERROR
    assert exportado.status.description is None
    (evento,) = exportado.events
    assert evento.name == "exception"
    assert dict(evento.attributes or {}) == {
        "exception.type": "RuntimeError",
        "exception.escaped": "False",
    }
    assert _PLACA not in repr(exportado.to_json())


@pytest.mark.parametrize("escopo", _ESCOPOS)
def test_excecao_que_escapa_do_span_nao_leva_texto(
    provedor: _Provedor, escopo: str
) -> None:
    # O with do SDK grava a excecao e a descricao "Tipo: mensagem".
    with (
        pytest.raises(RuntimeError),
        provedor.tracer(escopo).start_as_current_span("GET /x", kind=SpanKind.SERVER),
    ):
        raise _erro_de_driver()

    (exportado,) = provedor.exportados.get_finished_spans()
    assert exportado.status.status_code is StatusCode.ERROR
    assert exportado.status.description is None
    assert _PLACA not in repr(exportado.to_json())


def test_evento_que_nao_e_excecao_e_atributo_do_span_ficam_como_criados(
    provedor: _Provedor,
) -> None:
    with provedor.tracer().start_as_current_span("process X") as span:
        span.set_status(StatusCode.ERROR)
        span.set_attribute("pytstop.resultado", "dlq")
        span.add_event("retry", {"tentativa": 2})

    (exportado,) = provedor.exportados.get_finished_spans()
    assert exportado.status.status_code is StatusCode.ERROR
    assert dict(exportado.attributes or {}) == {"pytstop.resultado": "dlq"}
    (evento,) = exportado.events
    assert (evento.name, dict(evento.attributes or {})) == ("retry", {"tentativa": 2})


def test_span_sem_erro_sai_como_foi_criado(provedor: _Provedor) -> None:
    with provedor.tracer().start_as_current_span("GET /ok") as span:
        span.set_status(StatusCode.OK)

    (exportado,) = provedor.exportados.get_finished_spans()
    assert exportado.status.status_code is StatusCode.OK
    assert exportado.events == ()
