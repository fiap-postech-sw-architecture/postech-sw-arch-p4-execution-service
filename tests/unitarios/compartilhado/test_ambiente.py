from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import src.consumidor
import src.relay
from src.compartilhado.infraestrutura.ambiente import (
    inteiro_opcional,
    url_de_conexao,
    url_http_obrigatoria,
    variavel_obrigatoria,
)
from src.main import criar_app


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


# As senhas do docker-compose.yml e do .env.example.
_SENHA_DO_BANCO = "execucao-demo"  # gitleaks:allow
_SENHA_DO_BROKER = "pytstop-execucao-demo-2026"  # gitleaks:allow
_DEMONSTRACAO = {
    "DATABASE_URL": f"postgresql://execucao:{_SENHA_DO_BANCO}@db/execucao",
    "RABBITMQ_URL": f"amqp://execucao:{_SENHA_DO_BROKER}@mq/%2F",
}
_DE_VERDADE = {
    "DATABASE_URL": "postgresql://execucao_app:a1b2c3@db/execucao",
    "RABBITMQ_URL": "amqp://execucao:d4e5f6@mq/%2F",
}


@pytest.mark.parametrize("ambiente", [None, "production", "staging", ""])
@pytest.mark.parametrize(
    ("nome", "url"),
    [
        *_DEMONSTRACAO.items(),
        pytest.param(
            "RABBITMQ_URL",
            f"amqp://execucao:{_SENHA_DO_BROKER.replace('-', '%2D')}@mq/%2F",
            id="codificada",
        ),
    ],
)
def test_senha_de_demonstracao_fora_de_dev_e_test_para_o_boot_sem_ecoar_a_url(
    monkeypatch: pytest.MonkeyPatch, ambiente: str | None, nome: str, url: str
) -> None:
    # Sem ENVIRONMENT, ou com outro valor, e producao: falha fechada.
    if ambiente is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", ambiente)
    monkeypatch.setenv(nome, url)
    with pytest.raises(RuntimeError, match=f"{nome} usa a senha de demonstr") as erro:
        url_de_conexao(nome)
    for segredo in (url, _SENHA_DO_BANCO, _SENHA_DO_BROKER):
        assert segredo not in str(erro.value)


@pytest.mark.parametrize("ambiente", ["development", "test", " Development "])
@pytest.mark.parametrize("nome", ["DATABASE_URL", "RABBITMQ_URL"])
def test_dev_e_test_aceitam_a_senha_de_demonstracao(
    monkeypatch: pytest.MonkeyPatch, ambiente: str, nome: str
) -> None:
    monkeypatch.setenv("ENVIRONMENT", ambiente)
    monkeypatch.setenv(nome, _DEMONSTRACAO[nome])
    assert url_de_conexao(nome) == _DEMONSTRACAO[nome]


@pytest.mark.parametrize(
    ("nome", "url"),
    [
        *_DE_VERDADE.items(),
        pytest.param("DATABASE_URL", "postgresql:///execucao", id="sem-senha"),
    ],
)
def test_producao_aceita_a_senha_que_nao_e_de_demonstracao(
    monkeypatch: pytest.MonkeyPatch, nome: str, url: str
) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv(nome, url)
    assert url_de_conexao(nome) == url


def test_url_de_conexao_ausente_para_o_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        url_de_conexao("DATABASE_URL")


@pytest.mark.parametrize(
    "modulo", [src.relay, src.consumidor], ids=["relay", "consumidor"]
)
@pytest.mark.parametrize("nome", ["DATABASE_URL", "RABBITMQ_URL"])
def test_relay_e_consumidor_nao_sobem_com_a_senha_de_demonstracao(
    monkeypatch: pytest.MonkeyPatch, modulo: Any, nome: str
) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    for variavel, url in _DE_VERDADE.items():
        monkeypatch.setenv(variavel, url)
    monkeypatch.setenv(nome, _DEMONSTRACAO[nome])
    monkeypatch.setattr(modulo, "configurar_logging", lambda: None)
    monkeypatch.setattr(modulo, "configurar_telemetria", lambda _processo: None)
    with pytest.raises(RuntimeError, match=f"{nome} usa a senha de demonstracao"):
        modulo.main()


def test_api_nao_sobe_com_a_senha_de_demonstracao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("DATABASE_URL", _DEMONSTRACAO["DATABASE_URL"])
    with (
        pytest.raises(RuntimeError, match="DATABASE_URL usa a senha de demonstracao"),
        TestClient(criar_app()),
    ):
        pass
