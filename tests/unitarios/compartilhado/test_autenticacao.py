from __future__ import annotations

import io
import json
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
    CREDENCIAL_INVALIDA,
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
    return {"sub": str(uuid4()), "papel": "mecanico", "type": "access", **extras}


def _nao_autenticado(resposta: Any) -> None:
    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == "Bearer"
    assert resposta.json()["erro"]["codigo"] == "NAO_AUTENTICADO"
    assert resposta.json()["erro"]["mensagem"] == CREDENCIAL_INVALIDA


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
    _nao_autenticado(_cliente(_ValidadorStub(_claims())).get("/eu"))


def test_esquema_que_nao_e_bearer_e_401() -> None:
    cliente = _cliente(_ValidadorStub(_claims()))
    _nao_autenticado(cliente.get("/eu", headers={"Authorization": "Basic YTpi"}))


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(TokenExpiradoError("exp"), id="expirado"),
        pytest.param(TokenInvalidoError("sig"), id="invalido"),
    ],
)
def test_falha_de_credencial_e_401_com_a_mesma_mensagem(erro: Exception) -> None:
    _nao_autenticado(_get(_cliente(_ValidadorStub(erro=erro))))


def test_jwks_indisponivel_e_503_com_retry_after() -> None:
    erro = JwksIndisponivelError("down", retry_after=4)
    resposta = _get(_cliente(_ValidadorStub(erro=erro)))
    assert resposta.status_code == 503
    assert resposta.headers["Retry-After"] == "4"
    assert resposta.json()["erro"]["codigo"] == "SERVICO_INDISPONIVEL"
    assert "JWKS do OS Service" in resposta.json()["erro"]["mensagem"]


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param(_claims(type="refresh"), id="refresh-token"),
        pytest.param(
            {k: v for k, v in _claims().items() if k != "type"}, id="sem-type"
        ),
        pytest.param(_claims(sub="joao"), id="sub-que-nao-e-uuid"),
        pytest.param(_claims(papel=None), id="papel-ausente"),
        pytest.param(_claims(papel="cliente"), id="papel-desconhecido"),
        pytest.param(_claims(papel=7), id="papel-que-nao-e-texto"),
    ],
)
def test_claims_invalidas_sao_falha_de_credencial(claims: dict[str, Any]) -> None:
    _nao_autenticado(_get(_cliente(_ValidadorStub(claims))))


@pytest.mark.parametrize(
    ("papel", "caminho", "status"),
    [
        pytest.param("mecanico", "/oficina", 200, id="mecanico-na-oficina"),
        pytest.param("atendente", "/oficina", 403, id="atendente-na-oficina"),
        pytest.param("admin", "/oficina", 200, id="admin-na-oficina"),
        pytest.param("admin", "/so-admin", 200, id="admin-no-admin"),
        pytest.param("mecanico", "/so-admin", 403, id="mecanico-no-admin"),
        pytest.param("atendente", "/so-admin", 403, id="atendente-no-admin"),
    ],
)
def test_rbac_admin_sempre_passa(papel: str, caminho: str, status: int) -> None:
    resposta = _get(_cliente(_ValidadorStub(_claims(papel=papel))), caminho)
    assert resposta.status_code == status
    if status == 403:
        assert resposta.json()["erro"]["codigo"] == "ACESSO_NEGADO"


@pytest.mark.parametrize(
    ("validador", "cabecalhos", "motivo"),
    [
        pytest.param(_ValidadorStub(_claims()), {}, "token_ausente", id="ausente"),
        pytest.param(
            _ValidadorStub(erro=TokenExpiradoError("exp")),
            {"Authorization": "Bearer tok.en.x"},
            "token_expirado",
            id="expirado",
        ),
        pytest.param(
            _ValidadorStub(erro=TokenInvalidoError("sig")),
            {"Authorization": "Bearer tok.en.x"},
            "token_invalido",
            id="invalido",
        ),
        pytest.param(
            _ValidadorStub(_claims(type="refresh")),
            {"Authorization": "Bearer tok.en.x"},
            "tipo_nao_access",
            id="refresh",
        ),
        pytest.param(
            _ValidadorStub(_claims(sub="joao")),
            {"Authorization": "Bearer tok.en.x"},
            "sub_invalido",
            id="sub",
        ),
        pytest.param(
            _ValidadorStub(_claims(papel="cliente")),
            {"Authorization": "Bearer tok.en.x"},
            "papel_invalido",
            id="papel",
        ),
    ],
)
def test_motivo_do_401_vai_so_para_o_log_e_sem_o_token(
    log_capturado: io.StringIO,
    validador: _ValidadorStub,
    cabecalhos: dict[str, str],
    motivo: str,
) -> None:
    # A resposta e a mesma para todos; quem opera acha o motivo no log.
    _nao_autenticado(_cliente(validador).get("/eu", headers=cabecalhos))
    registros = [json.loads(linha) for linha in log_capturado.getvalue().splitlines()]
    [falha] = [r for r in registros if r["event"] == "authentication_failed"]
    assert falha["reason"] == motivo
    assert "tok.en.x" not in log_capturado.getvalue()
