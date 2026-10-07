"""O que o relay e o consumidor tem de processo: sinais de vida, parada e metricas."""

from __future__ import annotations

import signal
import tempfile
import threading
from pathlib import Path

from prometheus_client import start_http_server

from src.compartilhado.infraestrutura.ambiente import inteiro_opcional


class SinaisDoProcesso:
    """Heartbeat em arquivo (liveness, padrao do relay do p3) e arquivo de prontidao.

    O heartbeat e tocado a cada volta do laco, inclusive enquanto reconecta:
    broker ou banco fora nao deve reiniciar o pod. O arquivo de prontidao existe
    enquanto as conexoes estao de pe (readiness).
    """

    def __init__(self, heartbeat: Path, pronto: Path) -> None:
        self._heartbeat = heartbeat
        self._pronto = pronto

    @classmethod
    def do_processo(cls, nome: str) -> SinaisDoProcesso:
        """``<tmp>/<nome>-heartbeat`` e ``<tmp>/<nome>-pronto`` (``/tmp`` na imagem)."""
        base = Path(tempfile.gettempdir())
        return cls(base / f"{nome}-heartbeat", base / f"{nome}-pronto")

    def heartbeat(self) -> None:
        self._heartbeat.touch()

    def pronto(self) -> None:
        self._pronto.touch()

    def indisponivel(self) -> None:
        self._pronto.unlink(missing_ok=True)


class Backoff:
    """Espera que dobra a cada falha seguida, ate o teto; volta ao inicio no sucesso."""

    def __init__(self, inicial_s: float = 1.0, teto_s: float = 30.0) -> None:
        self._inicial = inicial_s
        self._teto = teto_s
        self._atual = inicial_s

    def esperar(self, parar: threading.Event) -> None:
        """Dorme o atraso da vez; ``parar`` interrompe a espera."""
        parar.wait(self._atual)
        self._atual = min(self._atual * 2, self._teto)

    def reiniciar(self) -> None:
        self._atual = self._inicial


def parada_por_sinal() -> threading.Event:
    """Evento ligado por SIGTERM ou SIGINT: o laco conclui o que esta em curso e sai.

    Como PID 1 do container, sem handler o processo ignoraria o SIGTERM e o
    rollout esperaria o grace period inteiro ate o SIGKILL (licao do p3).
    """
    parar = threading.Event()
    for sinal in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sinal, lambda *_: parar.set())
    return parar


def servir_metricas() -> None:
    """``/metrics`` em ``METRICS_PORT`` (padrao 9100), a porta ``metrics`` do pod."""
    start_http_server(inteiro_opcional("METRICS_PORT", 9100))
