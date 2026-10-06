from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from src.execucao.aplicacao.ports import ItemDaFila
from src.execucao.dominio.execucao import Execucao, StatusExecucao
from src.execucao.infraestrutura.mapping import execucoes_table

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session

_t = execucoes_table
_NA_FILA = _t.c.status == StatusExecucao.AGUARDANDO


class ExecucaoSQLAlchemyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def obter(self, ordem_id: UUID, *, com_lock: bool = False) -> Execucao | None:
        stmt = select(Execucao).where(_t.c.ordem_id == ordem_id)
        if com_lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return self._session.scalars(stmt).one_or_none()

    def salvar(self, execucao: Execucao) -> None:
        self._session.add(execucao)
        self._session.flush()


class FilaDeExecucaoSQLAlchemy:
    """Read model da fila: prioridade desc, chegada asc, ``ordem_id`` desempata."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def listar(self, offset: int, limit: int) -> list[ItemDaFila]:
        stmt = (
            select(_t.c.ordem_id, _t.c.prioridade, _t.c.enfileirada_em)
            .where(_NA_FILA)
            .order_by(_t.c.prioridade.desc(), _t.c.enfileirada_em, _t.c.ordem_id)
            .offset(offset)
            .limit(limit)
        )
        return [
            ItemDaFila(
                posicao=offset + indice,
                ordem_id=linha.ordem_id,
                prioridade=linha.prioridade,
                enfileirada_em=linha.enfileirada_em,
            )
            for indice, linha in enumerate(self._session.execute(stmt), start=1)
        ]

    def contar(self) -> int:
        return self._session.scalar(select(func.count()).where(_NA_FILA)) or 0

    def posicao(self, execucao: Execucao) -> int:
        mesma_prioridade = _t.c.prioridade == execucao.prioridade
        a_frente = or_(
            _t.c.prioridade > execucao.prioridade,
            and_(mesma_prioridade, _t.c.enfileirada_em < execucao.enfileirada_em),
            and_(
                mesma_prioridade,
                _t.c.enfileirada_em == execucao.enfileirada_em,
                _t.c.ordem_id < execucao.ordem_id,
            ),
        )
        stmt = select(func.count()).where(_NA_FILA, a_frente)
        return (self._session.scalar(stmt) or 0) + 1
