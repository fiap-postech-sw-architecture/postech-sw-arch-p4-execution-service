"""Instrumentacao da API: o que o span guarda da requisicao e do cliente HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.trace import SpanKind, StatusCode

from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker
from src.compartilhado.infraestrutura.instrumentacao import (
    instrumentar_api,
    instrumentar_cliente_http,
    redigir_url_da_span,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import tracer
from src.diagnostico.infraestrutura.validador_billing import ValidadorDeItensBilling

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

TEMPLATE = "/api/v1/execucoes/{ordem_id}/inicio"
SEGREDO = "tok-3f9a1c"


class _SpanFalso:
    """Span minimo: o que o hook usa, sem o SDK."""

    def __init__(self, atributos: dict[str, Any], *, gravando: bool = True) -> None:
        self.attributes = atributos
        self._gravando = gravando
        self.gravados: dict[str, Any] = {}

    def is_recording(self) -> bool:
        return self._gravando

    def set_attribute(self, chave: str, valor: Any) -> None:
        self.gravados[chave] = valor


def _span(span: _SpanFalso) -> Any:
    return span


# --- hook ----------------------------------------------------------------------


def test_hook_troca_os_quatro_atributos_de_url_pelo_template() -> None:
    span = _SpanFalso({"http.route": TEMPLATE})

    redigir_url_da_span(_span(span), {"path": f"/api/v1/execucoes/{SEGREDO}/inicio"})

    assert span.gravados == dict.fromkeys(
        ("url.path", "http.target", "http.url", "url.full"), TEMPLATE
    )


def test_hook_requisicao_sem_rota_casada_leva_nao_roteada() -> None:
    span = _SpanFalso({})

    redigir_url_da_span(_span(span), {"path": f"/qualquer/{SEGREDO}"})

    assert set(span.gravados.values()) == {"nao_roteada"}


def test_hook_com_query_marca_a_query_como_redigida() -> None:
    span = _SpanFalso({"http.route": TEMPLATE})

    redigir_url_da_span(_span(span), {"query_string": f"x={SEGREDO}".encode()})

    assert span.gravados["url.query"] == "REDACTED"
    assert SEGREDO not in repr(span.gravados)


def test_hook_sem_query_nao_inventa_a_query() -> None:
    span = _SpanFalso({"http.route": TEMPLATE})

    redigir_url_da_span(_span(span), {"query_string": b""})

    assert "url.query" not in span.gravados


def test_hook_span_que_nao_grava_nao_toca_nos_atributos() -> None:
    span = _SpanFalso({"http.route": TEMPLATE}, gravando=False)

    redigir_url_da_span(_span(span), {"query_string": b"a=b"})

    assert span.gravados == {}


# --- FastAPI ---------------------------------------------------------------------


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/links/{token}/decisao")
    def decisao(token: str) -> dict[str, str]:
        return {"token": token}

    @app.get("/explode/{placa}")
    def explode(placa: str) -> None:
        msg = f"defeito com a placa {placa}"
        raise RuntimeError(msg)

    @app.get("/metrics")
    def metricas() -> str:
        return "ok"

    @app.get("/api/v1/saude/pronto")
    def pronto() -> str:
        return "ok"

    instrumentar_api(app)
    return app


def _servidor(spans: InMemorySpanExporter) -> ReadableSpan:
    (servidor,) = [s for s in spans.get_finished_spans() if s.kind is SpanKind.SERVER]
    return servidor


def test_span_da_requisicao_guarda_o_template_e_nenhum_atributo_o_caminho_cru(
    spans: InMemorySpanExporter,
) -> None:
    resposta = TestClient(_app()).get(f"/links/{SEGREDO}/decisao?filtro=x")

    assert resposta.status_code == 200
    servidor = _servidor(spans)
    template = "/links/{token}/decisao"
    assert servidor.name == f"GET {template}"
    atributos = dict(servidor.attributes or {})
    assert {atributos[a] for a in ("url.path", "http.target", "http.url")} == {template}
    assert atributos["url.full"] == template
    assert atributos["url.query"] == "REDACTED"
    assert SEGREDO not in repr(servidor.to_json())


def test_requisicao_sem_rota_casada_nao_leva_o_caminho_no_span(
    spans: InMemorySpanExporter,
) -> None:
    resposta = TestClient(_app()).get(f"/nao/existe/{SEGREDO}")

    assert resposta.status_code == 404
    servidor = _servidor(spans)
    assert dict(servidor.attributes or {})["url.path"] == "nao_roteada"
    assert SEGREDO not in repr(servidor.to_json())


def test_erro_nao_tratado_marca_o_span_sem_o_texto_da_excecao(
    spans: InMemorySpanExporter,
) -> None:
    # O instrumentador do FastAPI grava a excecao e "Tipo: mensagem" no span.
    resposta = TestClient(_app(), raise_server_exceptions=False).get("/explode/ABC1D23")

    assert resposta.status_code == 500
    servidor = _servidor(spans)
    assert servidor.status.status_code is StatusCode.ERROR
    assert servidor.status.description is None
    (evento,) = [e for e in servidor.events if e.name == "exception"]
    assert dict(evento.attributes or {})["exception.type"] == "RuntimeError"
    assert "ABC1D23" not in repr(servidor.to_json())


@pytest.mark.parametrize("caminho", ["/metrics", "/api/v1/saude/pronto"])
def test_metricas_e_sondas_nao_geram_span(
    spans: InMemorySpanExporter, caminho: str
) -> None:
    assert TestClient(_app()).get(caminho).status_code == 200

    assert spans.get_finished_spans() == ()


def test_rota_com_metrics_no_meio_do_caminho_segue_com_trace(
    spans: InMemorySpanExporter,
) -> None:
    # A exclusao e sobre o fim da URL: so o /metrics do Prometheus fica sem trace.
    app = FastAPI()

    @app.get("/api/v1/metrics/{ano}")
    def por_ano(ano: int) -> int:
        return ano

    instrumentar_api(app)

    assert TestClient(app).get("/api/v1/metrics/2026").status_code == 200
    assert _servidor(spans).name == "GET /api/v1/metrics/{ano}"


def test_requisicao_nao_gera_os_spans_de_receive_e_send(
    spans: InMemorySpanExporter,
) -> None:
    TestClient(_app()).get(f"/links/{SEGREDO}/decisao")

    assert [s.name for s in spans.get_finished_spans()] == [
        "GET /links/{token}/decisao"
    ]


# --- httpx -----------------------------------------------------------------------


def test_chamada_ao_billing_leva_o_traceparent_do_span_ativo(
    spans: InMemorySpanExporter,
) -> None:
    recebido: dict[str, str] = {}

    def billing(requisicao: httpx.Request) -> httpx.Response:
        recebido.update(requisicao.headers)
        return httpx.Response(200, json={"invalidos": []})

    cliente = httpx.Client(
        base_url="http://billing.test", transport=httpx.MockTransport(billing)
    )
    instrumentar_cliente_http(cliente)
    validador = ValidadorDeItensBilling(
        cliente=cliente,
        breaker=CircuitBreaker("billing-teste"),
        authorization="Bearer x",
    )

    with tracer.start_as_current_span("concluir diagnostico") as passo:
        validador.codigos_invalidos(servicos=["SRV-FREIOS"], pecas=[])

    (chamada,) = [s for s in spans.get_finished_spans() if s.kind is SpanKind.CLIENT]
    versao, trace_id, span_id, _flags = recebido["traceparent"].split("-")
    assert versao == "00"
    assert int(trace_id, 16) == passo.get_span_context().trace_id
    assert int(span_id, 16) == chamada.context.span_id
    assert chamada.parent.span_id == passo.get_span_context().span_id
    assert recebido["authorization"] == "Bearer x"
