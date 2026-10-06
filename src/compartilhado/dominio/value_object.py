from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValueObject:
    """Base dos objetos de valor: imutaveis e iguais pelo valor dos campos."""
