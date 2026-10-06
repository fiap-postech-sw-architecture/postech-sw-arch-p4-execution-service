"""Composicao dos casos de uso da execucao na sessao do request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.execucao.aplicacao.use_cases import FinalizarExecucao, IniciarExecucao
from src.execucao.infraestrutura.adapters import EstoqueSQLAlchemyAdapter
from src.execucao.infraestrutura.repository import (
    ExecucaoSQLAlchemyRepository,
    FilaDeExecucaoSQLAlchemy,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def obter_fila(session: Session) -> FilaDeExecucaoSQLAlchemy:
    return FilaDeExecucaoSQLAlchemy(session)


def obter_iniciar_execucao(session: Session) -> IniciarExecucao:
    return IniciarExecucao(
        ExecucaoSQLAlchemyRepository(session),
        EstoqueSQLAlchemyAdapter(session),
        SQLAlchemyUnitOfWork(lambda: session),
    )


def obter_finalizar_execucao(session: Session) -> FinalizarExecucao:
    return FinalizarExecucao(
        ExecucaoSQLAlchemyRepository(session),
        EstoqueSQLAlchemyAdapter(session),
        SQLAlchemyUnitOfWork(lambda: session),
    )
