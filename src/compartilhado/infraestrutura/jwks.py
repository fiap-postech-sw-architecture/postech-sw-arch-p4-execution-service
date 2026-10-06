"""Validacao dos JWT RS256 emitidos pelo OS Service (RFC-004, secao 8; ADR-039).

A chave publica vem do JWKS do emissor (``JWKS_URL``, timeout de 2 s). O JWK
Set fica fresco por 10 min; quando a renovacao falha, o ultimo JWK Set bom
continua valendo por ate 1 h (stale-if-error) e a falha fica memorizada por
5 s, para os requests seguintes nao esperarem cada um 2 s pelo mesmo emissor
fora do ar. Sem JWK Set utilizavel, ``JwksIndisponivelError`` (a API responde
503 com ``Retry-After``), nunca 401. ``leeway`` de 10 s cobre relogios
dessincronizados entre os servicos. Sem segredo compartilhado: so o OS Service
tem a chave privada. Revogacao (logout) vale so no OS; aqui o limite e o
``exp`` curto do token.
"""

from __future__ import annotations

import math
import threading
import time
from typing import TYPE_CHECKING, Any, Final

import jwt
import structlog
from jwt import PyJWKClient
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError, PyJWTError
from prometheus_client import Counter

if TYPE_CHECKING:
    from collections.abc import Callable

    from jwt import PyJWK

EMISSOR: Final = "pytstop-os-service"
AUDIENCIA: Final = "pytstop"
LEEWAY_SEGUNDOS: Final = 10
FRESCO_SEGUNDOS: Final = 600
VELHO_MAXIMO_SEGUNDOS: Final = 3600
MEMORIA_DA_FALHA_SEGUNDOS: Final = 5
# Token com kid desconhecido forca no maximo uma busca a cada 30 s (rotacao de
# chave sem abrir caminho para um flood de buscas com kid aleatorio).
_INTERVALO_BUSCA_POR_KID_SEGUNDOS: Final = 30
_TIMEOUT_JWKS_SEGUNDOS: Final = 2

_log = structlog.get_logger(__name__)
_FALHAS_JWKS = Counter(
    "pytstop_jwks_falhas", "Buscas ao JWKS do OS Service que falharam."
)


class TokenInvalidoError(Exception):
    """Formato, assinatura, ``iss``, ``aud``, claims ou ``kid`` invalidos."""


class TokenExpiradoError(TokenInvalidoError):
    """``exp`` vencido, alem do ``leeway``."""


class JwksIndisponivelError(Exception):
    """Sem JWK Set utilizavel: o JWKS nao respondeu (ou veio sem chave de assinatura)
    e nao ha copia de ate 1 h. ``retry_after`` sao os segundos ate a proxima busca.
    """

    def __init__(self, mensagem: str, *, retry_after: int) -> None:
        super().__init__(mensagem)
        self.retry_after = retry_after


class ValidadorDeTokenJWKS:
    """Valida tokens do OS Service; uma instancia por processo (lifespan)."""

    def __init__(
        self, jwks_url: str, *, relogio: Callable[[], float] = time.monotonic
    ) -> None:
        # Sem o cache do PyJWKClient: frescor, copia velha e memoria da falha
        # ficam aqui, sob um lock so (uma busca ao emissor por vez).
        self._cliente = PyJWKClient(
            jwks_url, cache_jwk_set=False, timeout=_TIMEOUT_JWKS_SEGUNDOS
        )
        self._relogio = relogio
        self._lock = threading.Lock()
        self._chaves: list[PyJWK] = []
        self._obtidas_em: float | None = None
        self._falhou_em: float | None = None

    def validar(self, token: str) -> dict[str, Any]:
        """Devolve as claims de um token valido.

        Raises:
            TokenExpiradoError: ``exp`` vencido.
            TokenInvalidoError: formato, assinatura, ``iss``, ``aud``, claims
                obrigatorias ou ``kid`` invalidos.
            JwksIndisponivelError: sem JWK Set utilizavel.
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except InvalidTokenError as exc:
            raise TokenInvalidoError(str(exc)) from exc
        chave = self._chave(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                chave,
                algorithms=["RS256"],
                issuer=EMISSOR,
                audience=AUDIENCIA,
                leeway=LEEWAY_SEGUNDOS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except ExpiredSignatureError as exc:
            raise TokenExpiradoError(str(exc)) from exc
        except InvalidTokenError as exc:
            raise TokenInvalidoError(str(exc)) from exc
        return claims

    def _chave(self, kid: object) -> PyJWK:
        chave = _por_kid(self._chaves_de_assinatura(renovar=False), kid)
        if chave is None and self._pode_buscar_por_kid():
            chave = _por_kid(self._chaves_de_assinatura(renovar=True), kid)
        if chave is None:
            msg = "kid desconhecido no JWKS do emissor"
            raise TokenInvalidoError(msg)
        return chave

    def _pode_buscar_por_kid(self) -> bool:
        with self._lock:
            idade = self._idade(self._relogio())
        return idade >= _INTERVALO_BUSCA_POR_KID_SEGUNDOS

    def _chaves_de_assinatura(self, *, renovar: bool) -> list[PyJWK]:
        with self._lock:
            agora = self._relogio()
            if not renovar and self._idade(agora) < FRESCO_SEGUNDOS:
                return self._chaves
            if self._falhou_em is not None and (
                agora - self._falhou_em < MEMORIA_DA_FALHA_SEGUNDOS
            ):
                return self._copia_velha(agora)
            try:
                chaves = self._cliente.get_signing_keys()
            except (PyJWTError, ValueError) as exc:
                # ValueError: corpo que nao e JSON (o PyJWKClient deixa passar o
                # JSONDecodeError). Sem kid ou so com chave de cifra: PyJWTError.
                self._falhou_em = self._relogio()
                _FALHAS_JWKS.inc()
                _log.warning("jwks_refresh_failed", erro=type(exc).__name__)
                return self._copia_velha(self._falhou_em)
            self._chaves, self._obtidas_em, self._falhou_em = chaves, agora, None
            return chaves

    def _idade(self, agora: float) -> float:
        return math.inf if self._obtidas_em is None else agora - self._obtidas_em

    def _copia_velha(self, agora: float) -> list[PyJWK]:
        if self._idade(agora) < VELHO_MAXIMO_SEGUNDOS:
            return self._chaves
        falhou_em = agora if self._falhou_em is None else self._falhou_em
        restante = MEMORIA_DA_FALHA_SEGUNDOS - (agora - falhou_em)
        msg = "JWKS do emissor indisponivel e sem copia valida em cache"
        raise JwksIndisponivelError(msg, retry_after=max(1, math.ceil(restante)))


def _por_kid(chaves: list[PyJWK], kid: object) -> PyJWK | None:
    return next((chave for chave in chaves if chave.key_id == kid), None)
