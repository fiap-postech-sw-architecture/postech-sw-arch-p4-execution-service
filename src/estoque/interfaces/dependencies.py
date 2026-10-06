"""Composicao dos casos de uso do estoque (repositorios + UoW na sessao do request)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.estoque.aplicacao.use_cases import (
    AjustarQuantidade,
    AtualizarItemEstoque,
    ConsultarItemEstoque,
    CriarItemEstoque,
    DesativarItemEstoque,
    ListarItensEstoque,
)
from src.estoque.infraestrutura.repository import ItemEstoqueSQLAlchemyRepository

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _repo(session: Session) -> ItemEstoqueSQLAlchemyRepository:
    return ItemEstoqueSQLAlchemyRepository(session)


def _uow(session: Session) -> SQLAlchemyUnitOfWork:
    return SQLAlchemyUnitOfWork(lambda: session)


def obter_criar_item(session: Session) -> CriarItemEstoque:
    return CriarItemEstoque(_repo(session), _uow(session))


def obter_listar_itens(session: Session) -> ListarItensEstoque:
    return ListarItensEstoque(_repo(session))


def obter_consultar_item(session: Session) -> ConsultarItemEstoque:
    return ConsultarItemEstoque(_repo(session))


def obter_atualizar_item(session: Session) -> AtualizarItemEstoque:
    return AtualizarItemEstoque(_repo(session), _uow(session))


def obter_ajustar_quantidade(session: Session) -> AjustarQuantidade:
    return AjustarQuantidade(_repo(session), _uow(session))


def obter_desativar_item(session: Session) -> DesativarItemEstoque:
    return DesativarItemEstoque(_repo(session), _uow(session))
