from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

import pytest
from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState
from sqlalchemy.orm import Session

from src.compartilhado.aplicacao.integration_event import IntegrationEvent
from src.compartilhado.infraestrutura.mensageria.contratos import (
    MensagemInvalidaError,
    validar,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import tracer
from src.compartilhado.infraestrutura.unit_of_work import (
    SQLAlchemyUnitOfWork,
    TransacaoDaMensagem,
)
from src.estoque.aplicacao.events import ReservaLiberadaEvent


@dataclass(frozen=True, kw_only=True)
class ForaDoContratoEvent(IntegrationEvent):
    """Evento sem schema no contrato: so um defeito o gravaria."""


class _Tentativa:
    def __init__(self, chamadas: list[tuple[Any, ...]]) -> None:
        self._chamadas = chamadas

    def commit(self) -> None:
        self._chamadas.append(("liberar savepoint",))

    def rollback(self) -> None:
        self._chamadas.append(("voltar ao savepoint",))


class _SessaoFake:
    """Grava a sequencia de chamadas (a gravacao real e testada no Postgres)."""

    def __init__(self) -> None:
        self.chamadas: list[tuple[Any, ...]] = []

    def execute(self, stmt: object, params: object = None) -> None:
        self.chamadas.append(("execute", str(stmt), params))

    def begin_nested(self) -> _Tentativa:
        self.chamadas.append(("savepoint",))
        return _Tentativa(self.chamadas)

    def commit(self) -> None:
        self.chamadas.append(("commit",))

    def rollback(self) -> None:
        self.chamadas.append(("rollback",))

    def close(self) -> None:
        self.chamadas.append(("close",))


class _SessaoSemConexao(_SessaoFake):
    def rollback(self) -> None:
        super().rollback()
        raise ConnectionError


def _sessao(fake: _SessaoFake) -> Session:
    # Fake so com a superficie da Session que as unidades de trabalho usam.
    return cast("Session", fake)


def _uow(sessao: _SessaoFake | None = None) -> tuple[SQLAlchemyUnitOfWork, _SessaoFake]:
    fake = sessao or _SessaoFake()
    return SQLAlchemyUnitOfWork(lambda: _sessao(fake)), fake


def _w3c(contexto: SpanContext) -> str:
    return (
        f"00-{contexto.trace_id:032x}-{contexto.span_id:016x}-"
        f"{contexto.trace_flags:02x}"
    )


# --- API: o caso de uso comita ---------------------------------------------


def test_commit_sem_eventos_nao_toca_a_outbox() -> None:
    uow, sessao = _uow()
    with uow:
        uow.commit()
    assert sessao.chamadas == [("commit",), ("close",)]


def test_commit_grava_o_envelope_do_contrato_e_notifica_o_relay() -> None:
    uow, sessao = _uow()
    evento = ReservaLiberadaEvent(ordem_id=uuid4())
    with uow:
        uow.registrar_evento(evento)
        uow.commit()

    insert, notify, commit, close = sessao.chamadas
    assert insert[1].startswith("INSERT INTO outbox")
    (linha,) = insert[2]
    assert linha == {
        "mensagem_id": evento.id,
        "tipo": "ReservaLiberada",
        "correlation_id": evento.ordem_id,
        "exchange": "pytstop.eventos",
        "routing_key": "evento.execucao.reserva_liberada",
        "envelope": {
            "id": str(evento.id),
            "tipo": "ReservaLiberada",
            "versao": 1,
            "origem": "execution-service",
            "correlation_id": str(evento.ordem_id),
            "causation_id": None,
            "ocorrido_em": evento.ocorrido_em.isoformat(),
            "dados": {"ordem_id": str(evento.ordem_id)},
        },
        # Fora de span (requisicao sem trace): o relay abre um trace novo.
        "traceparent": None,
        "tracestate": None,
    }
    validar(linha["envelope"])
    assert notify[1:3] == ("SELECT pg_notify(:canal, '')", {"canal": "outbox_novo"})
    assert (commit, close) == (("commit",), ("close",))


def test_fato_do_mecanico_leva_a_causa_propria() -> None:
    abertura = uuid4()
    uow, sessao = _uow()
    with uow:
        uow.registrar_evento(
            ReservaLiberadaEvent(ordem_id=uuid4(), causation_id=abertura)
        )
        uow.commit()
    (linha,) = sessao.chamadas[0][2]
    assert linha["envelope"]["causation_id"] == str(abertura)


def _gravar(uow: SQLAlchemyUnitOfWork, evento: IntegrationEvent) -> None:
    with uow:
        uow.registrar_evento(evento)
        uow.commit()


def test_evento_fora_do_contrato_nao_e_gravado() -> None:
    uow, sessao = _uow()
    with pytest.raises(MensagemInvalidaError, match="tipo sem contrato"):
        _gravar(uow, ForaDoContratoEvent(ordem_id=uuid4()))
    assert sessao.chamadas == [("rollback",), ("close",)]


def test_eventos_sao_gravados_uma_vez_so() -> None:
    uow, sessao = _uow()
    with uow:
        uow.registrar_evento(ReservaLiberadaEvent(ordem_id=uuid4()))
        uow.commit()
        uow.commit()
    assert [c[0] for c in sessao.chamadas] == [
        "execute",
        "execute",
        "commit",
        "commit",
        "close",
    ]


def _falhar_no_meio(uow: SQLAlchemyUnitOfWork) -> None:
    with uow:
        uow.registrar_evento(ReservaLiberadaEvent(ordem_id=uuid4()))
        raise RuntimeError


def test_excecao_no_bloco_faz_rollback_e_descarta_eventos() -> None:
    uow, sessao = _uow()
    with pytest.raises(RuntimeError):
        _falhar_no_meio(uow)
    assert sessao.chamadas == [("rollback",), ("close",)]

    with uow:
        uow.commit()
    assert sessao.chamadas[-2:] == [("commit",), ("close",)]


def test_rollback_explicito_descarta_eventos() -> None:
    uow, sessao = _uow()
    with uow:
        uow.registrar_evento(ReservaLiberadaEvent(ordem_id=uuid4()))
        uow.rollback()
        uow.commit()
    assert sessao.chamadas == [("rollback",), ("commit",), ("close",)]


def test_commit_fora_do_with_e_erro_de_programacao() -> None:
    uow, _ = _uow()
    with pytest.raises(RuntimeError, match="nao foi iniciada"):
        uow.commit()


def test_rollback_que_falha_ainda_fecha_a_sessao() -> None:
    # Conexao caida no meio: sem o finally, a sessao ficaria aberta segurando a
    # conexao do pool.
    uow, sessao = _uow(_SessaoSemConexao())
    with pytest.raises(ConnectionError):
        _falhar_no_meio(uow)
    assert sessao.chamadas == [("rollback",), ("close",)]


# --- comando da saga: quem comita e o consumidor ------------------------------


def _transacao() -> tuple[TransacaoDaMensagem, _SessaoFake, Any]:
    fake, comando_id = _SessaoFake(), uuid4()
    return TransacaoDaMensagem(_sessao(fake), comando_id), fake, comando_id


def test_tentativa_guarda_o_efeito_e_as_respostas_sem_comitar() -> None:
    transacao, sessao, comando_id = _transacao()
    with tracer.start_as_current_span("process LiberarReserva") as span:
        with transacao:
            transacao.registrar_evento(ReservaLiberadaEvent(ordem_id=uuid4()))
        contexto = span.get_span_context()

    savepoint, insert, notify, liberar = sessao.chamadas
    assert (savepoint, liberar) == (("savepoint",), ("liberar savepoint",))
    assert insert[1].startswith("INSERT INTO outbox")
    assert notify[1] == "SELECT pg_notify(:canal, '')"
    (linha,) = insert[2]
    # A resposta leva o id do comando respondido e o contexto do consumidor.
    assert linha["envelope"]["causation_id"] == str(comando_id)
    assert linha["traceparent"] == _w3c(contexto)
    assert ("commit",) not in sessao.chamadas
    assert not transacao.descartado


def test_causa_propria_do_evento_vale_mais_que_o_comando_em_processamento() -> None:
    transacao, sessao, _ = _transacao()
    abertura = uuid4()
    with transacao:
        transacao.registrar_evento(
            ReservaLiberadaEvent(ordem_id=uuid4(), causation_id=abertura)
        )
    (linha,) = sessao.chamadas[1][2]
    assert linha["envelope"]["causation_id"] == str(abertura)


def _tentar_e_falhar(transacao: TransacaoDaMensagem) -> None:
    with transacao:
        transacao.registrar_evento(ReservaLiberadaEvent(ordem_id=uuid4()))
        raise RuntimeError


def test_tentativa_com_excecao_volta_ao_savepoint_e_perde_os_eventos() -> None:
    transacao, sessao, _ = _transacao()
    with pytest.raises(RuntimeError):
        _tentar_e_falhar(transacao)

    with transacao:
        pass
    assert sessao.chamadas == [
        ("savepoint",),
        ("voltar ao savepoint",),
        ("savepoint",),
        ("liberar savepoint",),
    ]


def test_resposta_fora_do_contrato_desfaz_a_tentativa() -> None:
    transacao, sessao, _ = _transacao()
    with pytest.raises(MensagemInvalidaError), transacao:
        transacao.registrar_evento(ForaDoContratoEvent(ordem_id=uuid4()))
    assert sessao.chamadas == [("savepoint",), ("voltar ao savepoint",)]


def test_descarte_vale_para_a_ultima_tentativa() -> None:
    transacao, _, _ = _transacao()
    with transacao:
        transacao.descartar()
    assert transacao.descartado
    with transacao:
        pass
    assert not transacao.descartado


def test_tracestate_do_contexto_corrente_vai_para_a_outbox_junto_do_traceparent() -> (
    None
):
    pai = SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        is_remote=True,
        trace_flags=TraceFlags(1),
        trace_state=TraceState([("fornecedor", "estado-1")]),
    )
    token = context.attach(trace.set_span_in_context(NonRecordingSpan(pai)))
    try:
        transacao, sessao, _ = _transacao()
        with transacao:
            transacao.registrar_evento(ReservaLiberadaEvent(ordem_id=uuid4()))
    finally:
        context.detach(token)
    (linha,) = sessao.chamadas[1][2]
    assert linha["tracestate"] == "fornecedor=estado-1"
    assert linha["traceparent"] == _w3c(pai)
