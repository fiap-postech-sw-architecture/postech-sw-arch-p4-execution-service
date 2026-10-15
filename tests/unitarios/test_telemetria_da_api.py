"""A partida da API com o endpoint OTLP no ambiente, como no cluster."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from src.compartilhado.infraestrutura.mensageria.telemetria import criar_provedor
from src.main import criar_app
from tests.conftest import provedor_dos_testes


def test_api_nao_deixa_o_fastapi_montar_o_export_otlp_na_partida(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O FastAPI soma exportadores OTLP ``http/protobuf`` por conta propria na
    partida quando ``OTEL_EXPORTER_OTLP_ENDPOINT`` esta no ambiente. Sem o extra
    ``fastapi[opentelemetry]`` ele avisa que a configuracao falhou, e o endpoint
    do cluster e gRPC (4317): a telemetria dos processos e a de
    ``mensageria/telemetria.py``. Com o ``auto_configure`` desligado o log de
    boot fica limpo."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://execucao_app:a1b2c3@db/execucao")
    monkeypatch.setenv("JWKS_URL", "http://os-service:8000/.well-known/jwks.json")
    monkeypatch.setenv("BILLING_URL", "http://billing-service:8000")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger.pytstop-plataforma:4317"
    )
    avisos: list[str] = []

    class Coletor(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            avisos.append(record.getMessage())

    logger = logging.getLogger("fastapi")
    coletor = Coletor(level=logging.WARNING)
    logger.addHandler(coletor)
    app = criar_app(provedor_dos_testes())
    try:
        # O engine e os clientes so conectam no primeiro uso: o lifespan sobe
        # sem banco nem OS Service.
        with TestClient(app):
            pass
    finally:
        logger.removeHandler(coletor)

    # O aviso so sai sem o extra ``fastapi[opentelemetry]``; com ele instalado o
    # FastAPI montaria os exportadores sem avisar. O estado e a causa: a opcao,
    # que o FastAPI guarda em ``_telemetry`` (0.142), fica desligada.
    assert app._telemetry["auto_configure"] is False
    assert avisos == []


class _ProvedorEspia(TracerProvider):
    """Provider que conta os ``shutdown``."""

    def __init__(self) -> None:
        super().__init__(shutdown_on_exit=False)
        self.encerrado = 0

    def shutdown(self) -> None:
        self.encerrado += 1
        super().shutdown()


@pytest.fixture
def ambiente_da_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://execucao_app:a1b2c3@db/execucao")
    monkeypatch.setenv("JWKS_URL", "http://os-service:8000/.well-known/jwks.json")
    monkeypatch.setenv("BILLING_URL", "http://billing-service:8000")


def test_a_api_instala_o_provider_do_processo_e_o_encerra_no_fim(
    monkeypatch: pytest.MonkeyPatch, ambiente_da_api: None
) -> None:
    """Sem a chamada a API ficaria com o provider vazio do OpenTelemetry (zero span,
    sem erro nem log); sem o ``shutdown`` os spans que esperam no lote se perdem
    quando o uvicorn re-levanta o SIGTERM e o processo nao e o PID 1."""
    espia = _ProvedorEspia()
    pedidos: list[str] = []

    def instalar(processo: str) -> TracerProvider:
        pedidos.append(processo)
        return espia

    monkeypatch.setattr("src.main.configurar_telemetria", instalar)

    with TestClient(criar_app()):
        assert espia.encerrado == 0

    assert pedidos == ["api"]
    assert espia.encerrado == 1


def test_a_api_nao_encerra_o_provider_que_recebeu(ambiente_da_api: None) -> None:
    """Quem injeta o provider e dono dele (o dos testes serve a sessao inteira)."""
    espia = _ProvedorEspia()

    with TestClient(criar_app(espia)):
        pass

    assert espia.encerrado == 0


def test_os_spans_da_api_saem_pelo_provider_dela_e_nao_pelo_global(
    ambiente_da_api: None, spans: InMemorySpanExporter
) -> None:
    exportados = InMemorySpanExporter()
    provedor = criar_provedor({}, processo="api")
    provedor.add_span_processor(SimpleSpanProcessor(exportados))

    with TestClient(criar_app(provedor)) as cliente:
        assert cliente.get("/api/v1/estoque").status_code == 401

    assert [s.name for s in exportados.get_finished_spans()] == ["GET /api/v1/estoque"]
    assert spans.get_finished_spans() == ()
