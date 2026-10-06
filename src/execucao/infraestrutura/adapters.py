from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from src.diagnostico.infraestrutura.mapping import diagnosticos_table
from src.estoque.aplicacao.use_cases import baixar_reserva, reserva_ativa
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)
from src.execucao.aplicacao.events import PecaConsumidaDTO

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.orm import Session

    from src.compartilhado.dominio.veiculo import Veiculo


class EstoqueSQLAlchemyAdapter:
    """``EstoquePort`` sobre o contexto estoque, na sessao do caso de uso.

    A baixa e a do proprio estoque (``baixar_reserva``): ordem de lock execucao
    -> reserva -> itens por sku, igual a da liberacao, sem deadlock entre elas.
    """

    def __init__(self, session: Session) -> None:
        self._reservas = ReservaSQLAlchemyRepository(session)
        self._itens = ItemEstoqueSQLAlchemyRepository(session)

    def tem_reserva_ativa(self, ordem_id: UUID) -> bool:
        return reserva_ativa(self._reservas, ordem_id)

    def consumir_reserva(
        self, ordem_id: UUID, agora: datetime
    ) -> list[PecaConsumidaDTO] | None:
        reserva = baixar_reserva(self._itens, self._reservas, ordem_id, agora)
        if reserva is None:
            return None
        return [
            PecaConsumidaDTO(sku=str(linha.sku), quantidade=linha.quantidade)
            for linha in reserva.itens
        ]


class VeiculosSQLAlchemy:
    """``VeiculosPort`` sobre a tabela do diagnostico (mesmo banco do servico)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def da_ordem(self, ordem_id: UUID) -> Veiculo | None:
        stmt = select(diagnosticos_table.c.veiculo).where(
            diagnosticos_table.c.ordem_id == ordem_id
        )
        veiculo: Veiculo | None = self._session.scalar(stmt)
        return veiculo
