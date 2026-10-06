from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from src.compartilhado.infraestrutura.database import (
    duplicata_vira_excecao_de_dominio,
)
from src.estoque.dominio.item_estoque import ItemEstoque
from src.estoque.dominio.reserva import Reserva
from src.estoque.infraestrutura.mapping import itens_estoque_table, reservas_table

if TYPE_CHECKING:
    from collections.abc import Collection
    from uuid import UUID

    from sqlalchemy.orm import Session

    from src.estoque.dominio.sku import Sku


class ItemEstoqueSQLAlchemyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def obter_por_sku(self, sku: Sku, *, com_lock: bool = False) -> ItemEstoque | None:
        stmt = select(ItemEstoque).where(itens_estoque_table.c.sku == sku)
        if com_lock:
            # populate_existing: relida sob lock, a instancia ja presente no
            # identity map recebe o valor comitado por quem segurava o lock
            # (sem isso o FOR UPDATE devolveria o objeto stale; licao do p3).
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return self._session.scalars(stmt).one_or_none()

    def obter_com_lock(self, skus: Collection[Sku]) -> dict[Sku, ItemEstoque]:
        if not skus:
            return {}
        # ORDER BY sku + FOR UPDATE: o Postgres trava as linhas na ordem do sort,
        # entao duas reservas sobre os mesmos itens nunca travam em ordem oposta.
        stmt = (
            select(ItemEstoque)
            .where(itens_estoque_table.c.sku.in_(set(skus)))
            .order_by(itens_estoque_table.c.sku)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return {item.sku: item for item in self._session.scalars(stmt)}

    def salvar(self, item: ItemEstoque) -> None:
        self._session.add(item)
        with duplicata_vira_excecao_de_dominio(
            f"Ja existe item de estoque com SKU {item.sku}"
        ):
            self._session.flush()

    def listar(self, offset: int, limit: int) -> list[ItemEstoque]:
        stmt = (
            select(ItemEstoque)
            .order_by(itens_estoque_table.c.sku)
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def contar(self) -> int:
        stmt = select(func.count()).select_from(itens_estoque_table)
        return self._session.scalar(stmt) or 0


class ReservaSQLAlchemyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def obter_por_ordem(
        self, ordem_id: UUID, *, com_lock: bool = False
    ) -> Reserva | None:
        stmt = select(Reserva).where(reservas_table.c.ordem_id == ordem_id)
        if com_lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return self._session.scalars(stmt).one_or_none()

    def salvar(self, reserva: Reserva) -> None:
        self._session.add(reserva)
        with duplicata_vira_excecao_de_dominio(
            f"Ja existe reserva para a ordem {reserva.ordem_id}"
        ):
            self._session.flush()
