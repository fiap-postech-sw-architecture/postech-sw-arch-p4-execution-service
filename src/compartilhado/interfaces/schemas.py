"""Envelope de erro (documentacao OpenAPI) e respostas comuns das rotas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Erro(BaseModel):
    codigo: str
    mensagem: str
    id_requisicao: str


class ErroResponse(BaseModel):
    erro: Erro


def respostas(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Entradas ``responses`` do OpenAPI com o envelope de erro do servico."""
    descricoes = {
        401: "Token ausente, invalido ou expirado",
        403: "Papel sem permissao ou usuario nao responsavel pelo objeto",
        404: "Recurso nao encontrado",
        409: "Estado atual nao permite a operacao",
        422: (
            "Dados recusados por regra de negocio (envelope `erro`) ou corpo "
            "invalido no schema (`detail` + `id_requisicao`, formato do p3)"
        ),
        503: "Dependencia indisponivel (JWKS do OS Service ou Billing)",
    }
    return {
        codigo: {"model": ErroResponse, "description": descricoes[codigo]}
        for codigo in status_codes
    }
