from __future__ import annotations

import pytest

from src.compartilhado.infraestrutura.ambiente import (
    inteiro_opcional,
    url_http_obrigatoria,
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


@pytest.mark.parametrize("valor", ["-1", "abc", "1.5", "²"])
def test_inteiro_invalido_para_o_boot(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", valor)
    with pytest.raises(RuntimeError, match="DB_POOL_SIZE"):
        inteiro_opcional("DB_POOL_SIZE", 5)


@pytest.mark.parametrize(
    "url", ["http://billing:8000", "https://billing.exemplo.com.br/base"]
)
def test_url_http_com_host(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("BILLING_URL", url)
    assert url_http_obrigatoria("BILLING_URL") == url


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("billing:8000", id="sem-esquema"),
        pytest.param("ftp://billing", id="esquema-errado"),
        pytest.param("http://", id="sem-host"),
        pytest.param("http://usuario:segredo@", id="credencial-sem-host"),
    ],
)
def test_url_mal_formada_para_o_boot_sem_ecoar_o_valor(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("BILLING_URL", url)
    with pytest.raises(RuntimeError, match="BILLING_URL") as erro:
        url_http_obrigatoria("BILLING_URL")
    assert url not in str(erro.value)
