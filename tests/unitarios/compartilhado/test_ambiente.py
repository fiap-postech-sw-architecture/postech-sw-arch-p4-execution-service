from __future__ import annotations

import pytest

from src.compartilhado.infraestrutura.ambiente import (
    inteiro_opcional,
    variavel_obrigatoria,
)


@pytest.mark.parametrize(
    "valor", [pytest.param("", id="vazia"), pytest.param("  ", id="espacos")]
)
def test_obrigatoria_vazia_para_o_boot_com_o_nome(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("ALGUMA", valor)
    with pytest.raises(RuntimeError, match="ALGUMA"):
        variavel_obrigatoria("ALGUMA")


def test_obrigatoria_aparada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGUMA", " valor ")
    assert variavel_obrigatoria("ALGUMA") == "valor"


def test_inteiro_ausente_usa_o_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    assert inteiro_opcional("DB_POOL_SIZE", 5) == 5


def test_inteiro_definido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    assert inteiro_opcional("DB_POOL_SIZE", 5) == 0


@pytest.mark.parametrize("valor", ["-1", "abc", "1.5"])
def test_inteiro_invalido_para_o_boot(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", valor)
    with pytest.raises(RuntimeError, match="DB_POOL_SIZE"):
        inteiro_opcional("DB_POOL_SIZE", 5)
