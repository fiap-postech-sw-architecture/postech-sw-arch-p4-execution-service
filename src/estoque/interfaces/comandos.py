"""Comandos da saga para o estoque, entregues pelo consumidor (RFC-004 5.3).

O envelope chega validado pelo contrato; o caso de uso roda na sessao e na
unidade de trabalho da mensagem, e o consumidor comita efeito, resposta e
idempotencia juntos.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.estoque.aplicacao.use_cases import LiberarReserva, ReservarPecas
from src.estoque.dominio.reserva import Faltante, ItemReserva
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
    """``ReservarPecas``: SKU fora do formato do Billing falta inteiro.

    O contrato aceita codigo que o estoque nunca cadastra (minusculas, ``.`` e
    ``_``): a resposta e ``ReservaDePecasFalhou`` com ele, e a saga compensa,
    em vez de a mensagem ir para a DLQ.
    """
    dados = envelope["dados"]
    pecas, fora_do_catalogo = [], []
    for peca in dados["pecas"]:
        try:
            pecas.append(ItemReserva(Sku(peca["sku"]), peca["quantidade"]))
        except ValorInvalidoError:
            fora_do_catalogo.append(
                Faltante(sku=peca["sku"], solicitado=peca["quantidade"], disponivel=0)
            )
    ReservarPecas(
        ItemEstoqueSQLAlchemyRepository(sessao),
        ReservaSQLAlchemyRepository(sessao),
        uow,
    ).executar(UUID(dados["ordem_id"]), pecas, fora_do_catalogo)


def liberar_reserva(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
) -> None:
    dados = envelope["dados"]
    LiberarReserva(
        ItemEstoqueSQLAlchemyRepository(sessao),
        ReservaSQLAlchemyRepository(sessao),
        uow,
    ).executar(UUID(dados["ordem_id"]))
