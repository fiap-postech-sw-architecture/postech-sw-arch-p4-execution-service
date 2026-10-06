from __future__ import annotations

import pytest

from src.compartilhado.aplicacao.idempotencia import releitura_em_corrida
from src.compartilhado.dominio.exceptions import (
    EntidadeDuplicadaException,
    ViolacaoRegraDeNegocioException,
)


class _Comando:
    def __init__(self, *falhas: Exception) -> None:
        self.falhas = list(falhas)
        self.execucoes = 0

    @releitura_em_corrida
    def executar(self, valor: int) -> int:
        self.execucoes += 1
        if self.falhas:
            raise self.falhas.pop(0)
        return valor


def test_copia_que_perdeu_a_corrida_roda_de_novo_e_le_a_vencedora() -> None:
    comando = _Comando(EntidadeDuplicadaException())
    assert comando.executar(7) == 7
    assert comando.execucoes == 2


def test_sem_corrida_roda_uma_vez() -> None:
    comando = _Comando()
    assert comando.executar(7) == 7
    assert comando.execucoes == 1


def test_duplicata_persistente_sobe_na_segunda_execucao() -> None:
    comando = _Comando(EntidadeDuplicadaException(), EntidadeDuplicadaException())
    with pytest.raises(EntidadeDuplicadaException):
        comando.executar(7)
    assert comando.execucoes == 2


def test_outros_erros_nao_repetem() -> None:
    comando = _Comando(ViolacaoRegraDeNegocioException())
    with pytest.raises(ViolacaoRegraDeNegocioException):
        comando.executar(7)
    assert comando.execucoes == 1
