"""Projecao pura de um ``IntegrationEvent`` no ``dados`` JSON da outbox."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from src.compartilhado.aplicacao.integration_event import IntegrationEvent

# Metadados que viajam no envelope, fora do ``dados`` (brief secao 4).
_CAMPOS_DO_ENVELOPE = frozenset({"id", "ocorrido_em"})


def _json(valor: object) -> Any:  # noqa: ANN401 - estrutura JSON heterogenea
    # Enum antes de str: StrEnum tambem e str e deve virar o valor puro.
    if isinstance(valor, Enum):
        return _json(valor.value)
    if isinstance(valor, UUID):
        return str(valor)
    if isinstance(valor, datetime):
        return valor.isoformat()
    if is_dataclass(valor) and not isinstance(valor, type):
        return {
            campo.name: _json(getattr(valor, campo.name)) for campo in fields(valor)
        }
    if isinstance(valor, (list, tuple)):
        return [_json(item) for item in valor]
    # float fica de fora de proposito: valor monetario viaja como string decimal.
    if valor is None or isinstance(valor, (str, int)):
        return valor
    msg = f"Tipo nao suportado no dados da outbox: {type(valor).__name__}"
    raise TypeError(msg)


def dados_do_evento(evento: IntegrationEvent) -> dict[str, Any]:
    """Campos do evento (exceto ``id``/``ocorrido_em``) em tipos JSON nativos.

    Raises:
        TypeError: campo de tipo nao serializavel (falha no commit, nao no relay).
    """
    return {
        campo.name: _json(getattr(evento, campo.name))
        for campo in fields(evento)
        if campo.name not in _CAMPOS_DO_ENVELOPE
    }
