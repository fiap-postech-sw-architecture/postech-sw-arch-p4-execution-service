from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from src.compartilhado.dominio.exceptions import DependenciaIndisponivelException
from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker
from src.diagnostico.infraestrutura.validador_billing import (
    CAMINHO_VALIDACAO,
    ValidadorDeItensBilling,
    espera_com_jitter,
)

BILLING = "http://billing.test"
URL = BILLING + CAMINHO_VALIDACAO
TOKEN = "Bearer token-do-mecanico"


class _Relogio:
    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora


@pytest.fixture
def relogio() -> _Relogio:
    return _Relogio()


@pytest.fixture
def breaker(relogio: _Relogio) -> CircuitBreaker:
    return CircuitBreaker("billing", relogio=relogio)


@pytest.fixture
def esperas() -> list[float]:
    return []


@pytest.fixture
def billing() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BILLING, assert_all_called=False) as router:
        yield router


@pytest.fixture
def validador(
    breaker: CircuitBreaker, esperas: list[float]
) -> Iterator[ValidadorDeItensBilling]:
    with httpx.Client(base_url=BILLING, timeout=2.0) as cliente:
        yield ValidadorDeItensBilling(
            cliente=cliente, breaker=breaker, authorization=TOKEN, dormir=esperas.append
        )


def _chamar(validador: ValidadorDeItensBilling) -> list[str]:
    return validador.codigos_invalidos(servicos=["SRV-A"], pecas=["PEC-B"])


def test_repassa_token_e_le_os_invalidos(
    validador: ValidadorDeItensBilling, billing: respx.MockRouter
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO).respond(200, json={"invalidos": ["PEC-B"]})

    assert _chamar(validador) == ["PEC-B"]

    requisicao = rota.calls.last.request
    assert requisicao.headers["Authorization"] == TOKEN
    assert json.loads(requisicao.content) == {"servicos": ["SRV-A"], "pecas": ["PEC-B"]}


def test_erro_transitorio_e_repetido_com_jitter(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    esperas: list[float],
    breaker: CircuitBreaker,
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO)
    rota.side_effect = [
        httpx.ReadTimeout("lento"),
        httpx.Response(503),
        httpx.Response(200, json={"invalidos": []}),
    ]

    assert _chamar(validador) == []

    assert rota.call_count == 3
    assert len(esperas) == 2
    assert 0 <= esperas[0] <= 0.1
    assert 0 <= esperas[1] <= 0.2
    assert not breaker.aberto


def test_tres_falhas_esgotam_as_tentativas(
    validador: ValidadorDeItensBilling, billing: respx.MockRouter
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO).mock(
        side_effect=httpx.ConnectError("recusada")
    )
    with pytest.raises(DependenciaIndisponivelException, match="3 tentativas"):
        _chamar(validador)
    assert rota.call_count == 3


def test_circuito_abre_e_corta_a_rede(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    breaker: CircuitBreaker,
    relogio: _Relogio,
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO).respond(500)
    with pytest.raises(DependenciaIndisponivelException, match="3 tentativas"):
        _chamar(validador)
    # Quinta falha seguida abre o circuito no meio das tentativas da 2a chamada.
    with pytest.raises(DependenciaIndisponivelException, match="circuito aberto"):
        _chamar(validador)
    assert rota.call_count == 5
    assert breaker.aberto

    with pytest.raises(DependenciaIndisponivelException, match="em 30 s"):
        _chamar(validador)
    assert rota.call_count == 5

    # Vencido o prazo, a prova passa e, com sucesso, fecha o circuito.
    relogio.agora += 30
    rota.respond(200, json={"invalidos": []})
    assert _chamar(validador) == []
    assert not breaker.aberto


@pytest.mark.parametrize("status", [400, 401, 422])
def test_4xx_nao_repete_e_conta_como_billing_de_pe(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    breaker: CircuitBreaker,
    status: int,
) -> None:
    for _ in range(4):
        breaker.registrar_falha()
    rota = billing.post(CAMINHO_VALIDACAO).respond(status)
    with pytest.raises(DependenciaIndisponivelException, match=f"HTTP {status}"):
        _chamar(validador)
    assert rota.call_count == 1
    # O 4xx zerou a contagem: sem ele, esta seria a 5a falha seguida.
    breaker.registrar_falha()
    assert not breaker.aberto


@pytest.mark.parametrize(
    "resposta",
    [
        httpx.Response(200, text="nao e json"),
        httpx.Response(200, json={"outro": []}),
        httpx.Response(200, json={"invalidos": "PEC-B"}),
        httpx.Response(200, json={"invalidos": [1, 2]}),
        httpx.Response(200, json=["PEC-B"]),
    ],
)
def test_resposta_fora_do_contrato(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    resposta: httpx.Response,
) -> None:
    billing.post(CAMINHO_VALIDACAO).mock(return_value=resposta)
    with pytest.raises(DependenciaIndisponivelException, match="fora do contrato"):
        _chamar(validador)


def test_espera_com_jitter_fica_no_intervalo() -> None:
    for retry, teto in [(0, 0.1), (1, 0.2)]:
        for _ in range(50):
            assert 0 <= espera_com_jitter(retry) <= teto
