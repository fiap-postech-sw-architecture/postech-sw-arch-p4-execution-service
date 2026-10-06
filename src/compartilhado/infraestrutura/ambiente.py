"""Variaveis de ambiente lidas no boot, com erro claro (nunca ``KeyError`` cru)."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


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


def url_http_obrigatoria(nome: str) -> str:
    """URL ``http``/``https`` com host, conferida no boot.

    Sem esquema (``billing:8000``) o httpx levantaria ``UnsupportedProtocol`` a
    cada chamada, e o servico pareceria fora do ar em vez de mal configurado.

    Raises:
        RuntimeError: variavel ausente, sem esquema http(s) ou sem host (a
            mensagem nao ecoa o valor, que pode ter credencial embutida).
    """
    url = variavel_obrigatoria(nome)
    partes = urlsplit(url)
    if partes.scheme not in {"http", "https"} or not partes.hostname:
        msg = f"Variavel de ambiente {nome} deve ser uma URL http(s) com host"
        raise RuntimeError(msg)
    return url
