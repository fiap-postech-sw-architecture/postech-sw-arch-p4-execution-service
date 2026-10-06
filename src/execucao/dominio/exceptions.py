from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.dominio.exceptions import EntidadeNaoEncontradaException

if TYPE_CHECKING:
    from uuid import UUID


class ExecucaoNaoEncontradaException(EntidadeNaoEncontradaException):
    def __init__(self, ordem_id: UUID) -> None:
        super().__init__(mensagem=f"Execucao da ordem {ordem_id} nao encontrada")
