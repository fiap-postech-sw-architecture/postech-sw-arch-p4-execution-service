"""Corrida entre copias simultaneas do mesmo comando da saga."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from src.compartilhado.dominio.exceptions import EntidadeDuplicadaException

if TYPE_CHECKING:
    from collections.abc import Callable


def releitura_em_corrida[**P, T](executar: Callable[P, T]) -> Callable[P, T]:
    """Roda o comando de novo quando outra copia gravou a mesma chave primeiro.

    Duas copias (reenvio do orquestrador, compensacao e original em voo) leem
    "nada gravado" e inserem; a UNIQUE da ordem barra a segunda, que o
    repositorio traduz em ``EntidadeDuplicadaException``. A segunda execucao le
    a linha da vencedora e segue a regra de repeticao, em vez de virar erro.
    """

    @functools.wraps(executar)
    def com_releitura(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return executar(*args, **kwargs)
        except EntidadeDuplicadaException:
            return executar(*args, **kwargs)

    return com_releitura
