from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from src.execucao.dominio.execucao import Execucao


class ExecucaoRepository(Protocol):
    def obter(self, ordem_id: UUID, *, com_lock: bool = False) -> Execucao | None:
        """Execucao da ordem; ``com_lock=True`` aplica SELECT ... FOR UPDATE."""

    def salvar(self, execucao: Execucao) -> None:
        """Adiciona a execucao a sessao e faz flush."""
