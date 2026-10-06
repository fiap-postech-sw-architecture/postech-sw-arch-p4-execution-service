from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from src.diagnostico.dominio.diagnostico import Diagnostico, StatusDiagnostico


class DiagnosticoRepository(Protocol):
    def obter(self, ordem_id: UUID, *, com_lock: bool = False) -> Diagnostico | None:
        """Diagnostico da ordem; ``com_lock=True`` aplica SELECT ... FOR UPDATE."""

    def salvar(self, diagnostico: Diagnostico) -> None:
        """Adiciona o diagnostico a sessao e faz flush."""

    def listar(
        self, status: StatusDiagnostico | None, offset: int, limit: int
    ) -> list[Diagnostico]:
        """Pagina por ordem de chegada (``solicitado_em``), com filtro opcional."""

    def contar(self, status: StatusDiagnostico | None) -> int:
        """Total com o mesmo filtro de ``listar``."""
