"""Variaveis de ambiente lidas no boot, com erro claro (nunca ``KeyError`` cru)."""

from __future__ import annotations

import os
from typing import Final
from urllib.parse import unquote, urlsplit

# Os unicos ambientes que aceitam as senhas de demonstracao; qualquer outro
# valor de ENVIRONMENT, inclusive a variavel ausente, e producao (falha fechada).
_AMBIENTES_DE_DESENVOLVIMENTO: Final = frozenset({"development", "test"})
# Senhas publicas no repositorio: as do banco e do broker no docker-compose.yml
# e no .env.example.
_SENHAS_DE_DEMONSTRACAO: Final = frozenset(
    {"execucao-demo", "pytstop-execucao-demo-2026"}  # gitleaks:allow
)


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
    # isdecimal, nao isdigit: "²" passaria e o int() levantaria ValueError cru.
    if not bruto.isdecimal():
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


def url_de_conexao(nome: str) -> str:
    """URL obrigatoria de banco ou broker, sem a senha de demonstracao em producao.

    Fora de ``ENVIRONMENT`` ``development`` ou ``test`` (inclusive sem a
    variavel), a senha do compose e do ``.env.example`` e recusada: a credencial
    de verdade vem do Secret (ADR-042).

    Raises:
        RuntimeError: variavel ausente ou com a senha de demonstracao fora de
            development/test (a mensagem nao ecoa a URL).
    """
    url = variavel_obrigatoria(nome)
    ambiente = os.environ.get("ENVIRONMENT", "").strip().lower()
    senha = urlsplit(url).password
    if (
        ambiente not in _AMBIENTES_DE_DESENVOLVIMENTO
        and senha is not None
        and unquote(senha) in _SENHAS_DE_DEMONSTRACAO
    ):
        msg = (
            f"{nome} usa a senha de demonstracao do compose, recusada com "
            f"ENVIRONMENT={ambiente or '(ausente)'}: a credencial vem do Secret "
            "(development e test aceitam a de demonstracao)"
        )
        raise RuntimeError(msg)
    return url
