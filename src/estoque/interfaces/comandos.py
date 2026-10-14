"""Comandos da saga para o estoque, entregues pelo consumidor (RFC-004 5.3).

O envelope chega validado pelo contrato; o caso de uso roda na sessao e na
unidade de trabalho da mensagem, e o consumidor comita efeito, resposta e
idempotencia juntos.
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

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWorkDoComando


def reservar_pecas(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
) -> None:
    """``ReservarPecas``: reserva as pecas da ordem e responde.

    O contrato so aceita SKU no formato do Billing, o mesmo do ``Sku``: o que
    esta fora dele e recusado pelo consumidor antes do handler (DLQ), entao o
    ``Sku`` aqui nao recusa nada.
    """
    dados = envelope["dados"]
    pecas = [
        ItemReserva(Sku(peca["sku"]), peca["quantidade"]) for peca in dados["pecas"]
    ]
    ReservarPecas(
        ItemEstoqueSQLAlchemyRepository(sessao),
        ReservaSQLAlchemyRepository(sessao),
        uow,
    ).executar(UUID(dados["ordem_id"]), pecas)


def liberar_reserva(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
) -> None:
    """``LiberarReserva`` (compensacao): devolve as pecas e responde."""
    dados = envelope["dados"]
    LiberarReserva(
        ItemEstoqueSQLAlchemyRepository(sessao),
        ReservaSQLAlchemyRepository(sessao),
        uow,
    ).executar(UUID(dados["ordem_id"]))
