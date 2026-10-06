from __future__ import annotations

import pytest
import structlog
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

import src.compartilhado.infraestrutura.circuit_breaker as modulo
from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker


class _Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


@pytest.fixture(autouse=True)
def _logger_fresco(monkeypatch: pytest.MonkeyPatch) -> None:
    # capture_logs nao intercepta logger ja cacheado (cache_logger_on_first_use).
    monkeypatch.setattr(modulo, "_log", structlog.get_logger("test_circuit_breaker"))


@pytest.fixture
def relogio() -> _Relogio:
    return _Relogio()


@pytest.fixture
def breaker(relogio: _Relogio) -> CircuitBreaker:
    return CircuitBreaker("billing", relogio=relogio)


def _falhar(breaker: CircuitBreaker, vezes: int) -> None:
    for _ in range(vezes):
        assert breaker.permitir()
        breaker.registrar_falha()


def test_fechado_deixa_passar(breaker: CircuitBreaker) -> None:
    assert breaker.permitir()
    assert not breaker.aberto
    assert breaker.segundos_para_nova_tentativa() == 0


def test_quatro_falhas_seguidas_ainda_nao_abrem(breaker: CircuitBreaker) -> None:
    _falhar(breaker, 4)
    assert not breaker.aberto
    assert breaker.permitir()


def test_quinta_falha_abre_por_30_segundos(
    breaker: CircuitBreaker, relogio: _Relogio
) -> None:
    with capture_logs() as logs:
        _falhar(breaker, 5)
    assert breaker.aberto
    assert not breaker.permitir()
    assert breaker.segundos_para_nova_tentativa() == 30
    relogio.agora += 29.5
    assert not breaker.permitir()
    assert breaker.segundos_para_nova_tentativa() == 1
    assert logs == [
        {
            "event": "circuit_breaker_opened",
            "dependencia": "billing",
            "falhas": 5,
            "log_level": "warning",
        }
    ]


def test_sucesso_zera_a_contagem_de_falhas(breaker: CircuitBreaker) -> None:
    _falhar(breaker, 4)
    breaker.registrar_sucesso()
    _falhar(breaker, 4)
    assert not breaker.aberto


def test_meio_aberto_libera_uma_unica_prova(
    breaker: CircuitBreaker, relogio: _Relogio
) -> None:
    _falhar(breaker, 5)
    relogio.agora += 30
    assert breaker.permitir()
    assert not breaker.permitir()


def test_prova_com_sucesso_fecha(breaker: CircuitBreaker, relogio: _Relogio) -> None:
    _falhar(breaker, 5)
    relogio.agora += 30
    assert breaker.permitir()
    with capture_logs() as logs:
        breaker.registrar_sucesso()
    assert not breaker.aberto
    assert breaker.permitir()
    assert logs[0]["event"] == "circuit_breaker_closed"


def test_prova_com_falha_reabre_por_mais_30_segundos(
    breaker: CircuitBreaker, relogio: _Relogio
) -> None:
    _falhar(breaker, 5)
    relogio.agora += 30
    assert breaker.permitir()
    breaker.registrar_falha()
    assert not breaker.permitir()
    relogio.agora += 29
    assert not breaker.permitir()
    relogio.agora += 1
    assert breaker.permitir()


def test_gauge_acompanha_abertura_e_fechamento(relogio: _Relogio) -> None:
    breaker = CircuitBreaker("billing-gauge", relogio=relogio)

    def aberto() -> float | None:
        return REGISTRY.get_sample_value(
            "pytstop_circuit_breaker_aberto", {"dependencia": "billing-gauge"}
        )

    assert aberto() == 0
    _falhar(breaker, 5)
    assert aberto() == 1
    relogio.agora += 30
    assert breaker.permitir()
    breaker.registrar_sucesso()
    assert aberto() == 0


def test_prova_sem_resultado_nao_trava_o_circuito(
    breaker: CircuitBreaker, relogio: _Relogio
) -> None:
    _falhar(breaker, 5)
    relogio.agora += 30
    assert breaker.permitir()  # prova liberada e o resultado se perde
    relogio.agora += 30
    assert breaker.permitir()  # nova prova no prazo seguinte
