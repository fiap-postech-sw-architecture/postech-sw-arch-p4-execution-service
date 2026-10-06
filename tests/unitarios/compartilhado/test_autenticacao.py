from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.compartilhado.infraestrutura.jwks import (
    JwksIndisponivelError,
    TokenExpiradoError,
    TokenInvalidoError,
)
from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    exigir_papel,
    obter_usuario_autenticado,
)
from src.compartilhado.interfaces.error_handler import registrar_error_handlers


class _ValidadorStub:
    def __init__(
        self, claims: dict[str, Any] | None = None, erro: Exception | None = None
    ) -> None:
        self._claims = claims or {}
        self._erro = erro
        self.tokens: list[str] = []

    def validar(self, token: str) -> dict[str, Any]:
        self.tokens.append(token)
        if self._erro is not None:
            raise self._erro
        return self._claims


def _cliente(validador: _ValidadorStub) -> TestClient:
    app = FastAPI()
    registrar_error_handlers(app)
    app.state.validador_token = validador

    @app.get("/eu")
    def eu(
        usuario: Annotated[UsuarioAutenticado, Depends(obter_usuario_autenticado)],
    ) -> dict[str, str]:
        return {
            "id": str(usuario.id),
            "papel": usuario.papel,
            "authorization": usuario.authorization,
        }

    @app.get("/so-admin", dependencies=[Depends(exigir_papel())])
    def so_admin() -> dict[str, str]:
        return {"ok": "sim"}

    @app.get("/oficina", dependencies=[Depends(exigir_papel(Papel.MECANICO))])
    def oficina() -> dict[str, str]:
        return {"ok": "sim"}

    return TestClient(app)


def _claims(**extras: Any) -> dict[str, Any]:
    return {"sub": str(uuid4()), "papel": "mecanico", **extras}


def _get(cliente: TestClient, caminho: str = "/eu") -> Any:
    return cliente.get(caminho, headers={"Authorization": "Bearer tok.en.x"})


def test_usuario_autenticado_carrega_id_papel_e_header() -> None:
    claims = _claims()
    validador = _ValidadorStub(claims)
    resposta = _get(_cliente(validador))
    assert resposta.status_code == 200
    assert resposta.json() == {
        "id": claims["sub"],
        "papel": "mecanico",
        "authorization": "Bearer tok.en.x",
    }
    assert validador.tokens == ["tok.en.x"]


def test_sem_token_responde_401_com_desafio_bearer() -> None:
    resposta = _cliente(_ValidadorStub(_claims())).get("/eu")
    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == "Bearer"
    assert resposta.json()["erro"]["codigo"] == "NAO_AUTENTICADO"
    assert resposta.json()["erro"]["mensagem"] == "Token de autenticacao nao fornecido"


@pytest.mark.parametrize(
    ("erro", "status", "mensagem"),
    [
        (TokenExpiradoError("exp"), 401, "Token expirado"),
        (TokenInvalidoError("sig"), 401, "Token invalido"),
        (JwksIndisponivelError("down"), 503, None),
    ],
)
def test_falha_na_validacao(erro: Exception, status: int, mensagem: str | None) -> None:
    resposta = _get(_cliente(_ValidadorStub(erro=erro)))
    assert resposta.status_code == status
    if mensagem is not None:
        assert resposta.json()["erro"]["mensagem"] == mensagem
    else:
        assert "JWKS do OS Service" in resposta.json()["erro"]["mensagem"]
        assert resposta.json()["erro"]["codigo"] == "SERVICO_INDISPONIVEL"


def test_refresh_token_nao_autentica_requisicao() -> None:
    resposta = _get(_cliente(_ValidadorStub(_claims(type="refresh"))))
    assert resposta.status_code == 401
    assert resposta.json()["erro"]["mensagem"] == "Token nao e do tipo access"


def test_token_sem_type_e_tratado_como_access() -> None:
    claims = _claims()
    assert "type" not in claims
    assert _get(_cliente(_ValidadorStub(claims))).status_code == 200


def test_sub_que_nao_e_uuid() -> None:
    resposta = _get(_cliente(_ValidadorStub(_claims(sub="joao"))))
    assert resposta.status_code == 401


@pytest.mark.parametrize("papel", ["cliente", None, 7])
def test_papel_desconhecido_e_403(papel: object) -> None:
    resposta = _get(_cliente(_ValidadorStub(_claims(papel=papel))))
    assert resposta.status_code == 403
    assert resposta.json()["erro"]["codigo"] == "ACESSO_NEGADO"


@pytest.mark.parametrize(
    ("papel", "caminho", "status"),
    [
        ("mecanico", "/oficina", 200),
        ("atendente", "/oficina", 403),
        ("admin", "/oficina", 200),
        ("admin", "/so-admin", 200),
        ("mecanico", "/so-admin", 403),
        ("atendente", "/so-admin", 403),
    ],
)
def test_rbac_admin_sempre_passa(papel: str, caminho: str, status: int) -> None:
    resposta = _get(_cliente(_ValidadorStub(_claims(papel=papel))), caminho)
    assert resposta.status_code == status
