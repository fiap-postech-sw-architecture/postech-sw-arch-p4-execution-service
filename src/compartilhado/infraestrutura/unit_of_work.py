from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from src.compartilhado.infraestrutura.database import violacao_de_unicidade
from src.compartilhado.infraestrutura.mensageria.contratos import (
    EXCHANGE_EVENTOS,
    envelope_do_evento,
    routing_key,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import contexto_atual
from src.compartilhado.infraestrutura.outbox_mapping import (
    CANAL_NOTIFY,
    mensagens_processadas_table,
    outbox_table,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType
    from uuid import UUID

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.integration_event import IntegrationEvent


class MensagemJaProcessadaError(Exception):
    """Outra entrega do mesmo comando comitou primeiro: esta e desfeita inteira."""


class SQLAlchemyUnitOfWork:
    """``UnitOfWork`` sobre uma ``Session``, uma por caso de uso.

    ``commit`` grava estado, outbox e ``NOTIFY`` na mesma transacao; saida do
    bloco com excecao desfaz tudo e descarta os eventos; a sessao fecha sempre
    na saida (a do request pode ser reaberta pelo proximo bloco).

    ``mensagem_de_origem`` e o ``id`` do comando em processamento (consumidor):
    vira o ``causation_id`` dos eventos e a linha de ``mensagens_processadas``,
    gravada no primeiro commit, junto com o efeito.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        mensagem_de_origem: UUID | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._eventos: list[IntegrationEvent] = []
        self._mensagem_de_origem = mensagem_de_origem
        self.comitou = False

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

        ``pg_notify`` e transacional: o relay so e acordado quando o COMMIT
        concluir, entao nunca le uma linha que ainda pode sofrer rollback.

        Raises:
            MensagemJaProcessadaError: o comando ja foi processado por outra
                entrega (nada desta transacao fica).
            MensagemInvalidaError: evento fora do contrato (defeito do servico).
        """
        sessao = self._sessao()
        if self._mensagem_de_origem is not None and not self.comitou:
            self._registrar_processamento(sessao, self._mensagem_de_origem)
        if self._eventos:
            sessao.execute(
                outbox_table.insert(), [self._linha_da_outbox(e) for e in self._eventos]
            )
            sessao.execute(
                text("SELECT pg_notify(:canal, '')"), {"canal": CANAL_NOTIFY}
            )
        sessao.commit()
        self._eventos = []
        self.comitou = True

    def rollback(self) -> None:
        """Desfaz a transacao e descarta os eventos registrados."""
        self._sessao().rollback()
        self._eventos = []

    def _linha_da_outbox(self, evento: IntegrationEvent) -> dict[str, Any]:
        # Contexto OTel de quem grava (o span do consumidor ou da requisicao):
        # o relay publica como filho dele.
        contexto = contexto_atual()
        return {
            "mensagem_id": evento.id,
            "tipo": evento.tipo,
            "correlation_id": evento.ordem_id,
            "exchange": EXCHANGE_EVENTOS,
            "routing_key": routing_key(evento.tipo),
            "envelope": envelope_do_evento(evento, self._mensagem_de_origem),
            "traceparent": contexto.get("traceparent"),
            "tracestate": contexto.get("tracestate"),
        }

    @staticmethod
    def _registrar_processamento(sessao: Session, mensagem_id: UUID) -> None:
        # Na transacao do efeito: duas entregas simultaneas do mesmo comando se
        # serializam na chave primaria, e a segunda desfaz o proprio efeito.
        try:
            sessao.execute(
                insert(mensagens_processadas_table).values(mensagem_id=mensagem_id)
            )
        except IntegrityError as exc:
            if not violacao_de_unicidade(exc):
                raise
            msg = f"Mensagem {mensagem_id} ja processada"
            raise MensagemJaProcessadaError(msg) from exc
