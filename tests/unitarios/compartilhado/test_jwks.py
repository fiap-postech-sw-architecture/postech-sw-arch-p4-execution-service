from __future__ import annotations

import socket
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.compartilhado.infraestrutura.jwks import (
    JwksIndisponivelError,
    TokenExpiradoError,
    TokenInvalidoError,
    ValidadorDeTokenJWKS,
)
from tests.conftest import assinar, claims_padrao

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def validador(jwks_url: str) -> ValidadorDeTokenJWKS:
    return ValidadorDeTokenJWKS(jwks_url)


def test_token_valido_devolve_as_claims(
    validador: ValidadorDeTokenJWKS, emitir_token: Callable[..., str]
) -> None:
    sub = uuid4()
    claims = validador.validar(emitir_token("mecanico", sub))
    assert claims["sub"] == str(sub)
    assert claims["papel"] == "mecanico"


def test_jwks_fica_em_cache_entre_validacoes(
    jwks_url: str, servidor_jwks: Any, emitir_token: Callable[..., str]
) -> None:
    validador = ValidadorDeTokenJWKS(jwks_url)
    antes = servidor_jwks.requisicoes
    validador.validar(emitir_token("mecanico"))
    validador.validar(emitir_token("admin"))
    assert servidor_jwks.requisicoes - antes == 1


def test_token_expirado(
    validador: ValidadorDeTokenJWKS, emitir_token: Callable[..., str]
) -> None:
    token = emitir_token("mecanico", exp=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(TokenExpiradoError):
        validador.validar(token)


@pytest.mark.parametrize(
    "extras",
    [
        pytest.param({"iss": "outro-emissor"}, id="iss-errado"),
        pytest.param({"aud": "outra-audiencia"}, id="aud-errada"),
    ],
)
def test_emissor_ou_audiencia_errados(
    validador: ValidadorDeTokenJWKS,
    emitir_token: Callable[..., str],
    extras: dict[str, Any],
) -> None:
    token = emitir_token("mecanico", **extras)
    with pytest.raises(TokenInvalidoError):
        validador.validar(token)


def test_claim_obrigatoria_ausente(
    validador: ValidadorDeTokenJWKS, chave_privada: rsa.RSAPrivateKey
) -> None:
    claims = claims_padrao("mecanico", uuid4())
    del claims["sub"]
    token = assinar(chave_privada, claims)
    with pytest.raises(TokenInvalidoError):
        validador.validar(token)


def test_assinatura_de_outra_chave_com_o_mesmo_kid(
    validador: ValidadorDeTokenJWKS,
) -> None:
    intrusa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = assinar(intrusa, claims_padrao("admin", uuid4()))
    with pytest.raises(TokenInvalidoError):
        validador.validar(token)


def test_algoritmo_simetrico_e_recusado(validador: ValidadorDeTokenJWKS) -> None:
    # Confusao de algoritmo: HS256 com "segredo" qualquer e o kid valido.
    token = jwt.encode(
        claims_padrao("admin", uuid4()),
        "segredo-qualquer-com-32-bytes-ou-mais!!",
        algorithm="HS256",
        headers={"kid": "chave-de-teste"},
    )
    with pytest.raises(TokenInvalidoError):
        validador.validar(token)


@pytest.mark.parametrize("kid", ["kid-desconhecido", None])
def test_kid_desconhecido_ou_ausente(
    validador: ValidadorDeTokenJWKS, chave_privada: rsa.RSAPrivateKey, kid: str | None
) -> None:
    token = assinar(chave_privada, claims_padrao("admin", uuid4()), kid=kid)
    with pytest.raises(TokenInvalidoError):
        validador.validar(token)


def test_token_malformado(validador: ValidadorDeTokenJWKS) -> None:
    with pytest.raises(TokenInvalidoError):
        validador.validar("isto-nao-e-um-jwt")


class _CorpoQuebrado(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>nao e json</html>")

    def log_message(self, *_args: object) -> None:
        pass


def test_jwks_que_nao_e_json_e_indisponibilidade(
    emitir_token: Callable[..., str],
) -> None:
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), _CorpoQuebrado)
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{servidor.server_port}/.well-known/jwks.json"
        validador, token = ValidadorDeTokenJWKS(url), emitir_token("admin")
        with pytest.raises(JwksIndisponivelError):
            validador.validar(token)
    finally:
        servidor.shutdown()
        servidor.server_close()


def test_jwks_fora_do_ar(emitir_token: Callable[..., str]) -> None:
    with socket.socket() as livre:
        livre.bind(("127.0.0.1", 0))
        porta = livre.getsockname()[1]
    validador = ValidadorDeTokenJWKS(f"http://127.0.0.1:{porta}/.well-known/jwks.json")
    token = emitir_token("admin")
    with pytest.raises(JwksIndisponivelError):
        validador.validar(token)
