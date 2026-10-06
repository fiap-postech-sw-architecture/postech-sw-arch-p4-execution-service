from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.dominio.exceptions import EntidadeNaoEncontradaException

if TYPE_CHECKING:
    from uuid import UUID

    from src.estoque.dominio.sku import Sku


class ItemEstoqueNaoEncontradoException(EntidadeNaoEncontradaException):
    def __init__(self, sku: Sku) -> None:
        super().__init__(mensagem=f"Item de estoque {sku} nao encontrado")


class ReservaNaoEncontradaException(EntidadeNaoEncontradaException):
    def __init__(self, ordem_id: UUID) -> None:
        super().__init__(
            mensagem=f"Reserva de pecas da ordem {ordem_id} nao encontrada"
        )
