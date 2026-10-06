from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.compartilhado.interfaces.schemas import VeiculoResponse
from src.diagnostico.dominio.diagnostico import (
    PADRAO_CODIGO,
    TAMANHO_MAXIMO_CODIGO,
    TAMANHO_MAXIMO_TEXTO_LIVRE,
    StatusDiagnostico,
    TipoItem,
)


class ItemDiagnosticoSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    tipo: TipoItem
    codigo: str = Field(
        pattern=PADRAO_CODIGO,
        max_length=TAMANHO_MAXIMO_CODIGO,
        description="Codigo da tabela de precos do Billing (peca = SKU do estoque)",
        examples=["PEC-PASTILHA-FREIO"],
    )
    quantidade: int = Field(ge=1, le=1000)


class ConclusaoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    itens: list[ItemDiagnosticoSchema] = Field(min_length=1, max_length=50)
    observacoes: str = Field(default="", max_length=TAMANHO_MAXIMO_TEXTO_LIVRE)


class DiagnosticoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordem_id: UUID
    status: StatusDiagnostico
    veiculo: VeiculoResponse | None = Field(
        description="Retrato do veiculo; nulo so na lapide de um descarte adiantado"
    )
    descricao_problema: str | None
    mecanico_id: UUID | None
    itens: list[ItemDiagnosticoSchema]
    observacoes: str
    solicitado_em: datetime
    iniciado_em: datetime | None
    concluido_em: datetime | None
    descartado_em: datetime | None
