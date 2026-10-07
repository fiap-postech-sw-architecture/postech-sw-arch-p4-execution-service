"""Comandos para a execucao, entregues pelo consumidor (RFC-004 5.3).

O envelope chega validado pelo contrato; o caso de uso roda na sessao e na UoW
da mensagem, que gravam a idempotencia e a resposta na mesma transacao.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.compartilhado.aplicacao.lgpd import AnonimizarVeiculo
from src.execucao.aplicacao.use_cases import AgendarExecucao, CancelarExecucao
from src.execucao.dominio.execucao import Prioridade
from src.execucao.infraestrutura.adapters import (
    EstoqueSQLAlchemyAdapter,
    RetratosDoVeiculoSQLAlchemy,
    VeiculosSQLAlchemy,
)
from src.execucao.infraestrutura.repository import (
    ExecucaoSQLAlchemyRepository,
    FilaDeExecucaoSQLAlchemy,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork


def agendar_execucao(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWork
) -> None:
    dados = envelope["dados"]
    AgendarExecucao(
        ExecucaoSQLAlchemyRepository(sessao),
        FilaDeExecucaoSQLAlchemy(sessao),
        VeiculosSQLAlchemy(sessao),
        EstoqueSQLAlchemyAdapter(sessao),
        uow,
    ).executar(
        UUID(dados["ordem_id"]),
        Prioridade(dados["prioridade"]),
        agendamento_id=UUID(envelope["id"]),
    )


def cancelar_execucao(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWork
) -> None:
    dados = envelope["dados"]
    CancelarExecucao(ExecucaoSQLAlchemyRepository(sessao), uow).executar(
        UUID(dados["ordem_id"])
    )


def anonimizar_veiculo(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWork
) -> None:
    dados = envelope["dados"]
    AnonimizarVeiculo(RetratosDoVeiculoSQLAlchemy(sessao), uow).executar(
        UUID(dados["veiculo_id"])
    )
