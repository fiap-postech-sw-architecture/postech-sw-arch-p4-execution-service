"""Comandos da saga para o estoque, entregues pelo consumidor (RFC-004 5.3).

O envelope chega validado pelo contrato; o caso de uso roda na sessao e na UoW
da mensagem, que gravam a idempotencia e a resposta na mesma transacao.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.estoque.aplicacao.use_cases import LiberarReserva, ReservarPecas
from src.estoque.dominio.reserva import ItemReserva
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork


def reservar_pecas(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWork
) -> None:
    dados = envelope["dados"]
    ReservarPecas(
        ItemEstoqueSQLAlchemyRepository(sessao),
        ReservaSQLAlchemyRepository(sessao),
        uow,
    ).executar(
        UUID(dados["ordem_id"]),
        [ItemReserva(Sku(peca["sku"]), peca["quantidade"]) for peca in dados["pecas"]],
    )


def liberar_reserva(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWork
) -> None:
    dados = envelope["dados"]
    LiberarReserva(
        ItemEstoqueSQLAlchemyRepository(sessao),
        ReservaSQLAlchemyRepository(sessao),
        uow,
    ).executar(UUID(dados["ordem_id"]))
