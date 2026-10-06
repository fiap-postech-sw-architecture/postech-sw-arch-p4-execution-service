"""Validacao dos JWT RS256 emitidos pelo OS Service (brief secao 7).

A chave publica vem do JWKS do emissor (``JWKS_URL``), com cache de 10 min e
timeout de 2 s. Sem segredo compartilhado entre servicos: so o OS Service tem
a chave privada. Revogacao (logout) vale so no OS; aqui o limite e o ``exp``
curto do token (tradeoff do brief).
"""

from __future__ import annotations

from typing import Any, Final

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
    PyJWKSetError,
)

EMISSOR: Final = "pytstop-os-service"
AUDIENCIA: Final = "pytstop"
_CACHE_JWKS_SEGUNDOS: Final = 600
_TIMEOUT_JWKS_SEGUNDOS: Final = 2


class TokenInvalidoError(Exception):
    """Assinatura, ``iss``, ``aud`` ou formato invalidos; ``kid`` desconhecido."""


class TokenExpiradoError(TokenInvalidoError):
    """``exp`` vencido."""


class JwksIndisponivelError(Exception):
    """O JWKS do OS Service nao respondeu (ou respondeu sem chave utilizavel)."""


class ValidadorDeTokenJWKS:
    def __init__(self, jwks_url: str) -> None:
        self._jwks = PyJWKClient(
            jwks_url, lifespan=_CACHE_JWKS_SEGUNDOS, timeout=_TIMEOUT_JWKS_SEGUNDOS
        )

    def validar(self, token: str) -> dict[str, Any]:
        """Devolve as claims de um token valido.

        Raises:
            TokenExpiradoError: ``exp`` vencido.
            TokenInvalidoError: assinatura, ``iss``, ``aud``, claims obrigatorias
                ou ``kid`` invalidos.
            JwksIndisponivelError: falha ao buscar o JWKS do emissor.
        """
        try:
            chave = self._jwks.get_signing_key_from_jwt(token)
        except (PyJWKClientConnectionError, PyJWKSetError) as exc:
            raise JwksIndisponivelError(str(exc)) from exc
        except (PyJWKClientError, InvalidTokenError) as exc:
            raise TokenInvalidoError(str(exc)) from exc
        except ValueError as exc:
            # Corpo do JWKS que nao e JSON: o PyJWKClient deixa o JSONDecodeError
            # passar. Problema do emissor, nao do token (DecodeError do token e
            # InvalidTokenError, tratado acima).
            raise JwksIndisponivelError(str(exc)) from exc
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                chave,
                algorithms=["RS256"],
                issuer=EMISSOR,
                audience=AUDIENCIA,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except ExpiredSignatureError as exc:
            raise TokenExpiradoError(str(exc)) from exc
        except InvalidTokenError as exc:
            raise TokenInvalidoError(str(exc)) from exc
        return claims
