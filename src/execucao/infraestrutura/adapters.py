from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Text, cast, func, literal_column, select, update

from src.compartilhado.dominio.veiculo import TEXTO_ELIMINADO
from src.compartilhado.infraestrutura.outbox_mapping import outbox_table
from src.diagnostico.infraestrutura.mapping import diagnosticos_table
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.estoque.aplicacao.use_cases import baixar_reserva, reserva_ativa
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)
from src.execucao.aplicacao.events import PecaConsumidaDTO
from src.execucao.aplicacao.ports import DiagnosticoAnonimizado

if TYPE_CHECKING:
    from collections.abc import Sequence
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


class DiagnosticosDoVeiculoSQLAlchemy:
    """``DiagnosticosDoVeiculoPort`` pelo repositorio do diagnostico (mesmo banco).

    Cada diagnostico passa pelo agregado (``anonimizar_titular``), nunca por
    UPDATE direto na tabela do contexto vizinho.
    """

    def __init__(self, session: Session) -> None:
        self._repo = DiagnosticoSQLAlchemyRepository(session)

    def anonimizar(self, veiculo_id: UUID) -> list[DiagnosticoAnonimizado]:
        anonimizados = []
        for diagnostico in self._repo.do_veiculo(veiculo_id):
            if diagnostico.anonimizar_titular():
                self._repo.salvar(diagnostico)
                anonimizados.append(
                    DiagnosticoAnonimizado(
                        diagnostico.ordem_id, diagnostico.em_andamento
                    )
                )
        return anonimizados


# Unico texto livre das mensagens publicadas por este servico.
_OBSERVACOES = outbox_table.c.envelope[("dados", "observacoes")]


class MensagensGuardadasSQLAlchemy:
    """``MensagensGuardadasPort`` sobre a outbox, na transacao do comando."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def anonimizar(self, ordens: Sequence[UUID]) -> int:
        if not ordens:
            return 0
        stmt = (
            update(outbox_table)
            .where(
                outbox_table.c.tipo == "DiagnosticoConcluido",
                outbox_table.c.correlation_id.in_(ordens),
                func.coalesce(_OBSERVACOES.astext, "").not_in(["", TEXTO_ELIMINADO]),
            )
            .values(
                envelope=func.jsonb_set(
                    outbox_table.c.envelope,
                    literal_column("'{dados,observacoes}'"),
                    func.to_jsonb(cast(TEXTO_ELIMINADO, Text)),
                )
            )
        )
        alteradas: int = self._session.connection().execute(stmt).rowcount
        return alteradas
