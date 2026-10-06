from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.estoque.dominio.item_estoque import TAMANHO_MAXIMO_NOME
from src.estoque.dominio.sku import PADRAO_SKU, TAMANHO_MAXIMO_SKU

SkuTexto = Annotated[
    str,
    Field(
        pattern=PADRAO_SKU,
        max_length=TAMANHO_MAXIMO_SKU,
        examples=["PEC-OLEO-5W30"],
        description="Mesmo codigo da tabela de precos do Billing",
    ),
]
Nome = Annotated[str, Field(min_length=1, max_length=TAMANHO_MAXIMO_NOME)]
Quantidade = Annotated[int, Field(ge=0, le=1_000_000)]


class CriarItemEstoqueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: SkuTexto
    nome: Nome
    quantidade_disponivel: Quantidade


class AtualizarItemEstoqueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nome: Nome
    ativo: bool


class AjustarQuantidadeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantidade_disponivel: Quantidade = Field(
        description="Saldo fisico apos a contagem; nao pode ficar abaixo do reservado"
    )


class ItemEstoqueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sku: str
    nome: str
    quantidade_disponivel: int = Field(description="Saldo fisico (inclui reservado)")
    quantidade_reservada: int = Field(description="Comprometido com reservas ativas")
    quantidade_livre: int = Field(description="Disponivel para novas reservas")
    ativo: bool

    @field_validator("sku", mode="before")
    @classmethod
    def _sku_como_texto(cls, valor: object) -> str:
        return str(valor)
