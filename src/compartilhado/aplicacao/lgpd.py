"""Eliminacao de dados pessoais (LGPD) pedida pelo OS Service, dono do cadastro."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork

_log = structlog.get_logger(__name__)


class RetratosDoVeiculoPort(Protocol):
    """Retratos do veiculo guardados no servico (diagnostico e copia da execucao)."""

    def anonimizar(self, veiculo_id: UUID) -> int:
        """Troca a placa de cada retrato pelo marcador; devolve quantos mudaram."""


class AnonimizarVeiculo:
    """Comando ``AnonimizarVeiculo`` (fora da saga e sem resposta, RFC-004 5.3).

    A placa de todos os retratos do veiculo vira ``ANONIMIZADO:{veiculo_id}``
    (``Veiculo.anonimizar``); marca, modelo e ano ficam, porque nao identificam
    o titular. Idempotente: retrato ja anonimizado nao muda. O OS so elimina os
    dados de cliente sem OS ativa, entao nenhum agendamento concorrente copia a
    placa antiga para uma execucao nova.
    """

    def __init__(self, retratos: RetratosDoVeiculoPort, uow: UnitOfWork) -> None:
        self._retratos = retratos
        self._uow = uow

    def executar(self, veiculo_id: UUID) -> int:
        with self._uow:
            trocados = self._retratos.anonimizar(veiculo_id)
            self._uow.commit()
        _log.info("vehicle_anonymized", veiculo_id=str(veiculo_id), retratos=trocados)
        return trocados
