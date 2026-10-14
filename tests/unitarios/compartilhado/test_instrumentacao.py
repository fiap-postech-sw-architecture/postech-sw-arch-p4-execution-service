"""Instrumentacao da API: o que o span guarda da requisicao e do cliente HTTP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import baggage, trace
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace import SpanKind, StatusCode

from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker
from src.compartilhado.infraestrutura.instrumentacao import (
    instrumentar_api,
    instrumentar_cliente_http,
    redigir_url_da_span,
)
from src.compartilhado.infraestrutura.mensageria import telemetria
from src.compartilhado.infraestrutura.mensageria.telemetria import tracer
from src.diagnostico.infraestrutura.validador_billing import ValidadorDeItensBilling
from tests.conftest import provedor_dos_testes
from tests.spans import tudo_que_o_span_leva, tudo_que_os_spans_levam
from tests.unitarios.compartilhado.hook_nos_modos import CANARIO

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

RAIZ = Path(__file__).resolve().parents[3]
TEMPLATE = "/api/v1/execucoes/{ordem_id}/inicio"
SEGREDO = "tok-3f9a1c"
URL = ("url.path", "http.target", "http.url", "url.full")
# O que o instrumentador grava de quem chamou, nas duas convencoes semanticas.
TEXTOS_DO_CHAMADOR = (
    "http.user_agent",
    "user_agent.original",
    "net.peer.ip",
    "client.address",
    "http.server_name",
    "http.host",
    "server.address",
)
PORTAS_DO_CHAMADOR = ("net.peer.port", "client.port", "net.host.port", "server.port")


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

    assert span.gravados == dict.fromkeys(URL, TEMPLATE)


def test_hook_requisicao_sem_rota_casada_leva_nao_roteada() -> None:
    span = _SpanFalso({})

    redigir_url_da_span(_span(span), {"path": f"/qualquer/{SEGREDO}"})

    assert span.gravados == dict.fromkeys(URL, "nao_roteada")


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


def test_hook_troca_o_que_vem_do_chamador_por_valor_fixo() -> None:
    entrada = dict.fromkeys(TEXTOS_DO_CHAMADOR, "dado do chamador")
    entrada |= dict.fromkeys(PORTAS_DO_CHAMADOR, 51234)
    span = _SpanFalso({"http.route": TEMPLATE, **entrada})

    redigir_url_da_span(_span(span), {})

    esperado = dict.fromkeys(TEXTOS_DO_CHAMADOR, "REDACTED")
    esperado |= dict.fromkeys(PORTAS_DO_CHAMADOR, 0)
    assert {a: span.gravados[a] for a in entrada} == esperado


def test_hook_nao_inventa_o_dado_do_chamador_que_o_span_nao_tem() -> None:
    # Na convencao nova o span nao traz os nomes da antiga, e vice-versa.
    span = _SpanFalso({"http.route": TEMPLATE, "http.user_agent": "x"})

    redigir_url_da_span(_span(span), {})

    assert set(span.gravados) == {*URL, "http.user_agent"}


# --- FastAPI ---------------------------------------------------------------------


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/links/{token}/decisao")
    def decisao(token: str) -> dict[str, str]:
        return {"token": token}

    @app.post("/links/{token}/decisao")
    def decide(token: str, corpo: dict[str, Any]) -> dict[str, str]:
        return {"token": token}

    @app.get("/explode/{placa}")
    def explode(placa: str) -> None:
        msg = f"defeito com a placa {placa}"
        raise RuntimeError(msg)

    @app.get("/metrics")
    def metricas() -> str:
        return "ok"

    @app.get("/api/v1/saude")
    def saude() -> str:
        return "ok"

    @app.get("/api/v1/saude/pronto")
    def pronto() -> str:
        return "ok"

    instrumentar_api(app, provedor_dos_testes())
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
    assert {atributos[a] for a in URL} == {template}
    assert atributos["url.query"] == "REDACTED"
    assert SEGREDO not in "\n".join(tudo_que_o_span_leva(servidor))


def test_requisicao_sem_rota_casada_nao_leva_o_caminho_no_span(
    spans: InMemorySpanExporter,
) -> None:
    resposta = TestClient(_app()).get(f"/nao/existe/{SEGREDO}")

    assert resposta.status_code == 404
    servidor = _servidor(spans)
    assert dict(servidor.attributes or {})["url.path"] == "nao_roteada"
    assert SEGREDO not in "\n".join(tudo_que_o_span_leva(servidor))


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
    assert "ABC1D23" not in "\n".join(tudo_que_o_span_leva(servidor))


@pytest.mark.parametrize(
    "caminho", ["/metrics", "/api/v1/saude", "/api/v1/saude/pronto"]
)
@pytest.mark.parametrize("prefixo", ["", "/execucao"], ids=["compose", "borda"])
def test_metricas_e_sondas_nao_geram_span(
    spans: InMemorySpanExporter, caminho: str, prefixo: str
) -> None:
    # No cluster o uvicorn poe o ROOT_PATH (/execucao) na frente do caminho.
    cliente = TestClient(_app(), root_path=prefixo)

    assert cliente.get(caminho).status_code == 200

    assert spans.get_finished_spans() == ()


@pytest.mark.parametrize(
    "host",
    [
        "x/api/v1/saude",
        "x/api/v1/saude/pronto",
        "x/metrics",
        "[",
        "a]b",
        "user:senha@outro.exemplo",
    ],
)
def test_host_do_chamador_nao_tira_a_requisicao_do_trace(
    spans: InMemorySpanExporter, host: str
) -> None:
    # A exclusao e sobre a URL montada com o Host, que vem do chamador: sem a
    # ancora, `Host: x/api/v1/saude` apagaria o trace de qualquer requisicao.
    resposta = TestClient(_app()).get(
        f"/links/{SEGREDO}/decisao", headers={"Host": host}
    )

    assert resposta.status_code == 200
    servidor = _servidor(spans)
    assert servidor.name == "GET /links/{token}/decisao"
    # E o hook nao decompoe a URL, entao o Host malformado nao o derruba.
    assert dict(servidor.attributes or {})["url.path"] == "/links/{token}/decisao"


def test_rota_com_metrics_no_meio_do_caminho_segue_com_trace(
    spans: InMemorySpanExporter,
) -> None:
    # A exclusao e sobre o fim da URL: so o /metrics do Prometheus fica sem trace.
    app = FastAPI()

    @app.get("/api/v1/metrics/{ano}")
    def por_ano(ano: int) -> int:
        return ano

    instrumentar_api(app, provedor_dos_testes())

    assert TestClient(app).get("/api/v1/metrics/2026").status_code == 200
    assert _servidor(spans).name == "GET /api/v1/metrics/{ano}"


def test_requisicao_nao_gera_os_spans_de_receive_e_send(
    spans: InMemorySpanExporter,
) -> None:
    # Sem corpo so ha `send`: o POST com corpo tambem tem `receive`.
    cliente = TestClient(_app())

    cliente.get(f"/links/{SEGREDO}/decisao")
    cliente.post(f"/links/{SEGREDO}/decisao", json={"decisao": "aprovado"})

    assert [s.name for s in spans.get_finished_spans()] == [
        "GET /links/{token}/decisao",
        "POST /links/{token}/decisao",
    ]


@pytest.mark.parametrize(
    ("modo", "textos", "portas"),
    [
        pytest.param(
            "",
            ("http.user_agent", "net.peer.ip", "http.server_name", "http.host"),
            ("net.peer.port", "net.host.port"),
            id="convencao-antiga",
        ),
        pytest.param(
            "http",
            ("user_agent.original", "client.address", "server.address"),
            ("client.port", "server.port"),
            id="convencao-nova",
        ),
        pytest.param(
            "http/dup",
            (
                "http.user_agent",
                "net.peer.ip",
                "http.server_name",
                "http.host",
                "user_agent.original",
                "client.address",
                "server.address",
            ),
            ("net.peer.port", "net.host.port", "client.port", "server.port"),
            id="as-duas",
        ),
    ],
)
def test_server_nao_guarda_dado_do_chamador_em_nenhuma_convencao_semantica(
    modo: str, textos: tuple[str, ...], portas: tuple[str, ...]
) -> None:
    """User-Agent, Host, IP e portas do chamador, na convencao antiga, na nova e nas
    duas (``OTEL_SEMCONV_STABILITY_OPT_IN``, lida uma vez por processo)."""
    saida = subprocess.run(
        [sys.executable, "-m", "tests.unitarios.compartilhado.hook_nos_modos"],
        env={**os.environ, "OTEL_SEMCONV_STABILITY_OPT_IN": modo},
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    span = json.loads(saida)

    atributos = span["atributos"]
    assert {a: atributos[a] for a in textos} == dict.fromkeys(textos, "REDACTED")
    assert {a: atributos[a] for a in portas} == dict.fromkeys(portas, 0)
    tudo = "\n".join(span["tudo"])
    for dado in (CANARIO, "testclient", "testserver"):
        assert dado not in tudo


# --- propagador: so traceparent e tracestate --------------------------------------

_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C


def test_propagador_global_so_leva_traceparent_e_tracestate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O padrao do SDK soma o ``baggage`` do chamador."""
    instalados: list[object] = []
    monkeypatch.setattr(telemetria, "set_global_textmap", instalados.append)

    telemetria.instalar_propagador()

    [propagador] = instalados
    assert propagador.fields == {"traceparent", "tracestate"}


