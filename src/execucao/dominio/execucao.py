from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import (
    OperacaoNaoPermitidaException,
    TransicaoStatusInvalidaException,
    ValorInvalidoError,
)
from src.compartilhado.dominio.maquina_de_estados import validar_transicao

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

PRIORIDADE_MAXIMA: Final = 100


class StatusExecucao(StrEnum):
    AGUARDANDO = "AGUARDANDO"
    EM_EXECUCAO = "EM_EXECUCAO"
    FINALIZADA = "FINALIZADA"
    CANCELADA = "CANCELADA"


_TRANSICOES: dict[StatusExecucao, frozenset[StatusExecucao]] = {
    StatusExecucao.AGUARDANDO: frozenset(
        {StatusExecucao.EM_EXECUCAO, StatusExecucao.CANCELADA}
    ),
    # Inicio da execucao e o pivot da saga: dali em diante nao ha cancelamento.
    StatusExecucao.EM_EXECUCAO: frozenset({StatusExecucao.FINALIZADA}),
    StatusExecucao.FINALIZADA: frozenset(),
    StatusExecucao.CANCELADA: frozenset(),
}


@dataclass(eq=False, kw_only=True)
class Execucao(AggregateRoot):
    """Execucao do reparo de uma ordem; a identidade e o proprio ``ordem_id``.

    AGUARDANDO (na fila) -> EM_EXECUCAO -> FINALIZADA; AGUARDANDO -> CANCELADA
    (compensacao). A fila ordena por ``prioridade`` (maior primeiro) e
    ``enfileirada_em`` (mais antiga primeiro).
    """

    _status: StatusExecucao = StatusExecucao.AGUARDANDO
    _prioridade: int
    _enfileirada_em: datetime
    _mecanico_id: UUID | None = None
    _iniciada_em: datetime | None = None
    _finalizada_em: datetime | None = None
    _cancelada_em: datetime | None = None

    def __post_init__(self) -> None:
        if not 0 <= self._prioridade <= PRIORIDADE_MAXIMA:
            msg = f"Prioridade deve estar entre 0 (normal) e {PRIORIDADE_MAXIMA}"
            raise ValorInvalidoError(msg)

    @classmethod
    def agendar(cls, *, ordem_id: UUID, prioridade: int, agora: datetime) -> Execucao:
        return cls(id=ordem_id, _prioridade=prioridade, _enfileirada_em=agora)

    @classmethod
    def lapide(cls, *, ordem_id: UUID, agora: datetime) -> Execucao:
        """Cancelamento que chegou antes do ``AgendarExecucao``: nasce CANCELADA.

        Nunca entra na fila (so AGUARDANDO entra); prioridade normal e
        ``enfileirada_em`` = instante da compensacao so preenchem as colunas
        obrigatorias. O agendamento atrasado acha a lapide pela chave
        ``ordem_id`` e e descartado sem efeito (RFC-004, secao 4.5).
        """
        return cls(
            id=ordem_id,
            _status=StatusExecucao.CANCELADA,
            _prioridade=0,
            _enfileirada_em=agora,
            _cancelada_em=agora,
        )

    @property
    def ordem_id(self) -> UUID:
        return self.id

    @property
    def status(self) -> StatusExecucao:
        return self._status

    @property
    def prioridade(self) -> int:
        return self._prioridade

    @property
    def enfileirada_em(self) -> datetime:
        return self._enfileirada_em

    @property
    def mecanico_id(self) -> UUID | None:
        return self._mecanico_id

    @property
    def iniciada_em(self) -> datetime | None:
        return self._iniciada_em

    @property
    def finalizada_em(self) -> datetime | None:
        return self._finalizada_em

    @property
    def cancelada_em(self) -> datetime | None:
        return self._cancelada_em

    def iniciar(self, mecanico_id: UUID, agora: datetime) -> bool:
        """AGUARDANDO -> EM_EXECUCAO; repetir pelo mesmo mecanico e no-op (False)."""
        if self._status is StatusExecucao.EM_EXECUCAO:
            if self._mecanico_id == mecanico_id:
                return False
            msg = "Execucao ja iniciada por outro mecanico"
            raise TransicaoStatusInvalidaException(msg)
        self._transicionar(StatusExecucao.EM_EXECUCAO)
        self._mecanico_id = mecanico_id
        self._iniciada_em = agora
        return True

    def finalizada_por(self, mecanico_id: UUID) -> bool:
        return (
            self._status is StatusExecucao.FINALIZADA
            and self._mecanico_id == mecanico_id
        )

    def finalizar(self, mecanico_id: UUID, agora: datetime) -> bool:
        """EM_EXECUCAO -> FINALIZADA, so pelo mecanico que iniciou; repetir e no-op."""
        if self.finalizada_por(mecanico_id):
            return False
        validar_transicao(
            _TRANSICOES, self._status, StatusExecucao.FINALIZADA, agregado="Execucao"
        )
        if self._mecanico_id != mecanico_id:
            msg = "Somente o mecanico que iniciou a execucao pode finaliza-la"
            raise OperacaoNaoPermitidaException(msg)
        self._status = StatusExecucao.FINALIZADA
        self._finalizada_em = agora
        return True

    def cancelar(self, agora: datetime) -> None:
        """Compensacao: AGUARDANDO -> CANCELADA. Idempotente; iniciada: 409."""
        if self._status is StatusExecucao.CANCELADA:
            return
        self._transicionar(StatusExecucao.CANCELADA)
        self._cancelada_em = agora

    def _transicionar(self, para: StatusExecucao) -> None:
        validar_transicao(_TRANSICOES, self._status, para, agregado="Execucao")
        self._status = para
