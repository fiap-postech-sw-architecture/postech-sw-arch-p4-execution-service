from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.execucao.dominio.execucao import StatusExecucao


class ExecucaoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordem_id: UUID
    status: StatusExecucao
    prioridade: int
    enfileirada_em: datetime
    mecanico_id: UUID | None
    iniciada_em: datetime | None
    finalizada_em: datetime | None
    cancelada_em: datetime | None


class ItemDaFilaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    posicao: int
    ordem_id: UUID
    prioridade: int
    enfileirada_em: datetime


class FilaResponse(BaseModel):
    items: list[ItemDaFilaResponse]
    total: int
    offset: int
    limit: int
