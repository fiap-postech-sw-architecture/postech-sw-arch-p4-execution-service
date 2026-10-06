"""Eventos do catalogo da saga emitidos pelo diagnostico (brief secao 4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.compartilhado.aplicacao.integration_event import IntegrationEvent

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class ItemDados:
    tipo: str
    codigo: str
    quantidade: int


@dataclass(frozen=True, kw_only=True)
class DiagnosticoIniciadoEvent(IntegrationEvent):
    mecanico_id: UUID
    iniciado_em: datetime


@dataclass(frozen=True, kw_only=True)
class DiagnosticoConcluidoEvent(IntegrationEvent):
    itens: tuple[ItemDados, ...]
    observacoes: str = field(repr=False)  # texto livre: fora de traceback e log
    concluido_em: datetime


@dataclass(frozen=True, kw_only=True)
class DiagnosticoDescartadoEvent(IntegrationEvent):
    """Resposta a ``DescartarDiagnostico``; o ``dados`` so carrega ``ordem_id``."""
