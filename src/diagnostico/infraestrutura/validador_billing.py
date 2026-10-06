"""Port ``ValidadorDeItens`` sobre o Billing: ``POST /api/v1/precos/validacao``.

Envelope de resiliencia (RFC-004, secao 6.1; ADR-038): timeout de 2 s (no
``httpx.Client`` criado no lifespan), 2 retries com jitter so em erro
transitorio (timeout, rede, protocolo, 502/503/504) e circuit breaker
compartilhado entre requests (5 falhas abrem por 30 s). Retry e seguro porque a
validacao nao tem efeito colateral. Circuito aberto, 5xx e timeout depois dos
retries: 503. Um 4xx ou resposta fora do contrato: 502, sem retry (repetir nao
muda o resultado).
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Final

import httpx
import structlog

from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelException,
    RespostaInvalidaDaDependenciaException,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker

_log = structlog.get_logger(__name__)

CAMINHO_VALIDACAO: Final = "/api/v1/precos/validacao"
_TENTATIVAS: Final = 3  # 1 chamada + 2 retries
_ESPERA_BASE_S: Final = 0.1
_HTTP_OK: Final = 200
_HTTP_ERRO_SERVIDOR: Final = 500
_STATUS_TRANSITORIOS: Final = frozenset({502, 503, 504})
_ERROS_TRANSITORIOS: Final = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
_SEM_RESPOSTA: Final = (
    "O Billing nao respondeu a validacao de precos. Tente concluir o "
    "diagnostico novamente em instantes."
)
_aleatorio = secrets.SystemRandom()


def espera_com_jitter(retry: int) -> float:
    """Backoff exponencial com full jitter: ate 0,1 s no 1o retry, 0,2 s no 2o."""
    return _aleatorio.uniform(0, _ESPERA_BASE_S * 2**retry)


class ValidadorDeItensBilling:
    def __init__(
        self,
        *,
        cliente: httpx.Client,
        breaker: CircuitBreaker,
        authorization: str,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cliente = cliente
        self._breaker = breaker
        self._authorization = authorization
        self._dormir = dormir

    def codigos_invalidos(
        self, *, servicos: Sequence[str], pecas: Sequence[str]
    ) -> list[str]:
        corpo = {"servicos": list(servicos), "pecas": list(pecas)}
        for tentativa in range(_TENTATIVAS):
            if tentativa:
                self._dormir(espera_com_jitter(tentativa - 1))
            resposta = self._tentar(corpo)
            if resposta is not None:
                return _ler_invalidos(resposta)
        msg = (
            f"O Billing nao respondeu a validacao de precos apos {_TENTATIVAS} "
            "tentativas. Tente concluir o diagnostico novamente em instantes."
        )
        raise DependenciaIndisponivelException(msg)

    def _tentar(self, corpo: dict[str, list[str]]) -> httpx.Response | None:
        """Uma chamada; ``None`` = falha transitoria (conta no breaker e retenta).

        Raises:
            DependenciaIndisponivelException: circuito aberto ou falha que
                repetir nao resolve (500, erro de protocolo local).
        """
        if not self._breaker.permitir():
            segundos = max(1, self._breaker.segundos_para_nova_tentativa())
            msg = (
                "Validacao de precos suspensa: o Billing falhou repetidamente "
                f"(circuito aberto). Tente novamente em {segundos} s."
            )
            raise DependenciaIndisponivelException(msg, retry_after=segundos)
        try:
            resposta = self._cliente.post(
                CAMINHO_VALIDACAO,
                json=corpo,
                headers={"Authorization": self._authorization},
            )
        except _ERROS_TRANSITORIOS as exc:
            self._breaker.registrar_falha()
            _log.warning("billing_call_failed", erro=type(exc).__name__)
            return None
        except httpx.HTTPError as exc:
            self._breaker.registrar_falha()
            _log.warning("billing_call_failed", erro=type(exc).__name__)
            raise DependenciaIndisponivelException(_SEM_RESPOSTA) from exc
        if resposta.status_code >= _HTTP_ERRO_SERVIDOR:
            self._breaker.registrar_falha()
            _log.warning("billing_call_failed", status=resposta.status_code)
            if resposta.status_code in _STATUS_TRANSITORIOS:
                return None
            raise DependenciaIndisponivelException(_SEM_RESPOSTA)
        # 2xx a 4xx: o Billing esta de pe (um 4xx e problema do pedido).
        self._breaker.registrar_sucesso()
        return resposta


def _ler_invalidos(resposta: httpx.Response) -> list[str]:
    """``invalidos`` de um 200 no contrato; qualquer outra coisa vira 502."""
    if resposta.status_code != _HTTP_OK:
        msg = (
            f"O Billing recusou a validacao de precos (HTTP {resposta.status_code}). "
            "Confira o token e o contrato de /api/v1/precos/validacao."
        )
        raise RespostaInvalidaDaDependenciaException(msg)
    try:
        invalidos = resposta.json()["invalidos"]
    except (ValueError, KeyError, TypeError):
        invalidos = None
    if not isinstance(invalidos, list) or not all(
        isinstance(codigo, str) for codigo in invalidos
    ):
        msg = "Resposta do Billing fora do contrato: esperado {'invalidos': [codigos]}"
        raise RespostaInvalidaDaDependenciaException(msg)
    return invalidos
