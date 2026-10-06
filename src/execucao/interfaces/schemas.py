from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.compartilhado.interfaces.schemas import VeiculoResponse
from src.execucao.dominio.execucao import Prioridade, StatusExecucao


class ExecucaoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordem_id: UUID
    status: StatusExecucao
    prioridade: Prioridade
    enfileirada_em: datetime
    veiculo: VeiculoResponse | None
    mecanico_id: UUID | None
    iniciada_em: datetime | None
    finalizada_em: datetime | None
    cancelada_em: datetime | None


class ItemDaFilaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    posicao: int
    ordem_id: UUID
    prioridade: Prioridade
    enfileirada_em: datetime
    veiculo: VeiculoResponse | None = Field(
        description="Retrato do diagnostico, para achar o carro no patio"
    )
