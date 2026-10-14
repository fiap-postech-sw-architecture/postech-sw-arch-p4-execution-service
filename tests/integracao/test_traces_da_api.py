"""Trace da API contra o PostgreSQL: o que a requisicao exporta e o que nao exporta.

O app e o real (lifespan inclusive) e o provider dos testes e o de producao, com
o amostrador e a limpeza do texto de erro.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx
import pytest
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.compartilhado.dominio.veiculo import Veiculo
from src.compartilhado.infraestrutura.mensageria.telemetria import tracer
from src.diagnostico.aplicacao.use_cases import RegistrarSolicitacaoDeDiagnostico
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.estoque.infraestrutura.seed import semear
from tests.integracao.transacao import transacao_do_comando

if TYPE_CHECKING:
    from collections.abc import Callable

    import respx
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from sqlalchemy.orm import Session, sessionmaker

URL = "/api/v1/diagnosticos"
PLACA = "BRA2E19"
OBSERVACOES = "falar com Joao Silva, telefone da oficina"
ITENS = [
    {"tipo": "servico", "codigo": "SRV-TROCA-PASTILHA", "quantidade": 1},
    {"tipo": "peca", "codigo": "PEC-PASTILHA-FREIO", "quantidade": 1},
]
TEMPLATE_INICIO = "/api/v1/diagnosticos/{ordem_id}/inicio"


@pytest.fixture
def mecanico(autenticar: Callable[..., dict[str, str]]) -> dict[str, str]:
    return autenticar("mecanico", uuid4())


def _solicitar(session_factory: sessionmaker[Session]) -> UUID:
    ordem_id = uuid4()
    with session_factory() as session:
        RegistrarSolicitacaoDeDiagnostico(
            DiagnosticoSQLAlchemyRepository(session), transacao_do_comando(session)
        ).executar(
            ordem_id,
            Veiculo(
                veiculo_id=uuid4(), placa=PLACA, marca="Fiat", modelo="Uno", ano=2020
            ),
            "Freio chiando, cliente Maria",
            solicitacao_id=uuid4(),
        )
    return ordem_id


def _por_tipo(spans: InMemorySpanExporter, tipo: SpanKind) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.kind is tipo]


def _texto(span: ReadableSpan) -> str:
    return json.dumps(span.to_json())


def test_requisicao_gera_span_server_com_os_comandos_de_banco_como_filhos(
    api: TestClient,
    autenticar: Callable[..., dict[str, str]],
    spans: InMemorySpanExporter,
) -> None:
    resposta = api.get("/api/v1/estoque", headers=autenticar("atendente"))

    assert resposta.status_code == 200
    (servidor,) = _por_tipo(spans, SpanKind.SERVER)
    consultas = _por_tipo(spans, SpanKind.CLIENT)
    assert servidor.name == "GET /api/v1/estoque"
    assert any(s.name.startswith("SELECT") for s in consultas)
    assert {s.context.trace_id for s in spans.get_finished_spans()} == {
        servidor.context.trace_id
    }
    assert {s.parent.span_id for s in consultas} == {servidor.context.span_id}
    assert any(
        "FROM itens_estoque" in str((s.attributes or {}).get("db.statement"))
        for s in consultas
    )


@pytest.mark.parametrize(
    "caminho", ["/metrics", "/api/v1/saude", "/api/v1/saude/pronto"]
)
def test_raspagem_e_sondas_nao_exportam_span(
    api: TestClient, spans: InMemorySpanExporter, caminho: str
) -> None:
    # A readiness consulta o banco: o span de cliente dela, sem pai, e descartado.
    assert api.get(caminho).status_code == 200

    assert spans.get_finished_spans() == ()


def test_span_da_requisicao_leva_o_template_e_nao_a_ordem_nem_a_query(
    api: TestClient, mecanico: dict[str, str], spans: InMemorySpanExporter
) -> None:
    ordem_id = uuid4()

    resposta = api.post(f"{URL}/{ordem_id}/inicio?x=segredo", headers=mecanico)

    assert resposta.status_code == 404
    (servidor,) = _por_tipo(spans, SpanKind.SERVER)
    atributos = dict(servidor.attributes or {})
    assert servidor.name == f"POST {TEMPLATE_INICIO}"
    assert {
        atributos[n] for n in ("url.path", "http.target", "http.url", "url.full")
    } == {TEMPLATE_INICIO}
    assert atributos["url.query"] == "REDACTED"
    do_servico = [
        s
        for s in spans.get_finished_spans()
        if s.instrumentation_scope
        and not s.instrumentation_scope.name.startswith("pytstop")
    ]
    assert do_servico
    for span in do_servico:
        assert str(ordem_id) not in _texto(span)
        assert "segredo" not in _texto(span)


def test_requisicao_sem_rota_nao_leva_o_caminho_no_span(
    api: TestClient, spans: InMemorySpanExporter
) -> None:
    resposta = api.get("/api/v1/nao-existe/tok-3f9a1c")

    assert resposta.status_code == 404
    (servidor,) = _por_tipo(spans, SpanKind.SERVER)
    assert dict(servidor.attributes or {})["url.path"] == "nao_roteada"
    assert "tok-3f9a1c" not in _texto(servidor)


def test_conclusao_do_diagnostico_e_um_trace_com_o_banco_e_o_billing(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    billing: respx.MockRouter,
    spans: InMemorySpanExporter,
) -> None:
    semear(session_factory)
    # O comando da saga cria o diagnostico dentro do span do consumidor: o
    # traceparent dele fica guardado, e a acao do mecanico continua o trace.
    with tracer.start_as_current_span("process SolicitarDiagnostico") as consumo:
        ordem_id = _solicitar(session_factory)
    assert api.post(f"{URL}/{ordem_id}/inicio", headers=mecanico).status_code == 200
    rota = billing.post("/api/v1/precos/validacao").respond(200, json={"invalidos": []})
    spans.clear()

    resposta = api.post(
        f"{URL}/{ordem_id}/conclusao",
        json={"itens": ITENS, "observacoes": OBSERVACOES},
        headers=mecanico,
    )

    assert resposta.status_code == 200, resposta.text
    (servidor,) = _por_tipo(spans, SpanKind.SERVER)
    passo = next(
        s for s in spans.get_finished_spans() if s.name == "concluir diagnostico"
    )
    (chamada,) = [
        s
        for s in _por_tipo(spans, SpanKind.CLIENT)
        if (s.attributes or {}).get("http.method") == "POST"
    ]
    # O passo continua o trace da saga e aponta para a requisicao do mecanico.
    assert passo.context.trace_id == consumo.get_span_context().trace_id
    assert passo.parent.span_id == consumo.get_span_context().span_id
    assert [link.context.span_id for link in passo.links] == [servidor.context.span_id]
    assert servidor.context.trace_id != passo.context.trace_id
    # A chamada ao Billing sai como filha do passo, com o traceparent dela.
    assert chamada.parent.span_id == passo.context.span_id
    enviado = rota.calls.last.request.headers["traceparent"]
    assert enviado.split("-")[1:3] == [
        f"{chamada.context.trace_id:032x}",
        f"{chamada.context.span_id:016x}",
    ]
    # E o banco aparece dos dois lados: na requisicao e dentro do passo.
    em_trace_do_passo = [
        s
        for s in spans.get_finished_spans()
        if s.context.trace_id == passo.context.trace_id and s.kind is SpanKind.CLIENT
    ]
    assert any(
        "diagnosticos" in str((s.attributes or {}).get("db.statement"))
        for s in em_trace_do_passo
    )


def test_nenhum_span_leva_placa_nem_texto_livre(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    billing: respx.MockRouter,
    spans: InMemorySpanExporter,
) -> None:
    semear(session_factory)
    ordem_id = _solicitar(session_factory)
    billing.post("/api/v1/precos/validacao").respond(200, json={"invalidos": []})
    spans.clear()

    api.get(URL, headers=mecanico)
    api.post(f"{URL}/{ordem_id}/inicio", headers=mecanico)
    api.post(
        f"{URL}/{ordem_id}/conclusao",
        json={"itens": ITENS, "observacoes": OBSERVACOES},
        headers=mecanico,
    )

    exportados = spans.get_finished_spans()
    assert len(exportados) > 5
    tudo = "\n".join(_texto(s) for s in exportados)
    for sensivel in (PLACA, "Joao", "Maria", "Freio chiando", "telefone"):
        assert sensivel not in tudo


def test_erro_do_banco_nao_leva_o_detail_do_driver_ao_span(
    api: TestClient, spans: InMemorySpanExporter
) -> None:
    # O CHECK reprova a linha, e o PostgreSQL devolve "Failing row contains" com
    # a placa: o texto do driver nao pode ir para o status do span.
    with (
        pytest.raises(IntegrityError, match=PLACA),
        tracer.start_as_current_span("requisicao", kind=SpanKind.SERVER),
        api.app.state.session_factory() as sessao,
    ):
        sessao.execute(
            text(
                "INSERT INTO diagnosticos (ordem_id, status, veiculo, itens, "
                "observacoes, solicitado_em) VALUES (:id, 'INVALIDO', "
                "CAST(:veiculo AS jsonb), '[]', '', now())"
            ),
            {"id": uuid4(), "veiculo": json.dumps({"placa": PLACA})},
        )

    (insercao,) = [
        s for s in _por_tipo(spans, SpanKind.CLIENT) if s.name.startswith("INSERT")
    ]
    assert insercao.status.status_code is StatusCode.ERROR
    assert insercao.status.description is None
    assert PLACA not in "\n".join(_texto(s) for s in spans.get_finished_spans())


def test_chamada_ao_billing_com_falha_nao_leva_o_texto_do_erro(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    billing: respx.MockRouter,
    spans: InMemorySpanExporter,
) -> None:
    semear(session_factory)
    ordem_id = _solicitar(session_factory)
    api.post(f"{URL}/{ordem_id}/inicio", headers=mecanico)
    billing.post("/api/v1/precos/validacao").mock(
        side_effect=httpx.ConnectError(f"sem rota para {PLACA}")
    )
    spans.clear()

    resposta = api.post(
        f"{URL}/{ordem_id}/conclusao", json={"itens": ITENS}, headers=mecanico
    )

    assert resposta.status_code == 503
    chamadas = [s for s in _por_tipo(spans, SpanKind.CLIENT) if s.name == "POST"]
    assert chamadas
    assert PLACA not in "\n".join(_texto(s) for s in spans.get_finished_spans())
