from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class ValidadorDeItensPort(Protocol):
    """Tabela de precos do Billing (unica chamada sincrona entre servicos)."""

    def codigos_invalidos(
        self, *, servicos: Sequence[str], pecas: Sequence[str]
    ) -> list[str]:
        """Codigos que o Billing nao reconhece.

        Raises:
            DependenciaIndisponivelException: Billing sem resposta ou circuito
                aberto (a API devolve 503 com mensagem acionavel).
        """


class CatalogoDePecasPort(Protocol):
    """Pecas cadastradas no estoque local."""

    def skus_indisponiveis(self, skus: Sequence[str]) -> list[str]:
        """SKUs sem cadastro ou inativos, na ordem recebida."""
