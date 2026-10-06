"""Eventos do catalogo da saga emitidos pelo estoque (RFC-004, secao 5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.aplicacao.integration_event import IntegrationEvent

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class FaltanteDados:
    sku: str
    solicitado: int
    disponivel: int


@dataclass(frozen=True, kw_only=True)
class PecasReservadasEvent(IntegrationEvent):
    reserva_id: UUID


@dataclass(frozen=True, kw_only=True)
class ReservaDePecasFalhouEvent(IntegrationEvent):
    faltantes: tuple[FaltanteDados, ...]


@dataclass(frozen=True, kw_only=True)
class ReservaLiberadaEvent(IntegrationEvent):
    """Resposta a ``LiberarReserva``; o ``dados`` so carrega ``ordem_id``."""
