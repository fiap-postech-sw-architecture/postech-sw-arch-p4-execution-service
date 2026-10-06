from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.compartilhado.dominio.value_object import ValueObject

# Mesmo formato dos codigos da tabela de precos do Billing (ex.: PEC-OLEO-5W30).
PADRAO_SKU: Final = r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$"
TAMANHO_MAXIMO_SKU: Final = 64
_REGEX_SKU = re.compile(PADRAO_SKU)


@dataclass(frozen=True, slots=True)
class Sku(ValueObject):
    """Codigo da peca; liga o estoque fisico (aqui) ao preco (no Billing)."""

    valor: str

    def __post_init__(self) -> None:
        # fullmatch: com match, o `$` aceitaria um "\n" final.
        if len(self.valor) > TAMANHO_MAXIMO_SKU or not _REGEX_SKU.fullmatch(self.valor):
            msg = (
                f"SKU invalido: {self.valor!r}. Use letras maiusculas, digitos e "
                f"hifens (ex.: PEC-OLEO-5W30), com ate {TAMANHO_MAXIMO_SKU} caracteres"
            )
            raise ValorInvalidoError(msg)

    def __str__(self) -> str:
        return self.valor
