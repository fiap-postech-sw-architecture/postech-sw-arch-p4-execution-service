"""Mensageria de ponta a ponta: RabbitMQ com a topologia do platform + PostgreSQL.

O teste faz o papel do OS Service: publica comandos em ``pytstop.comandos`` como
o usuario ``os`` e le os eventos que chegam a fila ``os.eventos``. Consumidor e
relay rodam de verdade, cada um na sua thread.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID, uuid4

import pika
import pytest
from opentelemetry.trace import SpanKind
from pika.exceptions import ChannelClosedByBroker, StreamLostError
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import src.consumidor
import src.relay
from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)
from src.compartilhado.infraestrutura.mensageria import relay as modulo_relay
from src.compartilhado.infraestrutura.mensageria.amqp import abrir_canal
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    NIVEIS_DE_RETRY,
    Consumidor,
)
from src.compartilhado.infraestrutura.mensageria.contratos import validar
from src.compartilhado.infraestrutura.mensageria.processo import (
    Backoff,
    SinaisDoProcesso,
)
from src.compartilhado.infraestrutura.mensageria.relay import Relay
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    tracer,
)
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.consumidor import HANDLERS
from src.estoque.aplicacao.use_cases import LiberarReserva
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)
from src.estoque.infraestrutura.seed import semear
from src.estoque.interfaces.comandos import reservar_pecas
from tests.integracao.broker import (
    TTL_DE_TESTE_MS,
    EmSegundoPlano,
    envelope_de_comando,
    esperar_ate,
)

if TYPE_CHECKING:
    import io
    from collections.abc import Callable, Mapping
    from pathlib import Path

    import respx
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from tests.integracao.broker import Broker

VEICULO_ID = UUID("3277db7e-8283-4dd9-89e7-df3eeacb8710")


@pytest.fixture
def sinais(tmp_path: Path) -> Callable[[str], SinaisDoProcesso]:
    def _sinais(nome: str) -> SinaisDoProcesso:
        return SinaisDoProcesso(
            tmp_path / f"{nome}-heartbeat", tmp_path / f"{nome}-pronto"
        )

    return _sinais


@pytest.fixture
def consumidor(
    broker: Broker,
    session_factory: sessionmaker[Session],
    sinais: Callable[[str], SinaisDoProcesso],
) -> Callable[..., Consumidor]:
    def _criar(handlers: Mapping[str, Any] = HANDLERS) -> Consumidor:
        return Consumidor(
            session_factory,
            broker.url("execucao"),
            handlers,
            sinais("consumidor"),
            Backoff(0.1, 0.5),
        )

    return _criar


@pytest.fixture
def relay(
    broker: Broker, engine: Engine, sinais: Callable[[str], SinaisDoProcesso]
) -> Callable[..., Relay]:
    def _criar(poll_s: float = 0.1) -> Relay:
        return Relay(
            engine,
            broker.url("execucao"),
            sinais("relay"),
            poll_s=poll_s,
            backoff=Backoff(0.1, 0.5),
        )

    return _criar


def consumidas(tipo: str, resultado: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total", {"tipo": tipo, "resultado": resultado}
    )
    return valor or 0.0


def _esperar_consumo(tipo: str, resultado: str, antes: float, vezes: int = 1) -> None:
    esperar_ate(lambda: consumidas(tipo, resultado) >= antes + vezes)


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


def _saldo(engine: Engine, sku: str) -> tuple[int, int]:
    with engine.connect() as conexao:
        linha = conexao.execute(
            text(
                "SELECT quantidade_disponivel, quantidade_reservada FROM itens_estoque "
                "WHERE sku = :sku"
            ),
            {"sku": sku},
        ).one()
    return linha.quantidade_disponivel, linha.quantidade_reservada


def _reservar(ordem_id: UUID, quantidade: int = 2) -> dict[str, Any]:
    pecas = [{"sku": "PEC-VELA", "quantidade": quantidade}] if quantidade else []
    return {"ordem_id": str(ordem_id), "pecas": pecas}


def _cabecalho_w3c(span: Any) -> str:
    contexto = span.get_span_context()
    return (
        f"00-{contexto.trace_id:032x}-{contexto.span_id:016x}-"
        f"{contexto.trace_flags:02x}"
    )


def _span(spans: InMemorySpanExporter, nome: str) -> Any:
    (encontrado,) = [s for s in spans.get_finished_spans() if s.name == nome]
    return encontrado


def test_comando_vira_evento_do_contrato_com_causa_e_trace_encadeados(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
    spans: InMemorySpanExporter,
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    ordem_id = uuid4()
    comando = envelope_de_comando("ReservarPecas", _reservar(ordem_id))
    # O OS publica dentro do proprio span; o traceparent vai no header.
    with tracer.start_as_current_span("publish ReservarPecas", kind=SpanKind.PRODUCER):
        broker.publicar_comando(comando, headers=contexto_atual())

    with EmSegundoPlano(consumidor()), EmSegundoPlano(relay()):
        props, corpo = esperar_ate(lambda: broker.pegar("os.eventos"))

    evento = json.loads(corpo)
    validar(evento)
    assert evento["tipo"] == "PecasReservadas"
    assert evento["origem"] == "execution-service"
    assert evento["correlation_id"] == str(ordem_id)
    assert evento["causation_id"] == comando["id"]
    assert evento["dados"]["ordem_id"] == str(ordem_id)
    assert (props.message_id, props.type) == (evento["id"], "PecasReservadas")
    assert (props.correlation_id, props.user_id) == (str(ordem_id), "execucao")
    assert (props.delivery_mode, props.content_type) == (2, "application/json")
    assert _saldo(engine, "PEC-VELA") == (5, 2)

    origem = _span(spans, "publish ReservarPecas")
    processo = _span(spans, "process ReservarPecas")
    publicacao = _span(spans, "publish PecasReservadas")
    assert processo.kind is SpanKind.CONSUMER
    assert publicacao.kind is SpanKind.PRODUCER
    assert processo.parent.span_id == origem.context.span_id
    assert publicacao.parent.span_id == processo.context.span_id
    assert {s.context.trace_id for s in (origem, processo, publicacao)} == {
        origem.context.trace_id
    }
    # O contexto gravado na outbox e o do consumidor; o publicado, o do relay.
    (linha,) = outbox()
    assert linha["traceparent"] == _cabecalho_w3c(processo)
    assert props.headers["traceparent"] == _cabecalho_w3c(publicacao)
    assert linha["status"] == "entregue"


def test_reentrega_do_mesmo_id_nao_repete_o_efeito(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    comando = envelope_de_comando("ReservarPecas", _reservar(uuid4()))
    antes = consumidas("ReservarPecas", "duplicada")

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(comando)
        broker.publicar_comando(comando)
        _esperar_consumo("ReservarPecas", "duplicada", antes)

    assert _saldo(engine, "PEC-VELA") == (5, 2)
    assert [linha["tipo"] for linha in outbox()] == ["PecasReservadas"]


def test_comando_repetido_com_id_novo_republica_o_desfecho_registrado(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # Reenvio do orquestrador no prazo tecnico: id novo, mesma ordem.
    _criar_item(engine, "PEC-VELA", 5)
    ordem_id = uuid4()
    original = envelope_de_comando("ReservarPecas", _reservar(ordem_id))
    reenvio = envelope_de_comando("ReservarPecas", _reservar(ordem_id))
    antes = consumidas("ReservarPecas", "processada")

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(original)
        broker.publicar_comando(reenvio)
        _esperar_consumo("ReservarPecas", "processada", antes, vezes=2)

    respostas = outbox()
    assert [linha["tipo"] for linha in respostas] == ["PecasReservadas"] * 2
    assert [linha["envelope"]["causation_id"] for linha in respostas] == [
        original["id"],
        reenvio["id"],
    ]
    assert respostas[0]["dados"] == respostas[1]["dados"]  # mesma reserva
    assert _saldo(engine, "PEC-VELA") == (5, 2)


def test_compensacao_antes_do_original_grava_lapide_e_o_original_e_descartado(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    ordem_id = uuid4()
    liberacao = envelope_de_comando(
        "LiberarReserva", {"ordem_id": str(ordem_id), "motivo": "cancelamento"}
    )
    reserva = envelope_de_comando("ReservarPecas", _reservar(ordem_id))
    antes = consumidas("ReservarPecas", "ignorada")

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(liberacao)
        esperar_ate(outbox)
        broker.publicar_comando(reserva)
        _esperar_consumo("ReservarPecas", "ignorada", antes)

    (resposta,) = outbox()
    assert resposta["tipo"] == "ReservaLiberada"
    assert resposta["envelope"]["causation_id"] == liberacao["id"]
    assert _saldo(engine, "PEC-VELA") == (5, 0)
    with engine.connect() as conexao:
        status = conexao.execute(
            text("SELECT status FROM reservas WHERE ordem_id = :o"), {"o": ordem_id}
        ).scalar_one()
    assert status == "LIBERADA"


def test_saga_cancelada_depois_do_agendamento_passa_por_todos_os_handlers(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    ordem_id = uuid4()
    ordem = {"ordem_id": str(ordem_id)}
    comandos = [
        envelope_de_comando(
            "SolicitarDiagnostico",
            {
                **ordem,
                "veiculo_id": str(VEICULO_ID),
                "veiculo": {
                    "placa": "BRA2E19",
                    "marca": "Volkswagen",
                    "modelo": "Gol",
                    "ano": 2019,
                },
                "descricao_problema": "Barulho na suspensao",
            },
        ),
        envelope_de_comando("ReservarPecas", _reservar(ordem_id)),
        envelope_de_comando("AgendarExecucao", {**ordem, "prioridade": "alta"}),
        envelope_de_comando("CancelarExecucao", {**ordem, "motivo": "cancelamento"}),
        envelope_de_comando("LiberarReserva", {**ordem, "motivo": "cancelamento"}),
        envelope_de_comando(
            "DescartarDiagnostico", {**ordem, "motivo": "cancelamento"}
        ),
        envelope_de_comando("AnonimizarVeiculo", {"veiculo_id": str(VEICULO_ID)}),
        # Repetido (outra eliminacao do mesmo veiculo): nada mais a trocar.
        envelope_de_comando("AnonimizarVeiculo", {"veiculo_id": str(VEICULO_ID)}),
    ]
    antes = consumidas("AnonimizarVeiculo", "processada")

    with EmSegundoPlano(consumidor()):
        # Em ordem, como o orquestrador manda: um comando por resposta.
        for comando in comandos:
            broker.publicar_comando(comando)
        _esperar_consumo("AnonimizarVeiculo", "processada", antes, vezes=2)

    respostas = [
        (linha["tipo"], linha["envelope"]["causation_id"]) for linha in outbox()
    ]
    assert respostas == [
        ("PecasReservadas", comandos[1]["id"]),
        ("ExecucaoAgendada", comandos[2]["id"]),
        ("ExecucaoCancelada", comandos[3]["id"]),
        ("ReservaLiberada", comandos[4]["id"]),
        ("DiagnosticoDescartado", comandos[5]["id"]),
    ]
    assert _saldo(engine, "PEC-VELA") == (5, 0)
    with engine.connect() as conexao:
        placas = conexao.execute(
            text(
                "SELECT veiculo ->> 'placa' FROM diagnosticos UNION ALL "
                "SELECT veiculo ->> 'placa' FROM execucoes"
            )
        ).scalars()
        assert list(placas) == [f"ANONIMIZADO:{VEICULO_ID}"] * 2


def test_comando_que_nao_corresponde_ao_estado_e_ignorado_sem_dlq(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # Sem reserva ativa a ordem nao entra na fila (RN-027): a regra de dominio
    # recusa, o comando nao volta e nao vai para a DLQ.
    agendamento = envelope_de_comando(
        "AgendarExecucao", {"ordem_id": str(uuid4()), "prioridade": "normal"}
    )
    antes = consumidas("AgendarExecucao", "ignorada")

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(agendamento)
        _esperar_consumo("AgendarExecucao", "ignorada", antes)

    assert outbox() == []
    assert broker.contar("execucao.comandos.dlq") == 0


class _FalhaTransitoria:
    """Handler que falha ``vezes`` com erro de banco e depois chama o real."""

    def __init__(self, vezes: int, real: Callable[..., None]) -> None:
        self.chamadas = 0
        self._vezes = vezes
        self._real = real

    def __call__(
        self, envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWork
    ) -> None:
        self.chamadas += 1
        if self.chamadas <= self._vezes:
            raise OperationalError("SELECT 1", {}, Exception("conexao caiu"))
        self._real(envelope, sessao, uow)


def test_erro_transitorio_passa_pelas_cinco_filas_de_retry_e_o_efeito_e_unico(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # Cinco falhas: a copia passa por retry.1s, 5s, 15s, 60s e 300s (TTL de
    # teste curto) e a sexta entrega processa.
    _criar_item(engine, "PEC-VELA", 5)
    handler = _FalhaTransitoria(5, reservar_pecas)
    antes = consumidas("ReservarPecas", "retry")

    with EmSegundoPlano(consumidor({"ReservarPecas": handler})):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        esperar_ate(outbox)

    assert handler.chamadas == 6
    assert consumidas("ReservarPecas", "retry") == antes + 5
    assert _saldo(engine, "PEC-VELA") == (5, 2)
    assert broker.contar("execucao.comandos.dlq") == 0


def test_copia_vai_para_a_fila_de_retry_do_nivel_sem_expiration(
    broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    # A fila do segundo nivel segura a copia para o teste ler: a primeira falha
    # passou pela retry.1s e voltou; a segunda para na retry.5s.
    comando = envelope_de_comando("ReservarPecas", _reservar(uuid4()))
    handler = _FalhaTransitoria(2, reservar_pecas)
    antes = consumidas("ReservarPecas", "retry")
    segundo_nivel = "execucao.comandos.retry.5s"
    broker.redeclarar_retry(segundo_nivel, 600_000)
    try:
        with EmSegundoPlano(consumidor({"ReservarPecas": handler})):
            with tracer.start_as_current_span("publish ReservarPecas"):
                cabecalho = contexto_atual()
            broker.publicar_comando(comando, headers=cabecalho)
            _esperar_consumo("ReservarPecas", "retry", antes, vezes=2)
        props, corpo = esperar_ate(lambda: broker.pegar(segundo_nivel))
    finally:
        broker.redeclarar_retry(segundo_nivel, TTL_DE_TESTE_MS)

    assert json.loads(corpo) == comando
    assert props.expiration is None
    assert props.headers["x-tentativa"] == 2
    assert props.headers["traceparent"] == cabecalho["traceparent"]
    assert (props.user_id, props.message_id) == ("execucao", comando["id"])
    assert broker.contar("execucao.comandos") == 0  # a original recebeu ack


def test_chave_de_retry_fora_das_filas_de_atraso_e_recusada_pelo_broker(
    broker: Broker,
) -> None:
    # Permissao de topico do usuario execucao: so as cinco filas de retry dele.
    with (
        pytest.raises(ChannelClosedByBroker) as recusa,
        broker.canal("execucao") as canal,
    ):
        canal.basic_publish(
            exchange="pytstop.retry",
            routing_key="execucao.comandos",
            body=b"{}",
            properties=pika.BasicProperties(user_id="execucao"),
        )
    assert recusa.value.reply_code == 403


def test_falha_depois_da_quinta_copia_vai_para_a_dlq(
    broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    handler = _FalhaTransitoria(99, reservar_pecas)
    antes = consumidas("ReservarPecas", "dlq")

    with EmSegundoPlano(consumidor({"ReservarPecas": handler})):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        _esperar_consumo("ReservarPecas", "dlq", antes)

    assert handler.chamadas == 6  # a primeira entrega e as cinco copias
    props, _ = esperar_ate(lambda: broker.pegar("execucao.comandos.dlq"))
    assert props.headers["x-tentativa"] == 5


@pytest.mark.parametrize(
    ("tipo", "dados", "versao"),
    [
        pytest.param(
            "ReservarPecas",
            {"ordem_id": str(uuid4()), "pecas": [{"sku": "PEC-VELA", "quantidade": 0}]},
            1,
            id="dados-fora-do-schema",
        ),
        pytest.param(
            "ReservarPecas", _reservar(UUID(int=7)), 2, id="versao-desconhecida"
        ),
        pytest.param(
            "GerarOrcamento",
            {"ordem_id": str(uuid4()), "itens": []},
            1,
            id="tipo-de-outro-servico",
        ),
        pytest.param(
            "SolicitarDiagnostico",
            {
                "ordem_id": str(uuid4()),
                "veiculo_id": str(VEICULO_ID),
                "veiculo": {
                    "placa": "BRA2E19",
                    "marca": "VW",
                    "modelo": "Gol",
                    "ano": 3000,
                },
                "descricao_problema": "x",
            },
            1,
            id="valor-recusado-pelo-dominio",
        ),
    ],
)
def test_erro_permanente_vai_direto_para_a_dlq(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
    tipo: str,
    dados: dict[str, Any],
    versao: int,
) -> None:
    comando = envelope_de_comando(tipo, dados, versao=versao)

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(comando)
        props, corpo = esperar_ate(lambda: broker.pegar("execucao.comandos.dlq"))

    assert json.loads(corpo) == comando
    assert "x-tentativa" not in (props.headers or {})
    assert outbox() == []
    assert [broker.contar(fila) for fila in NIVEIS_DE_RETRY] == [0] * 5


def test_mensagem_de_quem_nao_e_o_orquestrador_vai_direto_para_a_dlq(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    forjado = envelope_de_comando("ReservarPecas", _reservar(uuid4()))
    # A credencial da Execucao pode por mensagem na propria fila pela retry, mas
    # sem x-tentativa a origem nao confere: nao e o orquestrador.
    with broker.canal("execucao") as canal:
        canal.basic_publish(
            exchange="pytstop.retry",
            routing_key="execucao.comandos.retry.1s",
            body=json.dumps(forjado).encode(),
            properties=pika.BasicProperties(user_id="execucao", type="ReservarPecas"),
            mandatory=True,
        )
    do_admin = envelope_de_comando("ReservarPecas", _reservar(uuid4()))

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(do_admin, usuario="admin")
        esperar_ate(lambda: broker.contar("execucao.comandos.dlq") == 2)

    assert _saldo(engine, "PEC-VELA") == (5, 0)


def test_segunda_entrega_simultanea_do_mesmo_comando_desfaz_o_proprio_efeito(
    broker: Broker,
    engine: Engine,
    session_factory: sessionmaker[Session],
    consumidor: Callable[..., Consumidor],
) -> None:
    # Outra copia do mesmo id comitou enquanto esta rodava: a chave primaria de
    # mensagens_processadas desfaz esta transacao inteira.
    _criar_item(engine, "PEC-VELA", 5)
    comando = envelope_de_comando("ReservarPecas", _reservar(uuid4()))

    def concorrente(
        envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWork
    ) -> None:
        with session_factory() as outra:
            copia = SQLAlchemyUnitOfWork(
                lambda: outra, mensagem_de_origem=UUID(comando["id"])
            )
            with copia:
                copia.commit()
        reservar_pecas(envelope, sessao, uow)

    antes = consumidas("ReservarPecas", "duplicada")
    with EmSegundoPlano(consumidor({"ReservarPecas": concorrente})):
        broker.publicar_comando(comando)
        _esperar_consumo("ReservarPecas", "duplicada", antes)

    assert _saldo(engine, "PEC-VELA") == (5, 0)


def test_consumidor_reconecta_quando_o_broker_derruba_a_conexao(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
    tmp_path: Path,
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    with EmSegundoPlano(consumidor()):
        pronto = tmp_path / "consumidor-pronto"
        esperar_ate(pronto.exists)
        assert (tmp_path / "consumidor-heartbeat").exists()
        broker.rabbitmqctl("close_all_connections", "teste")
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        esperar_ate(outbox)
    assert not pronto.exists()  # encerramento gracioso tira a prontidao


def _linha_pendente(session_factory: sessionmaker[Session]) -> UUID:
    """Grava uma ReservaLiberada na outbox (lapide de uma ordem nova)."""
    ordem_id = uuid4()
    with session_factory() as sessao:
        LiberarReserva(
            ItemEstoqueSQLAlchemyRepository(sessao),
            ReservaSQLAlchemyRepository(sessao),
            SQLAlchemyUnitOfWork(lambda: sessao),
        ).executar(ordem_id)
    return ordem_id


def test_relay_acorda_pelo_notify_sem_esperar_o_poll(
    broker: Broker,
    engine: Engine,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    tmp_path: Path,
) -> None:
    processo = EmSegundoPlano(relay(poll_s=60))
    with processo:
        esperar_ate((tmp_path / "relay-pronto").exists)
        _linha_pendente(session_factory)
        esperar_ate(lambda: broker.pegar("os.eventos"), prazo_s=10)
        # Acorda o select para o relay ver o pedido de parada.
        processo.parar.set()
        with engine.begin() as conexao:
            conexao.execute(text("SELECT pg_notify('outbox_novo', '')"))


def test_mensagem_sem_rota_conta_tentativa_e_nao_vira_entregue(
    broker: Broker,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    with broker.canal() as canal:
        canal.queue_unbind("os.eventos", "pytstop.eventos", "evento.execucao.#")
    try:
        _linha_pendente(session_factory)
        with EmSegundoPlano(relay()):
            esperar_ate(lambda: outbox()[0]["tentativas"] == 1)
    finally:
        with broker.canal() as canal:
            canal.queue_bind("os.eventos", "pytstop.eventos", "evento.execucao.#")

    (linha,) = outbox()
    assert linha["status"] == "pendente"
    assert linha["ultimo_erro"] == "UnroutableError"


def test_quinta_falha_de_publicacao_vira_dead(
    broker: Broker,
    engine: Engine,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    _linha_pendente(session_factory)
    with engine.begin() as conexao:
        conexao.execute(text("UPDATE outbox SET tentativas = 4"))
    with broker.canal() as canal:
        canal.queue_unbind("os.eventos", "pytstop.eventos", "evento.execucao.#")
    try:
        with EmSegundoPlano(relay()):
            esperar_ate(lambda: outbox()[0]["status"] == "dead")
    finally:
        with broker.canal() as canal:
            canal.queue_bind("os.eventos", "pytstop.eventos", "evento.execucao.#")
    assert outbox()[0]["tentativas"] == 5
    assert REGISTRY.get_sample_value("outbox_dead") == 1


def test_broker_parado_nao_gasta_tentativa_e_a_entrega_sai_na_volta(
    broker: Broker,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
    tmp_path: Path,
) -> None:
    pronto = tmp_path / "relay-pronto"
    with EmSegundoPlano(relay()):
        esperar_ate(pronto.exists)
        broker.rabbitmqctl("stop_app")
        try:
            esperar_ate(lambda: not pronto.exists())
            _linha_pendente(session_factory)
            time.sleep(1)  # o relay tenta reconectar varias vezes nesse meio tempo
            (linha,) = outbox()
            assert (linha["status"], linha["tentativas"]) == ("pendente", 0)
        finally:
            broker.rabbitmqctl("start_app")
        esperar_ate(lambda: outbox()[0]["status"] == "entregue", prazo_s=60)
    assert outbox()[0]["tentativas"] == 0
    assert esperar_ate(lambda: broker.pegar("os.eventos"))


def test_retencao_apaga_entregues_e_processadas_antigas(
    broker: Broker,
    engine: Engine,
    relay: Callable[..., Relay],
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    agora = datetime.now(UTC)
    with engine.begin() as conexao:
        for dias, status in [(8, "entregue"), (6, "entregue"), (30, "dead")]:
            conexao.execute(
                text(
                    "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
                    "routing_key, envelope, status, entregue_em) VALUES "
                    "(gen_random_uuid(), 'ReservaLiberada', gen_random_uuid(), "
                    "'pytstop.eventos', 'evento.execucao.reserva_liberada', '{}', "
                    ":status, :quando)"
                ),
                {"status": status, "quando": agora - timedelta(days=dias)},
            )
        for dias in (31, 29):
            conexao.execute(
                text(
                    "INSERT INTO mensagens_processadas (mensagem_id, processada_em) "
                    "VALUES (gen_random_uuid(), :quando)"
                ),
                {"quando": agora - timedelta(days=dias)},
            )

    def processadas() -> int:
        with engine.connect() as conexao:
            return int(
                conexao.execute(
                    text("SELECT count(*) FROM mensagens_processadas")
                ).scalar_one()
            )

    with EmSegundoPlano(relay()), EmSegundoPlano(consumidor()):
        esperar_ate(lambda: len(outbox()) == 2)
        esperar_ate(lambda: processadas() == 1)

    assert sorted(linha["status"] for linha in outbox()) == ["dead", "entregue"]


@pytest.mark.parametrize("modulo", [src.relay, src.consumidor])
def test_processo_sobe_com_o_ambiente_e_para_no_sinal(
    modulo: Any,
    broker: Broker,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parado = threading.Event()
    parado.set()
    chamadas: list[str] = []
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("RABBITMQ_URL", broker.url("execucao"))
    monkeypatch.setattr(modulo, "parada_por_sinal", lambda: parado)
    monkeypatch.setattr(modulo, "servir_metricas", lambda: chamadas.append("metricas"))
    monkeypatch.setattr(modulo, "configurar_logging", lambda: chamadas.append("log"))
    monkeypatch.setattr(
        modulo, "configurar_telemetria", lambda: chamadas.append("trace")
    )

    modulo.main()

    assert chamadas == ["log", "trace", "metricas"]


class _BrokerQueCai:
    """Primeira conexao cai na primeira publicacao; a segunda publica tudo."""

    conexoes = 0
    publicadas: ClassVar[list[str]] = []
    ao_publicar: ClassVar[list[Callable[[], None]]] = []

    def __init__(self, _url: str) -> None:
        type(self).conexoes += 1
        self._numero = type(self).conexoes

    def publicar(self, linha: Any) -> None:
        if self._numero == 1:
            raise StreamLostError("conexao perdida")
        type(self).publicadas.append(linha.envelope["id"])
        for acao in type(self).ao_publicar:
            acao()

    def manter_viva(self) -> None:
        pass

    def fechar(self) -> None:
        pass


@pytest.fixture
def broker_que_cai(monkeypatch: pytest.MonkeyPatch) -> type[_BrokerQueCai]:
    _BrokerQueCai.conexoes = 0
    _BrokerQueCai.publicadas = []
    _BrokerQueCai.ao_publicar = []
    monkeypatch.setattr(modulo_relay, "_Broker", _BrokerQueCai)
    return _BrokerQueCai


def test_queda_do_broker_no_meio_do_lote_devolve_as_linhas_sem_gastar_tentativa(
    engine: Engine,
    session_factory: sessionmaker[Session],
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_que_cai: type[_BrokerQueCai],
    tmp_path: Path,
) -> None:
    for _ in range(3):
        _linha_pendente(session_factory)
    relay = Relay(
        engine, "amqp://execucao:x@broker.test/%2F", sinais("relay"), poll_s=0.1,
        backoff=Backoff(2.0, 2.0),
    )  # fmt: skip
    processo = EmSegundoPlano(relay)
    # Parada pedida no meio do lote: o relay termina o lote e nao pega outro.
    broker_que_cai.ao_publicar.append(processo.parar.set)
    with processo:
        esperar_ate(lambda: broker_que_cai.conexoes == 1)
        esperar_ate(lambda: not (tmp_path / "relay-pronto").exists())
        with engine.connect() as conexao:
            devolvidas = conexao.execute(
                text(
                    "SELECT count(*) FROM outbox WHERE status = 'pendente' "
                    "AND tentativas = 0 AND proxima_tentativa_em <= now()"
                )
            ).scalar_one()
        # Lease devolvido e nenhuma tentativa gasta: valem de novo ja.
        assert devolvidas == 3
        esperar_ate(lambda: len(broker_que_cai.publicadas) == 3)

    assert [(linha["status"], linha["tentativas"]) for linha in outbox()] == [
        ("entregue", 0)
    ] * 3


def test_linha_travada_por_outra_replica_e_pulada(
    engine: Engine,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_que_cai: type[_BrokerQueCai],
) -> None:
    broker_que_cai.conexoes = 1  # a "primeira" conexao ja caiu: esta publica
    _linha_pendente(session_factory)
    _linha_pendente(session_factory)
    segunda = engine.connect()
    transacao = segunda.begin()

    def outra_replica_pega_a_segunda() -> None:
        if len(broker_que_cai.publicadas) == 1:
            # So a segunda: com OFFSET o Postgres travaria tambem a primeira.
            segunda.execute(
                text(
                    "SELECT 1 FROM outbox WHERE id = (SELECT max(id) FROM outbox) "
                    "FOR UPDATE"
                )
            )

    broker_que_cai.ao_publicar.append(outra_replica_pega_a_segunda)
    try:
        with EmSegundoPlano(relay()):
            esperar_ate(lambda: outbox()[0]["status"] == "entregue")
            time.sleep(0.3)  # varias voltas com a segunda travada
            assert outbox()[1]["status"] == "pendente"
            transacao.rollback()
            # O lease de 60 s venceu (a outra replica sumiu sem entregar).
            with engine.begin() as conexao:
                conexao.execute(text("UPDATE outbox SET proxima_tentativa_em = now()"))
            esperar_ate(lambda: outbox()[1]["status"] == "entregue")
    finally:
        segunda.close()
    assert len(broker_que_cai.publicadas) == 2


def test_banco_fora_tira_a_prontidao_e_o_gauge_vira_nan(
    broker: Broker, tmp_path: Path, log_capturado: io.StringIO
) -> None:
    morto = criar_engine("postgresql://x:y@127.0.0.1:1/nada")  # gitleaks:allow
    sinais = SinaisDoProcesso(tmp_path / "hb", tmp_path / "pronto")
    relay = Relay(morto, broker.url("execucao"), sinais, backoff=Backoff(0.05, 0.1))
    with EmSegundoPlano(relay):
        esperar_ate(lambda: "relay_dependency_unavailable" in log_capturado.getvalue())
        assert not (tmp_path / "pronto").exists()
        assert (tmp_path / "hb").exists()
    assert math.isnan(REGISTRY.get_sample_value("outbox_pendentes") or 0.0)
    assert '"dependencia": "database"' in log_capturado.getvalue()


def test_recusa_do_broker_conta_tentativa_e_o_canal_e_reaberto(
    broker: Broker,
    engine: Engine,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # A permissao de topico do usuario execucao so cobre evento.execucao.*:
    # o broker recusa (403) e fecha o canal.
    _linha_pendente(session_factory)
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "UPDATE outbox SET routing_key = 'evento.billing.pagamento_confirmado'"
            )
        )
    with EmSegundoPlano(relay()):
        esperar_ate(lambda: outbox()[0]["tentativas"] == 1)
        _linha_pendente(session_factory)
        esperar_ate(lambda: len(outbox()) == 2 and outbox()[1]["status"] == "entregue")

    recusada = outbox()[0]
    assert recusada["status"] == "pendente"
    # Texto fixo: classe e codigo, nada do que o broker devolveu.
    assert recusada["ultimo_erro"] == "ChannelClosedByBroker (403)"


@pytest.mark.parametrize(
    ("exchanges", "filas"),
    [
        pytest.param(("pytstop.comandos",), (), id="exchange-de-outro-servico"),
        pytest.param((), ("execucao.comandos.dlq",), id="dlq"),
        pytest.param(("pytstop.inexistente",), (), id="exchange-inexistente"),
    ],
)
def test_conferencia_da_topologia_fora_do_alcance_falha_e_fecha_a_conexao(
    broker: Broker, exchanges: tuple[str, ...], filas: tuple[str, ...]
) -> None:
    with pytest.raises(ChannelClosedByBroker):
        abrir_canal(broker.url("execucao"), exchanges=exchanges, filas=filas)


def test_limpeza_que_falha_nao_derruba_o_consumidor(
    broker: Broker, tmp_path: Path, log_capturado: io.StringIO
) -> None:
    morto = criar_session_factory(criar_engine("postgresql://x:y@127.0.0.1:1/nada"))
    consumidor = Consumidor(
        morto,
        broker.url("execucao"),
        HANDLERS,
        SinaisDoProcesso(tmp_path / "hb", tmp_path / "pronto"),
    )
    with EmSegundoPlano(consumidor):
        esperar_ate((tmp_path / "pronto").exists)
    assert "processed_messages_cleanup_failed" in log_capturado.getvalue()


def test_consumidor_assina_de_novo_quando_o_broker_cancela_a_assinatura(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
    tmp_path: Path,
) -> None:
    # Fila apagada (ou failover): o broker manda Basic.Cancel e o pika nao
    # levanta erro; sem tratar, o processo seguiria pronto sem consumir nada.
    _criar_item(engine, "PEC-VELA", 5)
    pronto = tmp_path / "consumidor-pronto"
    with EmSegundoPlano(consumidor()):
        esperar_ate(pronto.exists)
        with broker.canal() as canal:
            canal.queue_delete("execucao.comandos")
        try:
            esperar_ate(lambda: not pronto.exists())
        finally:
            with broker.canal() as canal:
                canal.queue_declare(
                    "execucao.comandos",
                    durable=True,
                    arguments={"x-queue-type": "quorum"},
                )
                canal.queue_bind(
                    "execucao.comandos", "pytstop.comandos", "comando.execucao.#"
                )
        esperar_ate(pronto.exists)
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        esperar_ate(outbox)


def test_fatos_do_mecanico_levam_a_causa_do_comando_que_abriu_o_fluxo(
    api: TestClient,
    autenticar: Callable[..., dict[str, str]],
    billing: respx.MockRouter,
    broker: Broker,
    session_factory: sessionmaker[Session],
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # O orquestrador casa as respostas pelo causation_id: o que o mecanico faz
    # pela API responde ao SolicitarDiagnostico ou ao AgendarExecucao.
    semear(session_factory)
    billing.post("/api/v1/precos/validacao").respond(200, json={"invalidos": []})
    mecanico = autenticar("mecanico", uuid4())
    ordem_id = uuid4()
    ordem = {"ordem_id": str(ordem_id)}
    solicitacao = envelope_de_comando(
        "SolicitarDiagnostico",
        {
            **ordem,
            "veiculo_id": str(VEICULO_ID),
            "veiculo": {
                "placa": "BRA2E19",
                "marca": "VW",
                "modelo": "Gol",
                "ano": 2019,
            },
            "descricao_problema": "Freio chiando",
        },
    )
    reserva = envelope_de_comando(
        "ReservarPecas",
        {**ordem, "pecas": [{"sku": "PEC-PASTILHA-FREIO", "quantidade": 1}]},
    )
    agendamento = envelope_de_comando(
        "AgendarExecucao", {**ordem, "prioridade": "normal"}
    )
    repetido = envelope_de_comando("AgendarExecucao", {**ordem, "prioridade": "alta"})
    diagnostico = f"/api/v1/diagnosticos/{ordem_id}"
    execucao = f"/api/v1/execucoes/{ordem_id}"

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(solicitacao)
        esperar_ate(
            lambda: api.post(f"{diagnostico}/inicio", headers=mecanico).is_success
        )
        conclusao = api.post(
            f"{diagnostico}/conclusao",
            headers=mecanico,
            json={
                "itens": [
                    {"tipo": "peca", "codigo": "PEC-PASTILHA-FREIO", "quantidade": 1}
                ],
                "observacoes": "",
            },
        )
        assert conclusao.status_code == 200, conclusao.text
        for comando in (reserva, agendamento, repetido):
            broker.publicar_comando(comando)
        esperar_ate(lambda: len(outbox()) == 5)
    assert api.post(f"{execucao}/inicio", headers=mecanico).status_code == 200
    assert api.post(f"{execucao}/finalizacao", headers=mecanico).status_code == 200

    causas = [(linha["tipo"], linha["envelope"]["causation_id"]) for linha in outbox()]
    assert causas == [
        ("DiagnosticoIniciado", solicitacao["id"]),
        ("DiagnosticoConcluido", solicitacao["id"]),
        ("PecasReservadas", reserva["id"]),
        ("ExecucaoAgendada", agendamento["id"]),
        # Reenvio com id novo: a resposta republicada leva o id do reenvio...
        ("ExecucaoAgendada", repetido["id"]),
        # ...e o fato do mecanico, o do comando que abriu a execucao.
        ("ExecucaoIniciada", agendamento["id"]),
        ("ExecucaoFinalizada", agendamento["id"]),
    ]
