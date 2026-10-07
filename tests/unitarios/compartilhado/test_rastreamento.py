"""Passo retomado pelo mecanico: filho do contexto guardado, link para quem retomou."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from opentelemetry import context, trace
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    StatusCode,
    TraceFlags,
    TraceState,
)

from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    tracer,
)
from src.compartilhado.infraestrutura.rastreamento import (
    colunas_do_contexto_de_espera,
    retomando,
)
from src.diagnostico.infraestrutura.mapping import diagnosticos_table

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from sqlalchemy.orm import Session

_GUARDADO = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


class _Linha:
    def __init__(self, traceparent: str | None) -> None:
        self._mapping = {"traceparent": traceparent, "tracestate": None}


class _Sessao:
    """Sessao falsa: o registro da ordem com o contexto guardado (ou sem registro)."""

    def __init__(self, linha: _Linha | None) -> None:
        self._linha = linha

    def execute(self, _stmt: object) -> _Sessao:
        return self

    def one_or_none(self) -> _Linha | None:
        return self._linha


class _SessaoQueRegistra(_Sessao):
    """Sessao falsa que guarda as consultas recebidas."""

    def __init__(self, linha: _Linha | None) -> None:
        super().__init__(linha)
        self.consultas: list[Any] = []

    def execute(self, stmt: object) -> _Sessao:
        self.consultas.append(stmt)
        return self


def _retomar(sessao: _Sessao) -> dict[str, str]:
    with retomando(
        cast("Session", sessao), diagnosticos_table, uuid4(), "iniciar diagnostico"
    ):
        return contexto_atual()


def _span(spans: InMemorySpanExporter, nome: str) -> Any:
    (encontrado,) = [s for s in spans.get_finished_spans() if s.name == nome]
    return encontrado


def _w3c(span: Any) -> str:
    contexto = span.get_span_context()
    return (
        f"00-{contexto.trace_id:032x}-{contexto.span_id:016x}-"
        f"{contexto.trace_flags:02x}"
    )


def test_passo_e_filho_do_contexto_guardado_com_link_para_quem_retomou(
    spans: InMemorySpanExporter,
) -> None:
    with tracer.start_as_current_span("POST /inicio", kind=SpanKind.SERVER) as http:
        gravado_na_outbox = _retomar(_Sessao(_Linha(_GUARDADO)))

    passo = _span(spans, "iniciar diagnostico")
    assert passo.context.trace_id == 0x0AF7651916CD43DD8448EB211C80319C
    assert passo.parent.span_id == 0xB7AD6B7169203331
    assert [link.context.span_id for link in passo.links] == [
        http.get_span_context().span_id
    ]
    # O fato do mecanico vai para a outbox com o contexto do passo: o relay o
    # publica no trace da saga.
    assert gravado_na_outbox["traceparent"] == _w3c(passo)


def test_sem_contexto_guardado_o_passo_abre_trace_proprio(
    spans: InMemorySpanExporter,
) -> None:
    for linha in (_Linha(None), None):
        _retomar(_Sessao(linha))
    passos = [s for s in spans.get_finished_spans() if s.name == "iniciar diagnostico"]
    assert [(s.parent, list(s.links)) for s in passos] == [(None, []), (None, [])]


def test_contexto_e_lido_so_do_registro_da_ordem_retomada() -> None:
    sessao = _SessaoQueRegistra(_Linha(_GUARDADO))
    ordem_id = uuid4()
    with retomando(
        cast("Session", sessao), diagnosticos_table, ordem_id, "iniciar diagnostico"
    ):
        pass
    (consulta,) = sessao.consultas
    compilada = consulta.compile()
    assert "WHERE diagnosticos.ordem_id = " in str(compilada)
    assert list(compilada.params.values()) == [ordem_id]


def test_excecao_do_caso_de_uso_nao_vai_para_o_span(
    spans: InMemorySpanExporter,
) -> None:
    # A mensagem da excecao pode trazer dado do pedido (placa, texto livre).
    with (
        pytest.raises(RuntimeError),
        retomando(
            cast("Session", _Sessao(_Linha(_GUARDADO))),
            diagnosticos_table,
            uuid4(),
            "iniciar diagnostico",
        ),
    ):
        raise RuntimeError("placa ABC1D23 do pedido")
    passo = _span(spans, "iniciar diagnostico")
    assert passo.events == ()
    assert passo.status.status_code is StatusCode.UNSET


def test_colunas_de_espera_guardam_o_contexto_do_span_corrente() -> None:
    # O comando da saga cria o registro no span do consumidor: o INSERT grava
    # traceparent e tracestate dele, sem passar pelo agregado.
    traceparent, tracestate = colunas_do_contexto_de_espera()
    pai = SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        is_remote=False,
        trace_flags=TraceFlags(1),
        trace_state=TraceState([("fornecedor", "estado-1")]),
    )
    token = context.attach(trace.set_span_in_context(NonRecordingSpan(pai)))
    try:
        # O SQLAlchemy chama o default com o contexto de execucao.
        assert traceparent.default.arg(None) == _GUARDADO
        assert tracestate.default.arg(None) == "fornecedor=estado-1"
    finally:
        context.detach(token)