def _app_que_chama_o_billing(
    recebido: dict[str, str], visto_na_rota: dict[str, Any]
) -> FastAPI:
    """Rota que le o contexto da requisicao e chama o Billing pelo httpx."""

    def billing(requisicao: httpx.Request) -> httpx.Response:
        recebido.update(requisicao.headers)
        return httpx.Response(200)

    cliente = httpx.Client(
        base_url="http://billing.test", transport=httpx.MockTransport(billing)
    )
    instrumentar_cliente_http(cliente, provedor_dos_testes())
    app = FastAPI()

    @app.get("/x")
    def rota() -> str:
        contexto = trace.get_current_span().get_span_context()
        visto_na_rota["baggage"] = dict(baggage.get_all())
        visto_na_rota["tracestate"] = contexto.trace_state.to_header()
        cliente.get("/precos")
        return "ok"

    instrumentar_api(app, provedor_dos_testes())
    return app


def test_baggage_do_chamador_nao_entra_na_api_nem_vai_ao_billing(
    spans: InMemorySpanExporter,
) -> None:
    recebido: dict[str, str] = {}
    visto: dict[str, Any] = {}
    cliente = TestClient(_app_que_chama_o_billing(recebido, visto))

    resposta = cliente.get(
        "/x", headers={"traceparent": _TRACEPARENT, "baggage": "usuario=maria"}
    )

    assert resposta.status_code == 200
    assert visto["baggage"] == {}
    assert recebido["traceparent"].split("-")[1] == f"{_TRACE_ID:032x}"
    assert "baggage" not in recebido


