"""Comando da saga rodado direto pelo teste, como o consumidor o rodaria."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem
from src.estoque.aplicacao.use_cases import LiberarReserva
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from types import TracebackType
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker


class TransacaoComitada(TransacaoDaMensagem):
    """Transacao da mensagem comitada logo depois de cada tentativa que deu certo.

    No consumidor o commit vem depois do handler; aqui vem no fim do caso de
    uso, para o teste preparar estado ou disputar locks como dois comandos.
    """

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        super().__exit__(exc_type, exc_val, exc_tb)
        if exc_type is None:
            self._sessao.commit()


def transacao_do_comando(sessao: Session) -> TransacaoComitada:
    """Transacao de um comando de ``id`` novo na sessao do teste."""
    return TransacaoComitada(sessao, uuid4())


def linha_pendente(session_factory: sessionmaker[Session]) -> UUID:
    """Grava uma ReservaLiberada na outbox (lapide de uma ordem nova)."""
    ordem_id = uuid4()
    with session_factory() as sessao:
        LiberarReserva(
            ItemEstoqueSQLAlchemyRepository(sessao),
            ReservaSQLAlchemyRepository(sessao),
            transacao_do_comando(sessao),
        ).executar(ordem_id)
    return ordem_id
