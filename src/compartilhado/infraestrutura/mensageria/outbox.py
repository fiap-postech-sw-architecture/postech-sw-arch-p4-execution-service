"""A outbox vista pelo relay: o SQL de cada passo e a politica de tentativas.

Nenhuma transacao fica aberta durante a publicacao: com o broker em alarme de
memoria ou disco o publish espera ate o timeout de bloqueio, e uma transacao
parada esse tempo cairia no ``idle_in_transaction_session_timeout`` do banco.
Cada passo e uma transacao curta:

1. Claim: as linhas ``pendente`` vencidas, em ordem de ``id`` e sem passar a
   frente de uma pendente da mesma ordem (``correlation_id``), com
   ``FOR UPDATE SKIP LOCKED`` (duas replicas reivindicam lotes disjuntos),
   ganham um lease (``proxima_tentativa_em`` no futuro). O fim do lease e o
   token da replica que reivindicou.
2. Antes de publicar, o lease e renovado se a linha ainda for desta replica
   (token igual): o publish tem o lease inteiro.
3. O desfecho (``entregue``, nova tentativa ou ``dead``) so e gravado com o
   token ainda igual (fencing): a replica que perdeu a linha nunca sobrescreve
   a marcacao de outra.

Um publish que passe do lease (broker mudo, ate o heartbeat do AMQP derrubar a
conexao) pode sair tambem pela replica que reivindicar a linha depois: as duas
copias tem o mesmo ``message_id`` e o consumidor descarta a repetida.

Todo tempo e do relogio do banco (``now()`` e ``clock_timestamp()``), o mesmo
com que a linha e gravada: com o relogio do processo, uma linha recem-gravada
podia parecer do futuro e o ``NOTIFY`` que acordou o relay se perdia ate o poll.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.compartilhado.infraestrutura.mensageria.processo import LOTE_DA_LIMPEZA

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy import Engine, TextClause

# Atrasos do relay do p3: um depois de cada falha da propria mensagem; a falha
# seguinte a ultima da tabela (a quinta) leva a linha a `dead`.
ATRASOS_S: Final[tuple[float, ...]] = (1, 4, 16, 64)
_RETENCAO_DAS_ENTREGUES: Final = timedelta(days=7)
# ``dead`` fica para quem opera conferir e republicar, e sai em 30 dias: guarda
# o envelope inteiro, inclusive o texto livre das observacoes.
_RETENCAO_DAS_DEAD: Final = timedelta(days=30)

_CLAIM: Final = text(
    "SELECT o.id, o.tipo, o.correlation_id, o.exchange, o.routing_key, "
    "o.envelope, o.traceparent, o.tracestate, o.tentativas "
    "FROM outbox o "
    "WHERE o.status = 'pendente' AND o.proxima_tentativa_em <= now() "
    "AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.correlation_id = "
    "o.correlation_id AND p.id < o.id AND p.status = 'pendente') "
    "ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT :limite"
)
_LEASE: Final = text(
    "UPDATE outbox SET proxima_tentativa_em = clock_timestamp() + :lease "
    "WHERE id = ANY(:ids) RETURNING id, proxima_tentativa_em"
)
# Fencing (o WHERE de cada marcacao): a linha segue pendente e com o lease que
# esta replica gravou.
_RENOVAR: Final = text(
    "UPDATE outbox SET proxima_tentativa_em = clock_timestamp() + :lease "
    "WHERE id = :id AND status = 'pendente' AND proxima_tentativa_em = :lease_ate "
    "RETURNING proxima_tentativa_em"
)
_ENTREGUE: Final = text(
    "UPDATE outbox SET status = 'entregue', entregue_em = clock_timestamp(), "
    "ultimo_erro = NULL "
    "WHERE id = :id AND status = 'pendente' AND proxima_tentativa_em = :lease_ate"
)
_NOVA_TENTATIVA: Final = text(
    "UPDATE outbox SET tentativas = :tentativas, "
    "proxima_tentativa_em = clock_timestamp() + :atraso, ultimo_erro = :erro "
    "WHERE id = :id AND status = 'pendente' AND proxima_tentativa_em = :lease_ate"
)
_DEAD: Final = text(
    "UPDATE outbox SET status = 'dead', tentativas = :tentativas, "
    "ultimo_erro = :erro "
    "WHERE id = :id AND status = 'pendente' AND proxima_tentativa_em = :lease_ate"
)
_LIBERAR: Final = text(
    "UPDATE outbox SET proxima_tentativa_em = now() "
    "WHERE id = :id AND status = 'pendente' AND proxima_tentativa_em = :lease_ate"
)
_LIMPAR_LOTE: Final = text(
    "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox "
    "WHERE (status = 'entregue' AND entregue_em < now() - :entregues) "
    "OR (status = 'dead' AND criado_em < now() - :dead) "
    "ORDER BY id LIMIT :lote)"
)
_CONTAR: Final = text("SELECT count(*) FROM outbox WHERE status = :status")


def atraso_depois_da_falha(
    tentativas: int, atrasos: Sequence[float] = ATRASOS_S
) -> float | None:
    """Segundos ate a proxima tentativa depois da falha numero ``tentativas``.

    ``None`` quando a falha passa da tabela: com quatro atrasos, a quinta falha
    leva a linha a ``dead``.
    """
    if tentativas > len(atrasos):
        return None
    return atrasos[tentativas - 1]


@dataclass(frozen=True, slots=True)
class LinhaDaOutbox:
    """Linha reivindicada; o envelope fica fora do ``repr`` (texto livre).

    ``lease_ate`` e o fim do lease que esta replica gravou: o token do fencing.
    """

    id: int
    tipo: str
    correlation_id: UUID
    exchange: str
    routing_key: str
    envelope: dict[str, Any] = field(repr=False)
    traceparent: str | None
    tracestate: str | None
    tentativas: int
    lease_ate: datetime


class Outbox:
    """Os passos do relay sobre a tabela ``outbox``, cada um numa transacao curta.

    As marcacoes devolvem False quando a linha deixou de ser desta replica (o
    lease venceu e outra a reivindicou, ou ja a finalizou).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def reivindicar(self, lote: int, lease: timedelta) -> list[LinhaDaOutbox]:
        """Ate ``lote`` linhas vencidas, em ordem por OS, seguras pelo ``lease``."""
        with self._engine.begin() as conexao:
            linhas = conexao.execute(_CLAIM, {"limite": lote}).all()
            if not linhas:
                return []
            leases: dict[int, datetime] = {
                linha.id: linha.proxima_tentativa_em
                for linha in conexao.execute(
                    _LEASE, {"lease": lease, "ids": [linha.id for linha in linhas]}
                )
            }
        return [
            LinhaDaOutbox(**linha._mapping, lease_ate=leases[linha.id])
            for linha in linhas
        ]

    def renovar(self, linha: LinhaDaOutbox, lease: timedelta) -> LinhaDaOutbox | None:
        """Lease novo antes de publicar; None se a linha nao e mais desta replica."""
        with self._engine.begin() as conexao:
            renovada = conexao.execute(
                _RENOVAR,
                {"id": linha.id, "lease_ate": linha.lease_ate, "lease": lease},
            ).scalar_one_or_none()
        if renovada is None:
            return None
        return dataclasses.replace(linha, lease_ate=renovada)

    def marcar_entregue(self, linha: LinhaDaOutbox) -> bool:
        return self._marcar(_ENTREGUE, linha)

    def registrar_falha(
        self, linha: LinhaDaOutbox, erro: str
    ) -> Literal["dead", "nova_tentativa", "perdida"]:
        """Conta a tentativa: volta depois do atraso da tabela ou vira ``dead``.

        ``erro`` vai para ``ultimo_erro`` e e texto fixo: a excecao do pika
        carrega a mensagem devolvida, com o texto livre do envelope.
        ``perdida``: a linha nao e mais desta replica, e nada foi gravado.
        """
        tentativas = linha.tentativas + 1
        atraso = atraso_depois_da_falha(tentativas)
        if atraso is None:
            dead = self._marcar(_DEAD, linha, tentativas=tentativas, erro=erro)
            return "dead" if dead else "perdida"
        gravada = self._marcar(
            _NOVA_TENTATIVA,
            linha,
            tentativas=tentativas,
            erro=erro,
            atraso=timedelta(seconds=atraso),
        )
        return "nova_tentativa" if gravada else "perdida"

    def marcar_dead(self, linha: LinhaDaOutbox, erro: str) -> bool:
        """``dead`` sem nova tentativa (envelope que nenhuma tentativa conserta)."""
        return self._marcar(_DEAD, linha, tentativas=linha.tentativas + 1, erro=erro)

    def liberar(self, linhas: Sequence[LinhaDaOutbox]) -> None:
        """Devolve as linhas ja, sem esperar o lease (o broker caiu)."""
        with self._engine.begin() as conexao:
            conexao.execute(
                _LIBERAR,
                [{"id": linha.id, "lease_ate": linha.lease_ate} for linha in linhas],
            )

    def limpar(self, lote: int = LOTE_DA_LIMPEZA) -> int:
        """Apaga, em lotes, as entregues ha mais de 7 dias e as ``dead`` de 30.

        Devolve quantas apagou.
        """
        apagadas = 0
        parametros = {
            "entregues": _RETENCAO_DAS_ENTREGUES,
            "dead": _RETENCAO_DAS_DEAD,
            "lote": lote,
        }
        while True:
            with self._engine.begin() as conexao:
                deste_lote: int = conexao.execute(_LIMPAR_LOTE, parametros).rowcount
            apagadas += deste_lote
            if deste_lote < lote:
                return apagadas

    def contar(self, status: str) -> float:
        """Linhas com o ``status`` (gauge do /metrics); NaN com o banco fora."""
        try:
            with self._engine.connect() as conexao:
                total = conexao.execute(_CONTAR, {"status": status}).scalar_one()
        except SQLAlchemyError:
            return float("nan")
        return float(total)

    def _marcar(
        self, sql: TextClause, linha: LinhaDaOutbox, **parametros: object
    ) -> bool:
        with self._engine.begin() as conexao:
            alteradas: int = conexao.execute(
                sql, {"id": linha.id, "lease_ate": linha.lease_ate, **parametros}
            ).rowcount
        return alteradas == 1
