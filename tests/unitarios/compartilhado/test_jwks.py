from __future__ import annotations

import socket
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from prometheus_client import REGISTRY

from src.compartilhado.infraestrutura.jwks import (
    FRESCO_SEGUNDOS,
    MEMORIA_DA_FALHA_SEGUNDOS,
    VELHO_MAXIMO_SEGUNDOS,
    JwksIndisponivelError,
    TokenExpiradoError,
    TokenInvalidoError,
    ValidadorDeTokenJWKS,
)
from tests.conftest import assinar, claims_padrao

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import ServidorJwks


class _Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


@pytest.fixture
def validador(jwks_url: str) -> ValidadorDeTokenJWKS:
    return ValidadorDeTokenJWKS(jwks_url)


def _falhas() -> float:
    return REGISTRY.get_sample_value("pytstop_jwks_falhas_total") or 0.0


def _porta_livre() -> int:
    with socket.socket() as livre:
        livre.bind(("127.0.0.1", 0))
        return int(livre.getsockname()[1])


def test_token_valido_devolve_as_claims(
    validador: ValidadorDeTokenJWKS, emitir_token: Callable[..., str]
) -> None:
    sub = uuid4()
    claims = validador.validar(emitir_token("mecanico", sub))
    assert claims["sub"] == str(sub)
    assert claims["papel"] == "mecanico"


def test_jwks_fica_em_cache_entre_validacoes(
    servidor_jwks_proprio: ServidorJwks, emitir_token: Callable[..., str]
) -> None:
    validador = ValidadorDeTokenJWKS(servidor_jwks_proprio.url)
    validador.validar(emitir_token("mecanico"))
    validador.validar(emitir_token("admin"))
    assert servidor_jwks_proprio.requisicoes == 1


