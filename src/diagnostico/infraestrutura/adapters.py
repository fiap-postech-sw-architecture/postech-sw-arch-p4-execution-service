from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from src.estoque.infraestrutura.mapping import itens_estoque_table

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session


class CatalogoDePecasSQLAlchemy:
    """``CatalogoDePecasPort`` sobre a tabela do estoque (mesmo banco do servico)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def skus_indisponiveis(self, skus: Sequence[str]) -> list[str]:
        stmt = select(itens_estoque_table.c.sku).where(
            itens_estoque_table.c.sku.in_(set(skus)),
            itens_estoque_table.c.ativo.is_(True),
        )
        ativos = {str(sku) for sku in self._session.scalars(stmt)}
        return [sku for sku in skus if sku not in ativos]
