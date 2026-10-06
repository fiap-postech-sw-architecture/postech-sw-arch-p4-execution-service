"""Codigo da tabela de precos do Billing (servico ou peca): uma regra so.

O SKU do estoque e o codigo de peca do Billing, e o diagnostico aponta servicos
e pecas pelo mesmo codigo: formato e tamanho sao os do Billing, dono dos
codigos (ex.: PEC-OLEO-5W30, SRV-FREIOS).
"""

from __future__ import annotations

import re
from typing import Final

PADRAO_CODIGO: Final = r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$"
TAMANHO_MAXIMO_CODIGO: Final = 50
_REGEX_CODIGO = re.compile(PADRAO_CODIGO)


def codigo_valido(codigo: str) -> bool:
    """Letras maiusculas, digitos e hifens, com ate 50 caracteres."""
    # fullmatch: com match, o `$` aceitaria um "\n" final.
    return len(codigo) <= TAMANHO_MAXIMO_CODIGO and bool(
        _REGEX_CODIGO.fullmatch(codigo)
    )
