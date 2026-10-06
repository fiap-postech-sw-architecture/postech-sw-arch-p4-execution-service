from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

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

    from src.compartilhado.dominio.veiculo import Veiculo


class Prioridade(StrEnum):
    """Prioridade do contrato ``AgendarExecucao``: ``alta`` passa na frente."""

    NORMAL = "normal"
    ALTA = "alta"


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
    (compensacao). A fila atende ``alta`` antes de ``normal`` e, dentro da
    mesma prioridade, por ordem de chegada (``enfileirada_em``). ``veiculo`` e a
    copia do retrato do diagnostico, para o mecanico achar o carro no patio.
    """

    _status: StatusExecucao = StatusExecucao.AGUARDANDO
    _prioridade: Prioridade
    _enfileirada_em: datetime
    _veiculo: Veiculo | None = None
    _mecanico_id: UUID | None = None
    _iniciada_em: datetime | None = None
    _finalizada_em: datetime | None = None
    _cancelada_em: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self._prioridade, Prioridade):
            msg = "Prioridade deve ser 'normal' ou 'alta'"
            raise ValorInvalidoError(msg)

    @classmethod
    def agendar(
        cls,
        *,
        ordem_id: UUID,
        prioridade: Prioridade,
        veiculo: Veiculo | None,
        agora: datetime,
    ) -> Execucao:
        return cls(
            id=ordem_id, _prioridade=prioridade, _veiculo=veiculo, _enfileirada_em=agora
        )

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
            _prioridade=Prioridade.NORMAL,
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
    def prioridade(self) -> Prioridade:
        return self._prioridade

    @property
    def enfileirada_em(self) -> datetime:
        return self._enfileirada_em

    @property
    def veiculo(self) -> Veiculo | None:
        return self._veiculo

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
