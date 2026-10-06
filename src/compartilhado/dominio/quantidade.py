"""Quantidade de uma linha (peca, servico, reserva): uma regra so no servico."""

from __future__ import annotations

from typing import Final

from src.compartilhado.dominio.exceptions import ValorInvalidoError

# Teto por linha, o mesmo do schema da API: protege a soma no saldo (int do
# banco) e recusa digitacao absurda antes de chegar ao estoque.
QUANTIDADE_MAXIMA_POR_LINHA: Final = 1000


def quantidade_valida(quantidade: int, descricao: str) -> int:
    """Devolve a quantidade se estiver entre 1 e ``QUANTIDADE_MAXIMA_POR_LINHA``.

    Raises:
        ValorInvalidoError: zero, negativa ou acima do teto.
    """
    if not 0 < quantidade <= QUANTIDADE_MAXIMA_POR_LINHA:
        msg = (
            f"Quantidade {descricao} deve ser positiva e no maximo "
            f"{QUANTIDADE_MAXIMA_POR_LINHA} (recebido: {quantidade})"
        )
        raise ValorInvalidoError(msg)
    return quantidade
