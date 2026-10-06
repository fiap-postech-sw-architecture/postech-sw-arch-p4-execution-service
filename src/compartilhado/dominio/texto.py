"""Texto vindo de fora (nome, marca, texto livre): aparado, com tamanho e limpo."""

from __future__ import annotations

import re
from typing import Final

from src.compartilhado.dominio.exceptions import ValorInvalidoError

_CONTROLE: Final = re.compile(r"[\x00-\x1f\x7f]")
# Texto livre aceita tabulacao e quebra de linha (\t, \n, \r).
_CONTROLE_EM_TEXTO_LIVRE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def texto_valido(
    valor: str,
    campo: str,
    *,
    maximo: int,
    obrigatorio: bool = True,
    multilinha: bool = False,
) -> str:
    """Devolve o texto aparado.

    Caractere de controle e recusado: NUL viraria 500 no driver (o psycopg2 nao
    grava NUL) e os demais escondem conteudo em tela e log. ``multilinha`` (texto
    livre) aceita tabulacao e quebra de linha.

    Raises:
        ValorInvalidoError: vazio quando ``obrigatorio``, acima de ``maximo``
            caracteres ou com caractere de controle (a mensagem nao ecoa o texto).
    """
    valor = valor.strip()
    if (obrigatorio and not valor) or len(valor) > maximo:
        minimo = 1 if obrigatorio else 0
        msg = f"{campo} deve ter de {minimo} a {maximo} caracteres"
        raise ValorInvalidoError(msg)
    controle = _CONTROLE_EM_TEXTO_LIVRE if multilinha else _CONTROLE
    if controle.search(valor):
        msg = f"{campo} nao pode conter caracteres de controle"
        raise ValorInvalidoError(msg)
    return valor
