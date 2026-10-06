from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from src.diagnostico.dominio.diagnostico import Diagnostico
from src.diagnostico.infraestrutura.mapping import diagnosticos_table

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Select
    from sqlalchemy.orm import Session

    from src.diagnostico.dominio.diagnostico import StatusDiagnostico


class DiagnosticoSQLAlchemyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def obter(self, ordem_id: UUID, *, com_lock: bool = False) -> Diagnostico | None:
        stmt = select(Diagnostico).where(diagnosticos_table.c.ordem_id == ordem_id)
        if com_lock:
            # populate_existing: a leitura sob lock sobrescreve a instancia ja
            # carregada nesta sessao (ConcluirDiagnostico le antes sem lock).
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return self._session.scalars(stmt).one_or_none()

    def salvar(self, diagnostico: Diagnostico) -> None:
        self._session.add(diagnostico)
        self._session.flush()

    def listar(
        self, status: StatusDiagnostico | None, offset: int, limit: int
    ) -> list[Diagnostico]:
        stmt = (
            _filtrar(select(Diagnostico), status)
            .order_by(diagnosticos_table.c.solicitado_em, diagnosticos_table.c.ordem_id)
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def contar(self, status: StatusDiagnostico | None) -> int:
        stmt = _filtrar(select(func.count()).select_from(diagnosticos_table), status)
        return self._session.scalar(stmt) or 0


def _filtrar[T: tuple[object, ...]](
    stmt: Select[T], status: StatusDiagnostico | None
) -> Select[T]:
    if status is None:
        return stmt
    return stmt.where(diagnosticos_table.c.status == status)
