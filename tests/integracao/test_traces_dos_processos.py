"""Trace do relay e do consumidor com o banco instrumentado e o amostrador real.

O provider dos testes e o de producao (``tests/conftest.py``): span de cliente
sem pai nao vira trace, e o que vira trace sai com o mesmo ``service.name`` dos
tres processos.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from sqlalchemy import event, text

from src.compartilhado.infraestrutura.instrumentacao import instrumentar_banco
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    tracer,
)
from tests.conftest import provedor_dos_testes
from tests.integracao.broker import EmSegundoPlano, envelope_de_comando, esperar_ate
from tests.spans import tudo_que_os_spans_levam

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from sqlalchemy import Engine

    from src.compartilhado.infraestrutura.mensageria.consumidor import Consumidor
    from src.compartilhado.infraestrutura.mensageria.relay import Relay
    from tests.integracao.broker import Broker


@pytest.fixture
def engine_instrumentado(engine: Engine) -> Engine:
    """O engine da suite com o instrumentador ligado, como no ``main`` dos processos."""
    instrumentar_banco(engine, provedor_dos_testes())
    return engine


@pytest.fixture
def comandos_sql(engine: Engine) -> Iterator[list[str]]:
    """Os comandos SQL que o engine executou durante o teste."""
    executados: list[str] = []

    def registrar(_conexao: object, _cursor: object, comando: str, *_: Any) -> None:
        executados.append(comando)

    event.listen(engine, "before_cursor_execute", registrar)
    yield executados
    event.remove(engine, "before_cursor_execute", registrar)


def _sql_dos_filhos(spans: InMemorySpanExporter, pai: ReadableSpan) -> str:
    """O texto dos comandos SQL dos spans filhos de ``pai``, em um bloco so."""
    return "\n".join(
        str((s.attributes or {}).get("db.statement", ""))
        for s in spans.get_finished_spans()
        if s.parent is not None and s.parent.span_id == pai.context.span_id
    )


def _span(spans: InMemorySpanExporter, nome: str) -> ReadableSpan:
    (encontrado,) = [s for s in spans.get_finished_spans() if s.name == nome]
    return encontrado


def _criar_item(engine: Engine, sku: str, quantidade: int) -> None:
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "INSERT INTO itens_estoque (id, sku, nome, quantidade_disponivel, "
                "quantidade_reservada, ativo) VALUES (gen_random_uuid(), :sku, :sku, "
                ":quantidade, 0, true)"
            ),
            {"sku": sku, "quantidade": quantidade},
        )


def _reservar(ordem_id: object) -> dict[str, Any]:
    return envelope_de_comando(
        "ReservarPecas",
        {"ordem_id": str(ordem_id), "pecas": [{"sku": "PEC-VELA", "quantidade": 2}]},
    )


def test_relay_ocioso_nao_exporta_span_com_o_banco_instrumentado(
    engine_instrumentado: Engine,
    comandos_sql: list[str],
    relay: Callable[..., Relay],
    spans: InMemorySpanExporter,
) -> None:
    with EmSegundoPlano(relay()):
        # O claim do poll e a contagem do /metrics, sem nenhum span em volta.
        esperar_ate(lambda: len(comandos_sql) >= 5)
        assert REGISTRY.get_sample_value("outbox_pendentes") == 0
        assert REGISTRY.get_sample_value("outbox_dead") == 0

    assert any("FROM outbox" in comando for comando in comandos_sql)
    assert spans.get_finished_spans() == ()


def test_consumidor_ocioso_nao_exporta_span_com_o_banco_instrumentado(
    engine_instrumentado: Engine,
    comandos_sql: list[str],
    consumidor: Callable[..., Consumidor],
    spans: InMemorySpanExporter,
) -> None:
    # A limpeza das mensagens processadas e a conferencia do banco depois de um
    # erro rodam fora de qualquer mensagem: comandos de cliente sem pai.
    ocioso = consumidor()

    ocioso._limpar_processadas_antigas()
    assert ocioso._banco_responde() is True

    assert any("mensagens_processadas" in comando for comando in comandos_sql)
    assert spans.get_finished_spans() == ()


def test_saga_tem_um_trace_so_com_os_comandos_de_banco_do_consumidor_e_do_relay(
    broker: Broker,
    engine_instrumentado: Engine,
    consumidor: Callable[..., Consumidor],
    relay: Callable[..., Relay],
    spans: InMemorySpanExporter,
) -> None:
    _criar_item(engine_instrumentado, "PEC-VELA", 5)
    spans.clear()
    with tracer.start_as_current_span("publish ReservarPecas", kind=SpanKind.PRODUCER):
        broker.publicar_comando(_reservar(uuid4()), headers=contexto_atual())

    with EmSegundoPlano(consumidor()), EmSegundoPlano(relay()):
        esperar_ate(lambda: broker.pegar("os.eventos"))

    origem = _span(spans, "publish ReservarPecas")
    consumo = _span(spans, "process ReservarPecas")
    publicacao = _span(spans, "publish PecasReservadas")
    # O claim, o lease, a limpeza e os polls do relay nao deixam trace: tudo o que
    # saiu e da saga.
    assert {s.context.trace_id for s in spans.get_finished_spans()} == {
        origem.context.trace_id
    }
    sql_do_consumo = _sql_dos_filhos(spans, consumo)
    assert "INSERT INTO mensagens_processadas" in sql_do_consumo
    assert "FROM itens_estoque" in sql_do_consumo
    assert "INSERT INTO outbox" in sql_do_consumo
    # A marcacao da linha entregue e filha da publicacao.
    assert "SET status = 'entregue'" in _sql_dos_filhos(spans, publicacao)


def test_span_de_banco_do_consumo_nao_leva_o_valor_dos_parametros(
    broker: Broker,
    engine_instrumentado: Engine,
    consumidor: Callable[..., Consumidor],
    spans: InMemorySpanExporter,
) -> None:
    _criar_item(engine_instrumentado, "PEC-VELA", 5)
    ordem_id = uuid4()
    spans.clear()
    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(_reservar(ordem_id))
        esperar_ate(
            lambda: any(
                s.name == "process ReservarPecas" for s in spans.get_finished_spans()
            )
        )

    texto = "\n".join(tudo_que_os_spans_levam(spans.get_finished_spans()))
    assert "INSERT INTO reservas" in texto
    # O correlation_id do span de consumo e o unico lugar da ordem; o SKU, so no
    # valor dos parametros.
    consumo = _span(spans, "process ReservarPecas")
    sem_o_consumo = "\n".join(
        tudo_que_os_spans_levam(
            s for s in spans.get_finished_spans() if s is not consumo
        )
    )
    assert str(ordem_id) not in sem_o_consumo
    assert "PEC-VELA" not in texto
