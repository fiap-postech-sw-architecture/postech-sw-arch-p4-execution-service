"""Variaveis de ambiente lidas no boot, com erro claro (nunca ``KeyError`` cru)."""

from __future__ import annotations

import os


def variavel_obrigatoria(nome: str) -> str:
    """Valor nao vazio da variavel.

    Raises:
        RuntimeError: variavel ausente ou vazia (o boot para com o nome dela).
    """
    valor = os.environ.get(nome, "").strip()
    if not valor:
        msg = f"Variavel de ambiente obrigatoria nao definida: {nome}"
        raise RuntimeError(msg)
    return valor


def inteiro_opcional(nome: str, padrao: int) -> int:
    """Inteiro >= 0 da variavel, ou ``padrao`` quando ausente.

    Raises:
        RuntimeError: valor que nao e inteiro >= 0.
    """
    bruto = os.environ.get(nome, "").strip()
    if not bruto:
        return padrao
    if not bruto.isdigit():
        msg = f"Variavel de ambiente {nome} deve ser inteiro >= 0 (recebido: {bruto!r})"
        raise RuntimeError(msg)
    return int(bruto)
