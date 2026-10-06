from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.dominio.exceptions import (
    DadosInvalidosException,
    EntidadeNaoEncontradaException,
)

if TYPE_CHECKING:
    from uuid import UUID


class DiagnosticoNaoEncontradoException(EntidadeNaoEncontradaException):
    def __init__(self, ordem_id: UUID) -> None:
        super().__init__(mensagem=f"Diagnostico da ordem {ordem_id} nao encontrado")


class ItensInvalidosException(DadosInvalidosException):
    """Codigos sem preco no Billing ou pecas sem cadastro ativo no estoque."""

    def __init__(self, mensagem: str) -> None:
        super().__init__(mensagem=mensagem, codigo="ITENS_INVALIDOS")
