from __future__ import annotations

from dataclasses import dataclass

from src.compartilhado.dominio.codigo import TAMANHO_MAXIMO_CODIGO, codigo_valido
from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.compartilhado.dominio.value_object import ValueObject


@dataclass(frozen=True, slots=True)
class Sku(ValueObject):
    """Codigo da peca; liga o estoque fisico (aqui) ao preco (no Billing)."""

    valor: str

    def __post_init__(self) -> None:
        # Um SKU fora da regra do Billing seria recusado na validacao do
        # diagnostico; aqui ja vira 422.
        if not codigo_valido(self.valor):
            msg = (
                f"SKU invalido: {self.valor!r}. Use letras maiusculas, digitos e "
                f"hifens (ex.: PEC-OLEO-5W30), com ate {TAMANHO_MAXIMO_CODIGO} "
                "caracteres"
            )
            raise ValorInvalidoError(msg)

    def __str__(self) -> str:
        return self.valor
