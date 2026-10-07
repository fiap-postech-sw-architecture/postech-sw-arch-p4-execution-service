"""Unidades de trabalho sobre SQLAlchemy: a dos casos de uso da API e a do comando.

As duas gravam os eventos na ``outbox`` (envelope do contrato, destino e o
contexto W3C de quem gravou) e o ``NOTIFY`` do relay na mesma transacao do
estado. Na API o caso de uso comita; no comando da saga quem comita e o
consumidor, depois do caso de uso (``TransacaoDaMensagem``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast

from sqlalchemy import text

from src.compartilhado.infraestrutura.mensageria.contratos import (
    EXCHANGE_EVENTOS,
    envelope_do_evento,
    routing_key,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import contexto_atual
from src.compartilhado.infraestrutura.outbox_mapping import CANAL_NOTIFY, outbox_table

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType
    from uuid import UUID

    from sqlalchemy.orm import Session, SessionTransaction

    from src.compartilhado.aplicacao.integration_event import IntegrationEvent


def gravar_na_outbox(
    sessao: Session, eventos: Sequence[IntegrationEvent], causa: UUID | None
) -> None:
    """Grava os eventos na outbox e acorda o relay, na transacao corrente.

    ``causa`` e o ``causation_id`` dos eventos que nao trazem o proprio (a
    resposta ao comando em processamento); o fato do mecanico traz o id do
    comando que abriu o fluxo. ``pg_notify`` e transacional: o relay so e
    acordado quando o COMMIT concluir, entao nunca le uma linha que ainda pode
    sofrer rollback.

    Raises:
        MensagemInvalidaError: evento fora do contrato (defeito do servico).
    """
    if not eventos:
        return
    sessao.execute(outbox_table.insert(), [_linha_da_outbox(e, causa) for e in eventos])
    sessao.execute(text("SELECT pg_notify(:canal, '')"), {"canal": CANAL_NOTIFY})


def _linha_da_outbox(evento: IntegrationEvent, causa: UUID | None) -> dict[str, Any]:
    # Contexto OTel de quem grava (o span do consumidor ou o do passo retomado):
    # o relay publica como filho dele.
    contexto = contexto_atual()
    return {
        "mensagem_id": evento.id,
        "tipo": evento.tipo,
        "correlation_id": evento.ordem_id,
        "exchange": EXCHANGE_EVENTOS,
        "routing_key": routing_key(evento.tipo),
        "envelope": envelope_do_evento(evento, evento.causation_id or causa),
        "traceparent": contexto.get("traceparent"),
        "tracestate": contexto.get("tracestate"),
    }


class SQLAlchemyUnitOfWork:
    """``UnitOfWork`` sobre uma ``Session``, uma por caso de uso da API.

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
        try:
            if exc_type is not None:
                self.rollback()
        finally:
            # Mesmo com o rollback falhando (conexao caida), a sessao fecha e
            # devolve a conexao ao pool.
            self._sessao().close()
            self._session = None

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        self._eventos.append(evento)

    def commit(self) -> None:
        """Comita o estado e, na MESMA transacao, grava os eventos na outbox.

        Raises:
            MensagemInvalidaError: evento fora do contrato (defeito do servico).
        """
        sessao = self._sessao()
        gravar_na_outbox(sessao, self._eventos, None)
        sessao.commit()
        self._eventos = []

    def rollback(self) -> None:
        """Desfaz a transacao e descarta os eventos registrados."""
        self._sessao().rollback()
        self._eventos = []


class TransacaoDaMensagem:
    """``UnitOfWorkDoComando`` na sessao da mensagem, que o consumidor comita.

    Cada tentativa do caso de uso e um SAVEPOINT: saida normal grava os eventos
    dela na outbox e libera o savepoint (o efeito segue na transacao da
    mensagem); excecao volta ao savepoint e descarta os eventos. ``comando_id``
    e o ``causation_id`` das respostas.
    """

    def __init__(self, sessao: Session, comando_id: UUID) -> None:
        self._sessao = sessao
        self._comando_id = comando_id
        self._tentativa: SessionTransaction | None = None
        self._eventos: list[IntegrationEvent] = []
        self.descartado = False

    def __enter__(self) -> Self:
        self._tentativa = self._sessao.begin_nested()
        self._eventos = []
        self.descartado = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # O __exit__ so roda depois do __enter__, que abriu a tentativa.
        tentativa = cast("SessionTransaction", self._tentativa)
        self._tentativa = None
        eventos, self._eventos = self._eventos, []
        if exc_type is not None:
            tentativa.rollback()
            return
        try:
            gravar_na_outbox(self._sessao, eventos, self._comando_id)
        except BaseException:
            tentativa.rollback()
            raise
        tentativa.commit()

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        self._eventos.append(evento)

    def descartar(self) -> None:
        self.descartado = True