def test_tracestate_curto_do_chamador_segue_ate_o_billing(
    spans: InMemorySpanExporter,
) -> None:
    recebido: dict[str, str] = {}
    visto: dict[str, Any] = {}
    cliente = TestClient(_app_que_chama_o_billing(recebido, visto))

    cliente.get("/x", headers={"traceparent": _TRACEPARENT, "tracestate": "vendor=ok"})

    assert visto["tracestate"] == "vendor=ok"
    assert recebido["tracestate"] == "vendor=ok"


@pytest.mark.parametrize(
    "tracestate",
    [
        pytest.param(",".join(f"v{i}={'x' * 100}" for i in range(6)), id="6-membros"),
        pytest.param(",".join(f"v{i}={'x' * 100}" for i in range(32)), id="32-membros"),
    ],
)
def test_tracestate_acima_do_teto_cai_na_entrada_e_nao_vai_ao_billing(
    spans: InMemorySpanExporter, tracestate: str
) -> None:
    # 512 e o teto do W3C; o padrao do SDK so corta em 8192.
    assert len(tracestate) > 512
    recebido: dict[str, str] = {}
    visto: dict[str, Any] = {}
    cliente = TestClient(_app_que_chama_o_billing(recebido, visto))

    cliente.get("/x", headers={"traceparent": _TRACEPARENT, "tracestate": tracestate})

    # O traceparent segue valendo: o trace e o mesmo.
    assert recebido["traceparent"].split("-")[1] == f"{_TRACE_ID:032x}"
    assert visto["tracestate"] == ""
    assert "tracestate" not in recebido


def test_o_teto_do_tracestate_e_sobre_a_soma_das_linhas_do_cabecalho() -> None:
    class Linhas(Getter[dict[str, list[str]]]):
        def get(self, carrier: dict[str, list[str]], key: str) -> list[str] | None:
            return carrier.get(key)

        def keys(self, carrier: dict[str, list[str]]) -> list[str]:
            return list(carrier)

    def lido(linhas: list[str]) -> str:
        portador = {"traceparent": [_TRACEPARENT], "tracestate": linhas}
        contexto = telemetria._PROPAGADOR.extract(portador, getter=Linhas())
        return (
            trace.get_current_span(contexto).get_span_context().trace_state.to_header()
        )

    # Duas linhas de 255 e 256 caracteres somam 512 com a virgula: passam; 257, nao.
    a, b = "a=" + "x" * 253, "b=" + "y" * 254
    assert len(a) == 255
    assert len(b) == 256
    assert lido([a, b]) == f"{a},{b}"
    assert lido([a, b + "z"]) == ""


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
    instrumentar_cliente_http(cliente, provedor_dos_testes())
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
    # O token do mecanico segue no cabecalho da chamada, nunca no span.
    assert "Bearer" not in "\n".join(
        tudo_que_os_spans_levam(spans.get_finished_spans())
    )
