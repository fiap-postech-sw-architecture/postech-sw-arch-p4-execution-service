from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from src.compartilhado.aplicacao.integration_event import IntegrationEvent
from src.compartilhado.infraestrutura.mensageria.contratos import (
    MensagemInvalidaError,
    validar,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import tracer
from src.compartilhado.infraestrutura.unit_of_work import (
    MensagemJaProcessadaError,
    SQLAlchemyUnitOfWork,
)
from src.estoque.aplicacao.events import ReservaLiberadaEvent


@dataclass(frozen=True, kw_only=True)
class ForaDoContratoEvent(IntegrationEvent):
    """Evento sem schema no contrato: so um defeito o gravaria."""


class _Violacao:
    def __init__(self, pgcode: str) -> None:
        self.pgcode = pgcode


class _SessaoFake:
    """Grava a sequencia de chamadas (a gravacao real e testada no Postgres)."""

    def __init__(self, recusa_pgcode: str | None = None) -> None:
        self.chamadas: list[tuple[Any, ...]] = []
        self._recusa = recusa_pgcode

    def execute(self, stmt: object, params: object = None) -> None:
        sql = str(stmt)
        if self._recusa and sql.startswith("INSERT INTO mensagens_processadas"):
            raise IntegrityError(sql, {}, _Violacao(self._recusa))  # type: ignore[arg-type]
        self.chamadas.append(("execute", sql, params, stmt))

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


def _uow(
    sessao: _SessaoFake | None = None, **kwargs: Any
) -> tuple[SQLAlchemyUnitOfWork, _SessaoFake]:
    sessao = sessao or _SessaoFake()
    # Fake so com a superficie da Session que a UoW usa (o tipo e Session).
    return SQLAlchemyUnitOfWork(lambda: sessao, **kwargs), sessao  # type: ignore[arg-type,return-value]


def _sql(sessao: _SessaoFake) -> list[str]:
    return [c[1].split(" (")[0] if c[0] == "execute" else c[0] for c in sessao.chamadas]


def test_commit_sem_eventos_nao_toca_a_outbox() -> None:
    uow, sessao = _uow()
    with uow:
        uow.commit()
    assert sessao.chamadas == [("commit",), ("close",)]
    assert uow.comitou


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


def test_comando_em_processamento_vira_causa_e_linha_de_processada() -> None:
    comando_id = uuid4()
    uow, sessao = _uow(mensagem_de_origem=comando_id)
    with tracer.start_as_current_span("process LiberarReserva") as span:
        with uow:
            uow.registrar_evento(ReservaLiberadaEvent(ordem_id=uuid4()))
            uow.commit()
        contexto = span.get_span_context()

    processada, insert, *_ = sessao.chamadas
    assert processada[1].startswith("INSERT INTO mensagens_processadas")
    assert processada[3].compile().params == {"mensagem_id": comando_id}
    (linha,) = insert[2]
    assert linha["envelope"]["causation_id"] == str(comando_id)
    assert linha["traceparent"] == (
        f"00-{contexto.trace_id:032x}-{contexto.span_id:016x}-"
        f"{contexto.trace_flags:02x}"
    )


def test_processada_e_gravada_so_no_primeiro_commit() -> None:
    uow, sessao = _uow(mensagem_de_origem=uuid4())
    with uow:
        uow.commit()
        uow.commit()
    assert _sql(sessao) == [
        "INSERT INTO mensagens_processadas",
        "commit",
        "commit",
        "close",
    ]


def _gravar(uow: SQLAlchemyUnitOfWork, evento: IntegrationEvent) -> None:
    with uow:
        uow.registrar_evento(evento)
        uow.commit()


def test_outra_entrega_do_mesmo_comando_ja_comitada_vira_excecao_propria() -> None:
    uow, sessao = _uow(_SessaoFake(recusa_pgcode="23505"), mensagem_de_origem=uuid4())
    with pytest.raises(MensagemJaProcessadaError):
        _gravar(uow, ReservaLiberadaEvent(ordem_id=uuid4()))
    assert sessao.chamadas == [("rollback",), ("close",)]
    assert not uow.comitou


def test_outra_violacao_de_integridade_sobe_como_esta() -> None:
    uow, _ = _uow(_SessaoFake(recusa_pgcode="23502"), mensagem_de_origem=uuid4())
    with pytest.raises(IntegrityError):
        _gravar(uow, ReservaLiberadaEvent(ordem_id=uuid4()))


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
