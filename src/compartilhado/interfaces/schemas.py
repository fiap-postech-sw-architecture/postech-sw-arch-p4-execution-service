"""Envelope de erro (documentacao OpenAPI), paginacao e respostas comuns."""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import Query
from pydantic import BaseModel, ConfigDict

# Teto do offset: alem do bigint do Postgres o OFFSET virava DataError (500).
OFFSET_MAXIMO: Final = 1_000_000
LIMITE_MAXIMO: Final = 100

Offset = Annotated[
    int, Query(ge=0, le=OFFSET_MAXIMO, description="Itens a pular (ate 1.000.000)")
]
Limite = Annotated[
    int, Query(ge=1, le=LIMITE_MAXIMO, description="Tamanho da pagina (1 a 100)")
]


class Erro(BaseModel):
    codigo: str
    mensagem: str
    id_requisicao: str


class ErroResponse(BaseModel):
    erro: Erro


class Pagina[T](BaseModel):
    """Pagina de uma listagem; ``total`` conta com o mesmo filtro dos ``items``."""

    items: list[T]
    total: int
    offset: int
    limit: int


class VeiculoResponse(BaseModel):
    """Retrato do veiculo; anonimizado (LGPD), a placa vem ``ANONIMIZADO:{id}``."""

    model_config = ConfigDict(from_attributes=True)

    placa: str
    marca: str
    modelo: str
    ano: int


def respostas(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Entradas ``responses`` do OpenAPI com o envelope de erro do servico."""
    descricoes = {
        401: "Credencial ausente, invalida ou expirada (uma resposta para toda falha)",
        403: "Papel sem permissao ou usuario nao responsavel pelo objeto",
        404: "Recurso nao encontrado",
        409: "Estado atual nao permite a operacao",
        422: (
            "Dados recusados por regra de negocio (envelope `erro`) ou corpo "
            "invalido no schema (`detail` + `id_requisicao`, formato do p3)"
        ),
        503: (
            "Dependencia indisponivel (JWKS do OS Service ou Billing); o header "
            "`Retry-After` traz os segundos ate a proxima tentativa"
        ),
    }
    return {
        codigo: {"model": ErroResponse, "description": descricoes[codigo]}
        for codigo in status_codes
    }