def test_token_expirado(
    validador: ValidadorDeTokenJWKS, emitir_token: Callable[..., str]
) -> None:
    token = emitir_token("mecanico", exp=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(TokenExpiradoError):
        validador.validar(token)


@pytest.mark.parametrize(
    "extras",
    [
        pytest.param({"iat": timedelta(seconds=5)}, id="iat-5s-no-futuro"),
        pytest.param({"nbf": timedelta(seconds=5)}, id="nbf-5s-no-futuro"),
        pytest.param({"exp": timedelta(seconds=-5)}, id="exp-vencido-ha-5s"),
    ],
)
def test_leeway_de_10s_aceita_relogio_dessincronizado(
    validador: ValidadorDeTokenJWKS,
    emitir_token: Callable[..., str],
    extras: dict[str, timedelta],
) -> None:
    agora = datetime.now(UTC)
    token = emitir_token(
        "mecanico", **{claim: agora + delta for claim, delta in extras.items()}
    )
    assert validador.validar(token)["papel"] == "mecanico"


def test_alem_do_leeway_continua_expirado(
    validador: ValidadorDeTokenJWKS, emitir_token: Callable[..., str]
) -> None:
    token = emitir_token("mecanico", exp=datetime.now(UTC) - timedelta(seconds=15))
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


@pytest.mark.parametrize("claim", ["exp", "iss", "aud", "sub"])
def test_claim_obrigatoria_ausente(
    validador: ValidadorDeTokenJWKS, chave_privada: rsa.RSAPrivateKey, claim: str
) -> None:
    # Sem `exp` obrigatorio, um token sem validade valeria para sempre.
    claims = claims_padrao("mecanico", uuid4())
    del claims[claim]
    token = assinar(chave_privada, claims)
    with pytest.raises(TokenInvalidoError, match=claim):
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


@pytest.mark.parametrize(
    "kid",
    [pytest.param("kid-desconhecido", id="desconhecido"), pytest.param(None, id="sem")],
)
def test_kid_desconhecido_ou_ausente(
    validador: ValidadorDeTokenJWKS, chave_privada: rsa.RSAPrivateKey, kid: str | None
) -> None:
    token = assinar(chave_privada, claims_padrao("admin", uuid4()), kid=kid)
    with pytest.raises(TokenInvalidoError):
        validador.validar(token)


def test_kid_novo_busca_o_jwks_de_novo_no_maximo_a_cada_30s(
    servidor_jwks_proprio: ServidorJwks,
    chave_privada: rsa.RSAPrivateKey,
    emitir_token: Callable[..., str],
) -> None:
    relogio = _Relogio()
    validador = ValidadorDeTokenJWKS(servidor_jwks_proprio.url, relogio=relogio)
    validador.validar(emitir_token("admin"))
    rotacionada = assinar(chave_privada, claims_padrao("admin", uuid4()), kid="nova")

    with pytest.raises(TokenInvalidoError):
        validador.validar(rotacionada)  # acabou de buscar: nao busca de novo
    assert servidor_jwks_proprio.requisicoes == 1
    relogio.agora += 30
    with pytest.raises(TokenInvalidoError):
        validador.validar(rotacionada)
    assert servidor_jwks_proprio.requisicoes == 2


def test_token_malformado(validador: ValidadorDeTokenJWKS) -> None:
    with pytest.raises(TokenInvalidoError):
        validador.validar("isto-nao-e-um-jwt")


@pytest.mark.parametrize(
    "corpo",
    [
        pytest.param(b"<html>nao e json</html>", id="nao-e-json"),
        pytest.param(b"[]", id="nao-e-objeto"),
        pytest.param(b'{"keys": []}', id="sem-chaves"),
        pytest.param(b'{"keys": [{"kty": "XYZ", "kid": "x"}]}', id="chave-ilegivel"),
    ],
)
def test_jwks_sem_chave_de_assinatura_utilizavel_e_indisponibilidade(
    servidor_jwks_proprio: ServidorJwks, emitir_token: Callable[..., str], corpo: bytes
) -> None:
    servidor_jwks_proprio.corpo = corpo
    validador = ValidadorDeTokenJWKS(servidor_jwks_proprio.url)
    with pytest.raises(JwksIndisponivelError) as erro:
        validador.validar(emitir_token("admin"))
    assert erro.value.retry_after == MEMORIA_DA_FALHA_SEGUNDOS


@pytest.mark.parametrize(
    "jwk_extra",
    [
        pytest.param({"use": "enc"}, id="so-cifra"),
        pytest.param({"kid": None}, id="sem-kid"),
    ],
)
def test_jwks_so_com_chave_que_nao_assina_e_indisponibilidade(
    servidor_jwks_proprio: ServidorJwks,
    jwk_publico: dict[str, Any],
    emitir_token: Callable[..., str],
    jwk_extra: dict[str, Any],
) -> None:
    jwk = {k: v for k, v in {**jwk_publico, **jwk_extra}.items() if v is not None}
    servidor_jwks_proprio.jwks = {"keys": [jwk]}
    validador = ValidadorDeTokenJWKS(servidor_jwks_proprio.url)
    with pytest.raises(JwksIndisponivelError):
        validador.validar(emitir_token("admin"))


def test_jwks_fora_do_ar_conta_a_falha(emitir_token: Callable[..., str]) -> None:
    url = f"http://127.0.0.1:{_porta_livre()}/.well-known/jwks.json"
    validador, antes = ValidadorDeTokenJWKS(url), _falhas()
    with pytest.raises(JwksIndisponivelError):
        validador.validar(emitir_token("admin"))
    assert _falhas() == antes + 1


def test_jwks_lento_desiste_em_2s(
    servidor_jwks_proprio: ServidorJwks, emitir_token: Callable[..., str]
) -> None:
    servidor_jwks_proprio.atraso = 3
    validador = ValidadorDeTokenJWKS(servidor_jwks_proprio.url)
    inicio = time.monotonic()
    with pytest.raises(JwksIndisponivelError):
        validador.validar(emitir_token("admin"))
    assert 1.5 < time.monotonic() - inicio < 2.9


def test_falha_memorizada_responde_na_hora_sem_buscar_de_novo(
    servidor_jwks_proprio: ServidorJwks, emitir_token: Callable[..., str]
) -> None:
    relogio = _Relogio()
    servidor_jwks_proprio.status = 500
    validador = ValidadorDeTokenJWKS(servidor_jwks_proprio.url, relogio=relogio)
    token = emitir_token("admin")
    with pytest.raises(JwksIndisponivelError):
        validador.validar(token)

    relogio.agora += 2
    with pytest.raises(JwksIndisponivelError) as erro:
        validador.validar(token)
    assert erro.value.retry_after == MEMORIA_DA_FALHA_SEGUNDOS - 2
    assert servidor_jwks_proprio.requisicoes == 1

    relogio.agora += MEMORIA_DA_FALHA_SEGUNDOS
    servidor_jwks_proprio.status = 200
    assert validador.validar(token)["papel"] == "admin"
    assert servidor_jwks_proprio.requisicoes == 2


def test_jwks_cai_depois_de_cacheado_e_a_chave_conhecida_vale_por_ate_1h(
    servidor_jwks_proprio: ServidorJwks, emitir_token: Callable[..., str]
) -> None:
    relogio = _Relogio()
    validador = ValidadorDeTokenJWKS(servidor_jwks_proprio.url, relogio=relogio)
    token = emitir_token("admin")
    validador.validar(token)
    servidor_jwks_proprio.status = 503

    relogio.agora += FRESCO_SEGUNDOS + 1  # venceu o cache: tenta renovar e falha
    assert validador.validar(token)["papel"] == "admin"
    assert servidor_jwks_proprio.requisicoes == 2

    relogio.agora = 1000.0 + VELHO_MAXIMO_SEGUNDOS + 1  # copia velha demais
    with pytest.raises(JwksIndisponivelError):
        validador.validar(token)
