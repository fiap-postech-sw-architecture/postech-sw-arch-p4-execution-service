from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection
    from uuid import UUID

    from src.estoque.dominio.item_estoque import ItemEstoque
    from src.estoque.dominio.reserva import Reserva
    from src.estoque.dominio.sku import Sku


class ItemEstoqueRepository(Protocol):
    def obter_por_sku(self, sku: Sku, *, com_lock: bool = False) -> ItemEstoque | None:
        """Busca o item; ``com_lock=True`` serializa escritas (FOR UPDATE)."""

    def obter_com_lock(self, skus: Collection[Sku]) -> dict[Sku, ItemEstoque]:
        """Trava os itens existentes em ordem de sku (evita deadlock entre reservas).

        SKUs sem cadastro ficam fora do dicionario.
        """

    def salvar(self, item: ItemEstoque) -> None:
        """Adiciona o item a sessao e faz flush."""

    def listar(self, offset: int, limit: int) -> list[ItemEstoque]:
        """Pagina ordenada por sku."""

    def contar(self) -> int:
        """Total de itens cadastrados."""


class ReservaRepository(Protocol):
    def obter_por_ordem(
        self, ordem_id: UUID, *, com_lock: bool = False
    ) -> Reserva | None:
        """Reserva da ordem (no maximo uma); ``com_lock=True`` aplica FOR UPDATE."""

    def salvar(self, reserva: Reserva) -> None:
        """Adiciona a reserva a sessao e faz flush."""
