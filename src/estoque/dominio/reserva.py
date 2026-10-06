from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.compartilhado.dominio.maquina_de_estados import validar_transicao
from src.compartilhado.dominio.value_object import ValueObject

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.estoque.dominio.sku import Sku


class StatusReserva(StrEnum):
    ATIVA = "ATIVA"
    LIBERADA = "LIBERADA"
    CONSUMIDA = "CONSUMIDA"
    RECUSADA = "RECUSADA"


_TRANSICOES: dict[StatusReserva, frozenset[StatusReserva]] = {
    StatusReserva.ATIVA: frozenset({StatusReserva.LIBERADA, StatusReserva.CONSUMIDA}),
    StatusReserva.LIBERADA: frozenset(),
    StatusReserva.CONSUMIDA: frozenset(),
    StatusReserva.RECUSADA: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ItemReserva(ValueObject):
    sku: Sku
    quantidade: int

    def __post_init__(self) -> None:
        if self.quantidade <= 0:
            msg = f"Quantidade da peca {self.sku} deve ser positiva"
            raise ValorInvalidoError(msg)


@dataclass(frozen=True, slots=True)
class Faltante(ValueObject):
    """Peca que impediu a reserva; ``disponivel`` e o saldo livre no momento."""

    sku: Sku
    solicitado: int
    disponivel: int


@dataclass(eq=False, kw_only=True)
class Reserva(AggregateRoot):
    """Decisao de estoque de uma ordem: unica por ordem, tudo-ou-nada.

    ATIVA (pecas separadas) -> LIBERADA ou CONSUMIDA. RECUSADA registra a
    falta de peca (com os ``faltantes``) sem separar nada: o comando repetido
    recebe a mesma resposta em vez de uma nova tentativa (reserva tardia de
    ordem que a saga ja compensou). Lista vazia de pecas e uma reserva valida.
    As quantidades nos itens mudam pelo servico de dominio (``services.py``).
    """

    _ordem_id: UUID
    _itens: tuple[ItemReserva, ...]
    _status: StatusReserva = StatusReserva.ATIVA
    _faltantes: tuple[Faltante, ...] = ()
    _criada_em: datetime
    _encerrada_em: datetime | None = None

    def __post_init__(self) -> None:
        skus = [item.sku for item in self._itens]
        if len(skus) != len(set(skus)):
            msg = "Cada SKU deve aparecer uma unica vez na reserva"
            raise ValorInvalidoError(msg)
        if (self._status is StatusReserva.RECUSADA) != bool(self._faltantes):
            msg = "Reserva recusada exige faltantes, e so ela os tem"
            raise ValorInvalidoError(msg)

    @classmethod
    def criar(
        cls, *, ordem_id: UUID, itens: Sequence[ItemReserva], agora: datetime
    ) -> Reserva:
        return cls(_ordem_id=ordem_id, _itens=tuple(itens), _criada_em=agora)

    @classmethod
    def recusar(
        cls,
        *,
        ordem_id: UUID,
        itens: Sequence[ItemReserva],
        faltantes: Sequence[Faltante],
        agora: datetime,
    ) -> Reserva:
        return cls(
            _ordem_id=ordem_id,
            _itens=tuple(itens),
            _status=StatusReserva.RECUSADA,
            _faltantes=tuple(faltantes),
            _criada_em=agora,
            _encerrada_em=agora,
        )

    @classmethod
    def lapide(cls, *, ordem_id: UUID, agora: datetime) -> Reserva:
        """Liberacao que chegou antes do ``ReservarPecas``: nasce LIBERADA, sem pecas.

        O ``ReservarPecas`` atrasado acha a lapide pela UNIQUE(ordem_id) e e
        descartado sem efeito (RFC-004, secao 4.5).
        """
        return cls(
            _ordem_id=ordem_id,
            _itens=(),
            _status=StatusReserva.LIBERADA,
            _criada_em=agora,
            _encerrada_em=agora,
        )

    @property
    def ordem_id(self) -> UUID:
        return self._ordem_id

    @property
    def itens(self) -> tuple[ItemReserva, ...]:
        return self._itens

    @property
    def status(self) -> StatusReserva:
        return self._status

    @property
    def faltantes(self) -> tuple[Faltante, ...]:
        return self._faltantes

    @property
    def criada_em(self) -> datetime:
        return self._criada_em

    @property
    def encerrada_em(self) -> datetime | None:
        return self._encerrada_em

    def liberar(self, agora: datetime) -> bool:
        """ATIVA -> LIBERADA. Liberada ou recusada: nada a devolver (``False``).

        Consumida (execucao finalizada) nao volta: 409.
        """
        if self._status in {StatusReserva.LIBERADA, StatusReserva.RECUSADA}:
            return False
        self._encerrar(StatusReserva.LIBERADA, agora)
        return True

    def consumir(self, agora: datetime) -> None:
        """ATIVA -> CONSUMIDA (baixa na finalizacao da execucao)."""
        self._encerrar(StatusReserva.CONSUMIDA, agora)

    def _encerrar(self, status: StatusReserva, agora: datetime) -> None:
        validar_transicao(_TRANSICOES, self._status, status, agregado="Reserva")
        self._status = status
        self._encerrada_em = agora
