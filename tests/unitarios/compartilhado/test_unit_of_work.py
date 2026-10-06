from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from src.compartilhado.aplicacao.integration_event import IntegrationEvent
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork


@dataclass(frozen=True, kw_only=True)
class CoisaFeitaEvent(IntegrationEvent):
    quantidade: int


class _SessaoFake:
    """Grava a sequencia de chamadas (a gravacao real e testada no Postgres)."""

    def __init__(self) -> None:
        self.chamadas: list[tuple[Any, ...]] = []

    def execute(self, stmt: object, params: object = None) -> None:
        self.chamadas.append(("execute", str(stmt), params))

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
    sessao: _SessaoFake | None = None,
) -> tuple[SQLAlchemyUnitOfWork, _SessaoFake]:
    sessao = sessao or _SessaoFake()
    # Fake so com a superficie da Session que a UoW usa (o tipo e Session).
    return SQLAlchemyUnitOfWork(lambda: sessao), sessao  # type: ignore[arg-type,return-value]


def test_commit_sem_eventos_nao_toca_a_outbox() -> None:
    uow, sessao = _uow()
    with uow:
        uow.commit()
    assert sessao.chamadas == [("commit",), ("close",)]


def test_commit_grava_eventos_e_notifica_o_relay_antes_do_commit() -> None:
    uow, sessao = _uow()
    evento = CoisaFeitaEvent(ordem_id=uuid4(), quantidade=2)
    with uow:
        uow.registrar_evento(evento)
        uow.commit()

    insert, notify, commit, close = sessao.chamadas
    assert insert[0] == "execute"
    assert insert[1].startswith("INSERT INTO outbox")
    assert insert[2] == [
        {
            "mensagem_id": evento.id,
            "tipo": "CoisaFeita",
            "correlation_id": evento.ordem_id,
            "ocorrido_em": evento.ocorrido_em,
            "dados": {"ordem_id": str(evento.ordem_id), "quantidade": 2},
        }
    ]
    assert notify == (
        "execute",
        "SELECT pg_notify(:canal, '')",
        {"canal": "outbox_novo"},
    )
    assert (commit, close) == (("commit",), ("close",))


def test_eventos_sao_gravados_uma_vez_so() -> None:
    uow, sessao = _uow()
    with uow:
        uow.registrar_evento(CoisaFeitaEvent(ordem_id=uuid4(), quantidade=1))
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
        uow.registrar_evento(CoisaFeitaEvent(ordem_id=uuid4(), quantidade=1))
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
        uow.registrar_evento(CoisaFeitaEvent(ordem_id=uuid4(), quantidade=1))
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
