from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from types import TracebackType

    from src.compartilhado.aplicacao.integration_event import IntegrationEvent


class UnitOfWork(Protocol):
    """Transacao de um caso de uso: estado e eventos da outbox juntos ou nada."""

    def __enter__(self) -> Self:
        """Abre a unidade de trabalho e devolve a si mesma."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Desfaz a transacao (e descarta eventos) se o bloco terminou em excecao."""

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        """Agenda o evento para a outbox; so e gravado se ``commit`` acontecer."""

    def commit(self) -> None:
        """Comita o estado e grava os eventos registrados na mesma transacao."""

    def rollback(self) -> None:
        """Desfaz as alteracoes pendentes e descarta os eventos registrados."""
