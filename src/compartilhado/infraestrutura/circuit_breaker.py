from __future__ import annotations

import math
import threading
import time
from typing import TYPE_CHECKING

import structlog
from prometheus_client import Gauge

if TYPE_CHECKING:
    from collections.abc import Callable

_log = structlog.get_logger(__name__)

# Alimenta o alerta "circuito aberto" (RFC-004, secao 9).
_CIRCUITO_ABERTO = Gauge(
    "pytstop_circuit_breaker_aberto",
    "1 com o circuito aberto (dependencia suspensa), 0 fechado.",
    ["dependencia"],
)


class CircuitBreaker:
    """Circuit breaker de uma dependencia, compartilhado entre requests.

    Fechado: tudo passa; ``limite_falhas`` falhas seguidas abrem o circuito por
    ``segundos_aberto``. Vencido o prazo, UMA chamada de prova passa (half-open)
    e as demais seguem barradas: sucesso fecha, falha reabre por mais
    ``segundos_aberto``. A prova empurra o prazo para frente ao ser liberada, entao
    uma prova cujo resultado se perdeu nao trava o circuito: outra prova sai no
    proximo prazo.
    """

    def __init__(
        self,
        dependencia: str,
        *,
        limite_falhas: int = 5,
        segundos_aberto: float = 30.0,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dependencia = dependencia
        self._limite_falhas = limite_falhas
        self._segundos_aberto = segundos_aberto
        self._relogio = relogio
        self._lock = threading.Lock()
        self._falhas = 0
        self._aberto_ate: float | None = None
        self._gauge = _CIRCUITO_ABERTO.labels(dependencia)
        self._gauge.set(0)

    @property
    def aberto(self) -> bool:
        return self._aberto_ate is not None

    def barrado(self) -> bool:
        """Aberto e dentro do prazo: a chamada seria barrada (sem efeito colateral).

        Diferente de ``permitir``, nao libera a prova de meia-abertura; serve para
        decidir sem custo se vale tentar (ex.: antes de tomar um lock).
        """
        with self._lock:
            return self._aberto_ate is not None and self._relogio() < self._aberto_ate

    def segundos_para_nova_tentativa(self) -> int:
        """Segundos (arredondados para cima) ate a proxima prova; 0 se fechado."""
        with self._lock:
            if self._aberto_ate is None:
                return 0
            return max(0, math.ceil(self._aberto_ate - self._relogio()))

    def permitir(self) -> bool:
        with self._lock:
            if self._aberto_ate is None:
                return True
            agora = self._relogio()
            if agora < self._aberto_ate:
                return False
            self._aberto_ate = agora + self._segundos_aberto
            return True

    def registrar_sucesso(self) -> None:
        with self._lock:
            if self._aberto_ate is not None:
                _log.info("circuit_breaker_closed", dependencia=self._dependencia)
                self._gauge.set(0)
            self._falhas = 0
            self._aberto_ate = None

    def registrar_falha(self) -> None:
        with self._lock:
            self._falhas += 1
            # Com o circuito aberto, so a prova chega aqui: falha nela reabre.
            if self._aberto_ate is not None or self._falhas >= self._limite_falhas:
                if self._aberto_ate is None:
                    _log.warning(
                        "circuit_breaker_opened",
                        dependencia=self._dependencia,
                        falhas=self._falhas,
                    )
                    self._gauge.set(1)
                self._aberto_ate = self._relogio() + self._segundos_aberto
