"""A partida da API com o endpoint OTLP no ambiente, como no cluster."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from src.main import criar_app


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
    try:
        # O engine e os clientes so conectam no primeiro uso: o lifespan sobe
        # sem banco nem OS Service.
        with TestClient(criar_app()):
            pass
    finally:
        logger.removeHandler(coletor)

    assert avisos == []
