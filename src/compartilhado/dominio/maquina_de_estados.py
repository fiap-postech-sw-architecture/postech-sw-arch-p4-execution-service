from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException

if TYPE_CHECKING:
    from collections.abc import Mapping


def validar_transicao[S: StrEnum](
    transicoes: Mapping[S, frozenset[S]], de: S, para: S, *, agregado: str
) -> None:
    """Confere a transicao contra a allow-list do agregado.

    Raises:
        TransicaoStatusInvalidaException: ``para`` nao e alcancavel a partir de
            ``de``; a mensagem lista as transicoes validas.
    """
    validas = transicoes[de]
    if para not in validas:
        permitidas = ", ".join(sorted(validas)) or "nenhuma (estado final)"
        msg = (
            f"{agregado} em {de} nao pode passar para {para}; "
            f"transicoes validas: {permitidas}"
        )
        raise TransicaoStatusInvalidaException(msg)
