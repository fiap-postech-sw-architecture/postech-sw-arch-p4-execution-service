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


class UnitOfWorkDoComando(Protocol):
    """Unidade de trabalho de um comando da saga, presa a transacao da mensagem.

    Quem comita e o consumidor, uma vez so, depois do caso de uso: efeito,
    respostas na outbox e o ``id`` do comando em ``mensagens_processadas``
    juntos ou nada. O caso de uso nao comita. Cada bloco ``with`` e uma
    tentativa: saida normal guarda o efeito e os eventos dela na transacao da
    mensagem; excecao desfaz so a tentativa (a releitura de corrida roda de
    novo).
    """

    def __enter__(self) -> Self:
        """Abre uma tentativa do caso de uso dentro da transacao da mensagem."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Guarda a tentativa (com os eventos) ou, com excecao, a desfaz."""

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        """Resposta ou fato do comando; vai para a outbox na transacao da mensagem."""

    def descartar(self) -> None:
        """O comando nao muda nada (atrasado ou repetido sem desfecho a republicar)."""
