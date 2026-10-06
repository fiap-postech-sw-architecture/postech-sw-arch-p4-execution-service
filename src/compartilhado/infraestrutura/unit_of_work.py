from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from sqlalchemy import text

from src.compartilhado.aplicacao.outbox import dados_do_evento
from src.compartilhado.infraestrutura.outbox_mapping import CANAL_NOTIFY, outbox_table

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.integration_event import IntegrationEvent


def _linha_da_outbox(evento: IntegrationEvent) -> dict[str, Any]:
    return {
        "mensagem_id": evento.id,
        "tipo": evento.tipo,
        "correlation_id": evento.ordem_id,
        "ocorrido_em": evento.ocorrido_em,
        "dados": dados_do_evento(evento),
    }


class SQLAlchemyUnitOfWork:
    """``UnitOfWork`` sobre uma ``Session``, uma por caso de uso.

    ``commit`` grava estado, outbox e ``NOTIFY`` na mesma transacao; saida do
    bloco com excecao desfaz tudo e descarta os eventos; a sessao fecha sempre
    na saida (a do request pode ser reaberta pelo proximo bloco).
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._eventos: list[IntegrationEvent] = []

    def _sessao(self) -> Session:
        if self._session is None:
            msg = "UnitOfWork nao foi iniciada. Use 'with' para inicia-la."
            raise RuntimeError(msg)
        return self._session

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        self._eventos = []
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollback()
        self._sessao().close()
        self._session = None

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        self._eventos.append(evento)

    def commit(self) -> None:
        """Comita o estado e, na MESMA transacao, grava os eventos na outbox.

        ``pg_notify`` e transacional: o relay so e acordado quando o COMMIT
        concluir, entao nunca le uma linha que ainda pode sofrer rollback.
        """
        sessao = self._sessao()
        if self._eventos:
            sessao.execute(
                outbox_table.insert(), [_linha_da_outbox(e) for e in self._eventos]
            )
            sessao.execute(
                text("SELECT pg_notify(:canal, '')"), {"canal": CANAL_NOTIFY}
            )
        sessao.commit()
        self._eventos = []

    def rollback(self) -> None:
        """Desfaz a transacao e descarta os eventos registrados."""
        self._sessao().rollback()
        self._eventos = []
