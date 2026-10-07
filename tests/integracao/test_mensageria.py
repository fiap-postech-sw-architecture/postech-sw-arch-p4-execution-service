"""Mensageria de ponta a ponta: RabbitMQ com a topologia do platform + PostgreSQL.

O teste faz o papel do OS Service: publica comandos em ``pytstop.comandos`` como
o usuario ``os`` e le os eventos que chegam a fila ``os.eventos``. Consumidor e
relay rodam de verdade, cada um na sua thread.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pika
import pytest
from opentelemetry.trace import SpanKind
from pika.exceptions import ChannelClosedByBroker
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import src.consumidor
import src.relay
from src.compartilhado.dominio.exceptions import DependenciaIndisponivelException
from src.compartilhado.dominio.veiculo import TEXTO_ELIMINADO, Veiculo
from src.compartilhado.infraestrutura.database import (
    criar_engine,
)
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.amqp import abrir_canal
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    NIVEIS_DE_RETRY,
    Consumidor,
    Resultado,
)
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, validar
from src.compartilhado.infraestrutura.mensageria.processo import (
    Backoff,
    SinaisDoProcesso,
)
from src.compartilhado.infraestrutura.mensageria.relay import Relay
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    tracer,
)
from src.compartilhado.infraestrutura.outbox_mapping import registrar_processada
from src.consumidor import HANDLERS
from src.diagnostico.dominio.diagnostico import Diagnostico, ItemDiagnostico, TipoItem
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.estoque.infraestrutura.seed import semear
from src.estoque.interfaces.comandos import reservar_pecas
from src.execucao.dominio.execucao import Execucao, Prioridade
from src.execucao.infraestrutura.repository import ExecucaoSQLAlchemyRepository
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

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWorkDoComando
    from tests.integracao.broker import Broker

VEICULO_ID = UUID("3277db7e-8283-4dd9-89e7-df3eeacb8710")


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


def _solicitacao(ordem_id: UUID) -> dict[str, Any]:
    return {
        "ordem_id": str(ordem_id),
        "veiculo_id": str(VEICULO_ID),
        "veiculo": {"placa": "BRA2E19", "marca": "VW", "modelo": "Gol", "ano": 2019},
        "descricao_problema": "Barulho na suspensao",
    }


@pytest.mark.parametrize(
    ("compensacao", "original", "dados", "resposta", "tabela", "status"),
    [
        pytest.param(
            "LiberarReserva",
            "ReservarPecas",
            lambda ordem: _reservar(ordem),
            "ReservaLiberada",
            "reservas",
            "LIBERADA",
            id="reserva",
        ),
        pytest.param(
            "CancelarExecucao",
            "AgendarExecucao",
            lambda ordem: {"ordem_id": str(ordem), "prioridade": "normal"},
            "ExecucaoCancelada",
            "execucoes",
            "CANCELADA",
            id="execucao",
        ),
        pytest.param(
            "DescartarDiagnostico",
            "SolicitarDiagnostico",
            _solicitacao,
            "DiagnosticoDescartado",
            "diagnosticos",
            "DESCARTADO",
            id="diagnostico",
        ),
    ],
)
def test_compensacao_antes_do_original_grava_lapide_e_o_original_e_descartado(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
    compensacao: str,
    original: str,
    dados: Callable[[UUID], dict[str, Any]],
    resposta: str,
    tabela: str,
    status: str,
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    ordem_id = uuid4()
    lapide = envelope_de_comando(
        compensacao, {"ordem_id": str(ordem_id), "motivo": "cancelamento"}
    )
    atrasado = envelope_de_comando(original, dados(ordem_id))
    antes = consumidas(original, "ignorada")

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(lapide)
        esperar_ate(outbox)
        broker.publicar_comando(atrasado)
        _esperar_consumo(original, "ignorada", antes)

    (linha,) = outbox()
    assert (linha["tipo"], linha["envelope"]["causation_id"]) == (
        resposta,
        lapide["id"],
    )
    assert _saldo(engine, "PEC-VELA") == (5, 0)
    with engine.connect() as conexao:
        gravado = conexao.execute(
            text(f"SELECT status FROM {tabela} WHERE ordem_id = :o"),  # noqa: S608 - tabela do parametro do teste
            {"o": ordem_id},
        ).scalar_one()
    assert gravado == status


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
    processadas = consumidas("AnonimizarVeiculo", "processada")
    ignoradas = consumidas("AnonimizarVeiculo", "ignorada")

    with EmSegundoPlano(consumidor()):
        # Em ordem, como o orquestrador manda: um comando por resposta.
        for comando in comandos:
            broker.publicar_comando(comando)
        _esperar_consumo("AnonimizarVeiculo", "processada", processadas)
        # O repetido nao tem o que trocar: descartado.
        _esperar_consumo("AnonimizarVeiculo", "ignorada", ignoradas)

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


def _diagnostico_concluido(
    session_factory: sessionmaker[Session], veiculo_id: UUID, placa: str
) -> UUID:
    """Diagnostico concluido com texto livre do titular e a copia na execucao."""
    ordem_id, mecanico, agora = uuid4(), uuid4(), datetime.now(UTC)
    veiculo = Veiculo(
        veiculo_id=veiculo_id, placa=placa, marca="VW", modelo="Gol", ano=2019
    )
    diagnostico = Diagnostico.solicitar(
        ordem_id=ordem_id,
        veiculo=veiculo,
        descricao_problema=f"Barulho; dona Maria Souza, placa {placa}",
        agora=agora,
        solicitacao_id=uuid4(),
    )
    diagnostico.iniciar(mecanico, agora)
    diagnostico.concluir(
        mecanico,
        [ItemDiagnostico(tipo=TipoItem.SERVICO, codigo="SRV-REVISAO", quantidade=1)],
        "Cliente Maria Souza (rua das Flores, 10)",
        agora,
    )
    execucao = Execucao.agendar(
        ordem_id=ordem_id,
        prioridade=Prioridade.NORMAL,
        veiculo=veiculo,
        agora=agora,
        agendamento_id=uuid4(),
    )
    execucao.cancelar(agora)
    with session_factory() as sessao:
        DiagnosticoSQLAlchemyRepository(sessao).salvar(diagnostico)
        ExecucaoSQLAlchemyRepository(sessao).salvar(execucao)
        sessao.commit()
    return ordem_id


def _concluido_na_outbox(engine: Engine, ordem_id: UUID, status: str) -> None:
    envelope = json.loads(
        (CONTRATOS / "exemplos" / "DiagnosticoConcluido.json").read_text()
    )
    envelope |= {"id": str(uuid4()), "correlation_id": str(ordem_id)}
    envelope["dados"] |= {
        "ordem_id": str(ordem_id),
        "observacoes": "Cliente Maria Souza (rua das Flores, 10)",
    }
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
                "routing_key, envelope, status) VALUES (:id, 'DiagnosticoConcluido', "
                ":ordem, 'pytstop.eventos', 'evento.execucao.diagnostico_concluido', "
                "CAST(:envelope AS jsonb), :status)"
            ),
            {
                "id": envelope["id"],
                "ordem": ordem_id,
                "envelope": json.dumps(envelope),
                "status": status,
            },
        )


def test_anonimizar_troca_so_o_veiculo_pedido_inclusive_nas_mensagens_guardadas(
    broker: Broker,
    engine: Engine,
    session_factory: sessionmaker[Session],
    consumidor: Callable[..., Consumidor],
    log_capturado: io.StringIO,
) -> None:
    outro_veiculo = uuid4()
    alvo = _diagnostico_concluido(session_factory, VEICULO_ID, "BRA2E19")
    vizinho = _diagnostico_concluido(session_factory, outro_veiculo, "RIO2A18")
    for status in ("pendente", "entregue", "dead"):
        _concluido_na_outbox(engine, alvo, status)
    _concluido_na_outbox(engine, vizinho, "pendente")
    antes = consumidas("AnonimizarVeiculo", "processada")

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(
            envelope_de_comando("AnonimizarVeiculo", {"veiculo_id": str(VEICULO_ID)})
        )
        _esperar_consumo("AnonimizarVeiculo", "processada", antes)

    with engine.connect() as conexao:
        diagnosticos = {
            linha.ordem_id: linha
            for linha in conexao.execute(
                text(
                    "SELECT ordem_id, veiculo ->> 'placa' AS placa, "
                    "descricao_problema, observacoes FROM diagnosticos"
                )
            )
        }
        execucoes = dict(
            conexao.execute(
                text("SELECT ordem_id, veiculo ->> 'placa' FROM execucoes")
            ).all()
        )
        mensagens = conexao.execute(
            text(
                "SELECT correlation_id, envelope #>> '{dados,observacoes}' "
                "FROM outbox ORDER BY id"
            )
        ).all()
    marcador = f"ANONIMIZADO:{VEICULO_ID}"
    assert diagnosticos[alvo].placa == execucoes[alvo] == marcador
    assert diagnosticos[alvo].descricao_problema == TEXTO_ELIMINADO
    assert diagnosticos[alvo].observacoes == TEXTO_ELIMINADO
    assert diagnosticos[vizinho].placa == execucoes[vizinho] == "RIO2A18"
    assert "Maria" in diagnosticos[vizinho].observacoes
    assert mensagens == [(alvo, TEXTO_ELIMINADO)] * 3 + [
        (vizinho, "Cliente Maria Souza (rua das Flores, 10)")
    ]
    (anonimizado,) = [
        json.loads(linha)
        for linha in log_capturado.getvalue().splitlines()
        if '"vehicle_anonymized"' in linha
    ]
    assert (anonimizado["retratos"], anonimizado["mensagens"]) == (2, 3)


def test_acao_do_mecanico_sai_no_trace_do_comando_que_abriu_o_passo(
    api: TestClient,
    autenticar: Callable[..., dict[str, str]],
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
    spans: InMemorySpanExporter,
) -> None:
    # O diagnostico guarda o contexto do consumo do SolicitarDiagnostico; o
    # DiagnosticoIniciado, gravado pela API, sai como filho dele e o relay o
    # publica no mesmo trace: a saga nao parte na espera pelo mecanico.
    ordem_id = uuid4()
    solicitacao = envelope_de_comando(
        "SolicitarDiagnostico",
        {
            "ordem_id": str(ordem_id),
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
    antes = consumidas("SolicitarDiagnostico", "processada")
    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(solicitacao)
        _esperar_consumo("SolicitarDiagnostico", "processada", antes)
    inicio = api.post(
        f"/api/v1/diagnosticos/{ordem_id}/inicio",
        headers=autenticar("mecanico", uuid4()),
    )
    assert inicio.status_code == 200, inicio.text
    with EmSegundoPlano(relay()):
        esperar_ate(lambda: outbox()[0]["status"] == "entregue")

    consumo = _span(spans, "process SolicitarDiagnostico")
    passo = _span(spans, "iniciar diagnostico")
    publicacao = _span(spans, "publish DiagnosticoIniciado")
    with engine.connect() as conexao:
        guardado = conexao.execute(
            text("SELECT traceparent FROM diagnosticos WHERE ordem_id = :o"),
            {"o": ordem_id},
        ).scalar_one()
    assert guardado == _cabecalho_w3c(consumo)
    assert passo.parent.span_id == consumo.context.span_id
    assert outbox()[0]["traceparent"] == _cabecalho_w3c(passo)
    assert publicacao.parent.span_id == passo.context.span_id
    assert {s.context.trace_id for s in (consumo, passo, publicacao)} == {
        consumo.context.trace_id
    }


def test_sku_fora_do_formato_do_billing_vira_faltante_e_nao_vai_para_a_dlq(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # O contrato aceita minusculas, ponto e sublinhado; o estoque so cadastra o
    # formato do Billing. A saga recebe a recusa e compensa, sem DLQ.
    _criar_item(engine, "PEC-VELA", 5)
    ordem_id = uuid4()
    comando = envelope_de_comando(
        "ReservarPecas",
        {
            "ordem_id": str(ordem_id),
            "pecas": [
                {"sku": "pec_vela.2", "quantidade": 1},
                {"sku": "PEC-VELA", "quantidade": 1},
            ],
        },
    )
    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(comando)
        (resposta,) = esperar_ate(outbox)

    assert resposta["tipo"] == "ReservaDePecasFalhou"
    assert resposta["dados"]["faltantes"] == [
        {"sku": "pec_vela.2", "solicitado": 1, "disponivel": 0}
    ]
    assert broker.contar("execucao.comandos.dlq") == 0
    assert _saldo(engine, "PEC-VELA") == (5, 0)


def test_solicitar_diagnostico_grava_o_retrato_e_a_descricao_do_comando(
    broker: Broker, engine: Engine, consumidor: Callable[..., Consumidor]
) -> None:
    ordem_id = uuid4()
    comando = envelope_de_comando(
        "SolicitarDiagnostico",
        {
            "ordem_id": str(ordem_id),
            "veiculo_id": str(VEICULO_ID),
            "veiculo": {
                "placa": "BRA2E19",
                "marca": "Fiat",
                "modelo": "Uno",
                "ano": 2011,
            },
            "descricao_problema": "Barulho no cambio",
        },
    )
    antes = consumidas("SolicitarDiagnostico", "processada")
    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(comando)
        _esperar_consumo("SolicitarDiagnostico", "processada", antes)
    with engine.connect() as conexao:
        linha = conexao.execute(
            text("SELECT veiculo, descricao_problema, solicitacao_id FROM diagnosticos")
        ).one()
    assert linha.veiculo == {
        "veiculo_id": str(VEICULO_ID),
        "placa": "BRA2E19",
        "marca": "Fiat",
        "modelo": "Uno",
        "ano": 2011,
    }
    assert linha.descricao_problema == "Barulho no cambio"
    assert str(linha.solicitacao_id) == comando["id"]


def test_agendar_execucao_grava_a_prioridade_do_comando(
    broker: Broker, engine: Engine, consumidor: Callable[..., Consumidor]
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    ordem_id = uuid4()
    antes = consumidas("AgendarExecucao", "processada")
    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(
            envelope_de_comando("SolicitarDiagnostico", _solicitacao(ordem_id))
        )
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(ordem_id))
        )
        broker.publicar_comando(
            envelope_de_comando(
                "AgendarExecucao", {"ordem_id": str(ordem_id), "prioridade": "alta"}
            )
        )
        _esperar_consumo("AgendarExecucao", "processada", antes)
    with engine.connect() as conexao:
        prioridade = conexao.execute(
            text("SELECT prioridade FROM execucoes")
        ).scalar_one()
    assert prioridade == "alta"


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
        self, envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
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
    # Mesmo trace do comando publicado: a copia e filha do span do consumo.
    assert (
        props.headers["traceparent"].split("-")[1]
        == (cabecalho["traceparent"].split("-")[1])
    )
    assert (props.user_id, props.message_id) == ("execucao", comando["id"])
    assert broker.contar("execucao.comandos") == 0  # a original recebeu ack


def test_copia_de_retry_sem_rota_leva_a_original_para_a_dlq(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # Sem a fila do primeiro nivel ligada, a copia volta do broker (mandatory):
    # a original nao leva ack sem copia, vai para a DLQ.
    _criar_item(engine, "PEC-VELA", 5)
    nivel = NIVEIS_DE_RETRY[0]
    comando = envelope_de_comando("ReservarPecas", _reservar(uuid4()))
    handler = _FalhaTransitoria(1, reservar_pecas)
    antes = consumidas("ReservarPecas", "dlq")
    with broker.canal() as canal:
        canal.queue_unbind(nivel, "pytstop.retry", nivel)
    try:
        with EmSegundoPlano(consumidor({"ReservarPecas": handler})):
            broker.publicar_comando(comando)
            _esperar_consumo("ReservarPecas", "dlq", antes)
    finally:
        with broker.canal() as canal:
            canal.queue_bind(nivel, "pytstop.retry", routing_key=nivel)

    props, corpo = esperar_ate(lambda: broker.pegar("execucao.comandos.dlq"))
    assert json.loads(corpo) == comando
    assert "x-tentativa" not in (props.headers or {})
    assert handler.chamadas == 1
    assert (outbox(), _saldo(engine, "PEC-VELA")) == ([], (5, 0))


def test_cada_copia_de_retry_vai_para_a_fila_do_proprio_nivel_no_broker_real(
    broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    # O x-death so guarda o ultimo salto: uma fila espia ligada as cinco chaves
    # de retry ve cada copia.
    espia = "execucao.comandos.espia-de-teste"
    with broker.canal() as canal:
        canal.queue_declare(espia, durable=True, arguments={"x-queue-type": "quorum"})
        canal.queue_bind(
            espia, "pytstop.retry", routing_key="execucao.comandos.retry.*"
        )
    antes = consumidas("ReservarPecas", "dlq")
    chaves: list[str] = []
    try:
        handler = _FalhaTransitoria(99, reservar_pecas)
        with EmSegundoPlano(consumidor({"ReservarPecas": handler})):
            broker.publicar_comando(
                envelope_de_comando("ReservarPecas", _reservar(uuid4()))
            )
            _esperar_consumo("ReservarPecas", "dlq", antes)
        with broker.canal() as canal:
            while True:
                metodo, _props, _corpo = canal.basic_get(espia, auto_ack=True)
                if metodo is None:
                    break
                chaves.append(metodo.routing_key)
    finally:
        with broker.canal() as canal:
            canal.queue_delete(espia)
    assert chaves == list(NIVEIS_DE_RETRY)


def test_copia_de_1s_nao_espera_a_de_300s_com_os_ttl_da_definicao(
    broker: Broker,
) -> None:
    # Uma fila por atraso: a copia de 300 s publicada antes nao segura a de 1 s
    # (numa fila so, com expiration, so expira quem esta na cabeca). Os TTL da
    # definition voltam so neste teste; o fixture os reduz para 100 ms.
    segura, rapida = "execucao.comandos.retry.300s", "execucao.comandos.retry.1s"
    broker.redeclarar_retry(segura, 300_000)
    broker.redeclarar_retry(rapida, 1_000)
    props = pika.BasicProperties(user_id="execucao", headers={"x-tentativa": 1})
    try:
        with broker.canal("execucao") as canal:
            canal.basic_publish("pytstop.retry", segura, b"{}", props, mandatory=True)
            canal.basic_publish("pytstop.retry", rapida, b"{}", props, mandatory=True)
        inicio = time.monotonic()
        esperar_ate(lambda: broker.pegar("execucao.comandos"), prazo_s=5)
        assert time.monotonic() - inicio < 4  # cerca de 1 s, nao 300 s
        assert broker.contar(segura) == 1
    finally:
        broker.redeclarar_retry(segura, TTL_DE_TESTE_MS)
        broker.redeclarar_retry(rapida, TTL_DE_TESTE_MS)


def test_chave_de_retry_fora_das_filas_de_atraso_e_recusada_pelo_broker(
    broker: Broker,
) -> None:
    # Permissao de topico do usuario execucao: so as cinco filas de retry dele.
    props = pika.BasicProperties(user_id="execucao")
    with (
        broker.canal("execucao") as canal,
        pytest.raises(ChannelClosedByBroker) as recusa,
    ):
        canal.basic_publish("pytstop.retry", "execucao.comandos", b"{}", props)
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


def test_header_que_o_pika_nao_le_vai_para_a_dlq_sem_levar_a_mensagem_de_tras(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # Cada entrega da venenosa derruba a conexao (o pika nao decodifica o
    # header). Com prefetch 1 so ela fica em voo, e o delivery-limit da fila a
    # manda para a DLQ; a valida, publicada atras, e processada.
    _criar_item(engine, "PEC-VELA", 5)
    venenosa = envelope_de_comando("ReservarPecas", _reservar(uuid4()))
    valida = envelope_de_comando("ReservarPecas", _reservar(uuid4()))
    broker.publicar_comando(venenosa, timestamp_em_ms=True)
    broker.publicar_comando(valida)

    with EmSegundoPlano(consumidor()):
        (resposta,) = esperar_ate(outbox, prazo_s=60)

    assert resposta["envelope"]["causation_id"] == valida["id"]
    assert broker.contar("execucao.comandos.dlq") == 1
    assert _saldo(engine, "PEC-VELA") == (5, 2)


def test_erro_nao_classificado_vai_direto_para_a_dlq_sem_passar_pelo_retry(
    broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    def defeito(*_: object) -> None:
        raise RuntimeError("defeito do handler")

    antes = consumidas("ReservarPecas", "dlq")
    with EmSegundoPlano(consumidor({"ReservarPecas": defeito})):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        _esperar_consumo("ReservarPecas", "dlq", antes)

    props, _ = esperar_ate(lambda: broker.pegar("execucao.comandos.dlq"))
    assert "x-tentativa" not in (props.headers or {})
    assert [broker.contar(fila) for fila in NIVEIS_DE_RETRY] == [0] * 5


def test_x_tentativa_acima_do_limite_vai_direto_para_a_dlq(
    broker: Broker, engine: Engine, consumidor: Callable[..., Consumidor]
) -> None:
    # Copia forjada com x-tentativa 6: nao roda o handler.
    _criar_item(engine, "PEC-VELA", 5)
    antes = consumidas("ReservarPecas", "dlq")
    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4())),
            headers={"x-tentativa": 6},
        )
        _esperar_consumo("ReservarPecas", "dlq", antes)
    assert _saldo(engine, "PEC-VELA") == (5, 0)


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(OSError("rede"), id="rede"),
        pytest.param(DependenciaIndisponivelException("fora"), id="dependencia-fora"),
    ],
)
def test_rede_e_dependencia_fora_passam_pelo_retry_no_broker_real(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
    erro: Exception,
) -> None:
    _criar_item(engine, "PEC-VELA", 5)
    tentativas: list[int] = []

    def falha_uma_vez(
        envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
    ) -> None:
        tentativas.append(1)
        if len(tentativas) == 1:
            raise erro
        reservar_pecas(envelope, sessao, uow)

    with EmSegundoPlano(consumidor({"ReservarPecas": falha_uma_vez})):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        esperar_ate(outbox)
    assert len(tentativas) == 2
    assert broker.contar("execucao.comandos.dlq") == 0


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


def _esperando_lock(engine: Engine) -> bool:
    # Uma leitura por transacao: pg_stat_activity e um retrato da transacao.
    with engine.connect() as conexao:
        esperando = conexao.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query LIKE 'INSERT INTO mensagens_processadas%'"
            )
        ).scalar_one()
    return bool(esperando)


def test_entrega_simultanea_do_mesmo_comando_espera_a_outra_e_nao_roda_o_handler(
    engine: Engine, consumidor: Callable[..., Consumidor]
) -> None:
    # Outra entrega do mesmo id gravou mensagens_processadas e ainda nao
    # comitou: esta espera na chave primaria e, com o commit da outra, responde
    # duplicada sem rodar o handler.
    _criar_item(engine, "PEC-VELA", 5)
    comando = envelope_de_comando("ReservarPecas", _reservar(uuid4()))
    handler = _FalhaTransitoria(0, reservar_pecas)
    resultados: list[Resultado] = []
    with engine.connect() as outra:
        transacao = outra.begin()
        assert registrar_processada(outra, UUID(comando["id"]))
        esta = threading.Thread(
            target=lambda: resultados.append(
                consumidor({"ReservarPecas": handler})._rodar_handler(comando)
            )
        )
        esta.start()
        esperar_ate(lambda: _esperando_lock(engine))
        transacao.commit()
        esta.join(timeout=10)

    assert resultados == [Resultado.DUPLICADA]
    assert handler.chamadas == 0
    assert _saldo(engine, "PEC-VELA") == (5, 0)


def _processadas(engine: Engine) -> list[UUID]:
    with engine.connect() as conexao:
        return list(
            conexao.execute(text("SELECT mensagem_id FROM mensagens_processadas"))
            .scalars()
            .all()
        )


def test_falha_depois_do_efeito_desfaz_efeito_resposta_e_idempotencia(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # O caso de uso reservou e registrou a resposta; o handler falha depois.
    # Quem comita e o consumidor: nada fica, e a mensagem vai para a DLQ.
    _criar_item(engine, "PEC-VELA", 5)

    def reserva_e_falha(
        envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
    ) -> None:
        reservar_pecas(envelope, sessao, uow)
        raise RuntimeError("falha depois do efeito")

    antes = consumidas("ReservarPecas", "dlq")
    with EmSegundoPlano(consumidor({"ReservarPecas": reserva_e_falha})):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        _esperar_consumo("ReservarPecas", "dlq", antes)

    assert _saldo(engine, "PEC-VELA") == (5, 0)
    assert outbox() == []
    assert _processadas(engine) == []


def test_handler_nao_comita_a_transacao_da_mensagem(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # A sessao do handler entra na transacao da mensagem por savepoint: o
    # commit dela nao comita nada; a falha seguinte desfaz tudo.
    _criar_item(engine, "PEC-VELA", 5)

    def reserva_comita_e_falha(
        envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
    ) -> None:
        reservar_pecas(envelope, sessao, uow)
        sessao.commit()
        raise RuntimeError("falha depois do commit do handler")

    antes = consumidas("ReservarPecas", "dlq")
    with EmSegundoPlano(consumidor({"ReservarPecas": reserva_comita_e_falha})):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        _esperar_consumo("ReservarPecas", "dlq", antes)

    assert _saldo(engine, "PEC-VELA") == (5, 0)
    assert outbox() == []
    assert _processadas(engine) == []


def test_comando_descartado_grava_o_id_e_a_reentrega_e_duplicada(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
) -> None:
    # ReservarPecas depois da lapide: sem efeito, mas o id fica gravado.
    ordem_id = uuid4()
    liberacao = envelope_de_comando(
        "LiberarReserva", {"ordem_id": str(ordem_id), "motivo": "cancelamento"}
    )
    atrasado = envelope_de_comando("ReservarPecas", _reservar(ordem_id, 0))
    ignoradas = consumidas("ReservarPecas", "ignorada")
    duplicadas = consumidas("ReservarPecas", "duplicada")

    with EmSegundoPlano(consumidor()):
        broker.publicar_comando(liberacao)
        broker.publicar_comando(atrasado)
        _esperar_consumo("ReservarPecas", "ignorada", ignoradas)
        broker.publicar_comando(atrasado)
        _esperar_consumo("ReservarPecas", "duplicada", duplicadas)

    assert set(_processadas(engine)) == {
        UUID(liberacao["id"]),
        UUID(atrasado["id"]),
    }


def test_handler_lento_dentro_do_teto_nao_derruba_a_conexao(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    log_capturado: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O handler roda na thread da conexao AMQP, sem heartbeat: mais lento que um
    # intervalo de heartbeat, mas dentro do teto, ele termina e a mensagem leva
    # ack na mesma conexao, sem reentrega.
    monkeypatch.setattr(amqp, "_HEARTBEAT_S", 4)
    _criar_item(engine, "PEC-VELA", 5)
    chamadas: list[int] = []

    def lento(
        envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
    ) -> None:
        chamadas.append(1)
        sessao.execute(text("SELECT pg_sleep(3)"))
        reservar_pecas(envelope, sessao, uow)

    antes = consumidas("ReservarPecas", "processada")
    with EmSegundoPlano(consumidor({"ReservarPecas": lento})):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        _esperar_consumo("ReservarPecas", "processada", antes)

    assert chamadas == [1]
    assert "consumer_broker_unavailable" not in log_capturado.getvalue()


def test_comando_preso_no_banco_e_cortado_pelo_teto_e_vira_retry(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
    log_capturado: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Comando que nao volta (lock, banco lento) e cortado pelo teto da transacao
    # da mensagem (5 s), antes de o broker dar a conexao por morta por falta de
    # heartbeat: vira copia de retry, sem reconexao nem reentrega fora da escada.
    monkeypatch.setattr(amqp, "_HEARTBEAT_S", 8)
    _criar_item(engine, "PEC-VELA", 5)
    chamadas: list[int] = []

    def preso_na_primeira(
        envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
    ) -> None:
        chamadas.append(1)
        if len(chamadas) == 1:
            sessao.execute(text("SELECT pg_sleep(60)"))
        reservar_pecas(envelope, sessao, uow)

    inicio = time.monotonic()
    with EmSegundoPlano(consumidor({"ReservarPecas": preso_na_primeira})):
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        esperar_ate(outbox, prazo_s=12)

    assert time.monotonic() - inicio < 12  # o teto do servidor, 15 s, nao chega
    assert chamadas == [1, 1]
    saida = log_capturado.getvalue()
    assert '"pgcode": "57014"' in saida  # query_canceled pelo statement_timeout
    assert "consumer_broker_unavailable" not in saida


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


@pytest.mark.parametrize(
    ("modulo", "processo"),
    [
        pytest.param(src.relay, "relay", id="relay"),
        pytest.param(src.consumidor, "consumidor", id="consumidor"),
    ],
)
def test_processo_sobe_com_o_ambiente_e_para_no_sinal(
    modulo: Any,
    processo: str,
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
        modulo, "configurar_telemetria", lambda nome: chamadas.append(f"trace {nome}")
    )

    modulo.main()

    assert chamadas == ["log", f"trace {processo}", "metricas"]


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
    url = broker.url("execucao")
    with pytest.raises(ChannelClosedByBroker):
        abrir_canal(url, exchanges=exchanges, filas=filas)


def test_limpeza_que_falha_nao_derruba_o_consumidor(
    broker: Broker, tmp_path: Path, log_capturado: io.StringIO
) -> None:
    morto = criar_engine("postgresql://x:y@127.0.0.1:1/nada")  # gitleaks:allow
    consumidor = Consumidor(
        morto,
        broker.url("execucao"),
        HANDLERS,
        SinaisDoProcesso(tmp_path / "hb", tmp_path / "pronto"),
    )
    with EmSegundoPlano(consumidor):
        esperar_ate((tmp_path / "pronto").exists)
    assert "processed_messages_cleanup_failed" in log_capturado.getvalue()


def test_banco_fora_para_o_consumo_em_vez_de_gastar_a_escada_de_retry(
    broker: Broker, tmp_path: Path, log_capturado: io.StringIO
) -> None:
    # Com o banco fora, cada comando falharia nas cinco filas de retry e iria
    # para a DLQ em 6 min: o consumidor para de consumir e sai da prontidao,
    # e a copia da primeira falha espera na fila.
    morto = criar_engine("postgresql://x:y@127.0.0.1:1/nada")  # gitleaks:allow
    consumidor = Consumidor(
        morto,
        broker.url("execucao"),
        HANDLERS,
        SinaisDoProcesso(tmp_path / "hb", tmp_path / "pronto"),
        Backoff(0.05, 0.1),
    )
    antes = consumidas("ReservarPecas", "retry")
    with EmSegundoPlano(consumidor):
        esperar_ate((tmp_path / "pronto").exists)
        broker.publicar_comando(
            envelope_de_comando("ReservarPecas", _reservar(uuid4()))
        )
        esperar_ate(lambda: "consumer_database_unavailable" in log_capturado.getvalue())
        esperar_ate(lambda: broker.contar("execucao.comandos") == 1)
        vistas = log_capturado.getvalue().count("consumer_database_unavailable")
        # Varias conferencias do banco depois, a copia segue na fila.
        esperar_ate(
            lambda: (
                log_capturado.getvalue().count("consumer_database_unavailable")
                >= vistas + 3
            )
        )
        assert broker.contar("execucao.comandos") == 1
        assert not (tmp_path / "pronto").exists()
    assert consumidas("ReservarPecas", "retry") == antes + 1


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
