"""Eventos do catalogo da saga emitidos pela execucao (RFC-004, secao 5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.aplicacao.integration_event import IntegrationEvent

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class PecaConsumida:
    sku: str
    quantidade: int


@dataclass(frozen=True, kw_only=True)
class ExecucaoAgendadaEvent(IntegrationEvent):
    posicao_na_fila: int


@dataclass(frozen=True, kw_only=True)
class ExecucaoCanceladaEvent(IntegrationEvent):
    """Resposta a ``CancelarExecucao``; o ``dados`` so carrega ``ordem_id``."""


@dataclass(frozen=True, kw_only=True)
class ExecucaoIniciadaEvent(IntegrationEvent):
    mecanico_id: UUID
    iniciada_em: datetime


@dataclass(frozen=True, kw_only=True)
class ExecucaoFinalizadaEvent(IntegrationEvent):
    finalizada_em: datetime
    pecas_consumidas: tuple[PecaConsumida, ...]
