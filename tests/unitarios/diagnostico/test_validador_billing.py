from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import respx

import src.diagnostico.infraestrutura.validador_billing as modulo
from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelException,
    RespostaInvalidaDaDependenciaException,
)
from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker
from src.diagnostico.infraestrutura.validador_billing import (
    ValidadorDeItensBilling,
    espera_com_jitter,
)

BILLING = "http://billing.test"
# Literal, e nao a constante da producao: trocar o caminho tem de quebrar o teste.
CAMINHO = "/api/v1/precos/validacao"
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
    rota = billing.post(CAMINHO).respond(200, json={"invalidos": ["PEC-B"]})

    assert _chamar(validador) == ["PEC-B"]

    requisicao = rota.calls.last.request
    assert requisicao.headers["Authorization"] == TOKEN
    assert json.loads(requisicao.content) == {"servicos": ["SRV-A"], "pecas": ["PEC-B"]}


@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(httpx.ReadTimeout("lento"), id="timeout"),
        pytest.param(httpx.ConnectError("recusada"), id="rede"),
        pytest.param(httpx.RemoteProtocolError("cortada"), id="protocolo"),
        pytest.param(httpx.Response(502), id="502"),
        pytest.param(httpx.Response(503), id="503"),
        pytest.param(httpx.Response(504), id="504"),
    ],
)
def test_erro_transitorio_e_repetido(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    breaker: CircuitBreaker,
    esperas: list[float],
    falha: Exception | httpx.Response,
) -> None:
    rota = billing.post(CAMINHO)
    rota.side_effect = [falha, falha, httpx.Response(200, json={"invalidos": []})]

    assert _chamar(validador) == []

    assert rota.call_count == 3
    assert len(esperas) == 2
    assert not breaker.aberto


def test_espera_com_jitter_entre_zero_e_o_teto_exponencial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sorteios: list[tuple[float, float]] = []

    def sortear(minimo: float, maximo: float) -> float:
        sorteios.append((minimo, maximo))
        return maximo / 2

    monkeypatch.setattr(modulo._aleatorio, "uniform", sortear)

    assert [espera_com_jitter(0), espera_com_jitter(1)] == [0.05, 0.1]
    assert sorteios == [(0, 0.1), (0, 0.2)]


def test_tres_falhas_transitorias_esgotam_as_tentativas(
    validador: ValidadorDeItensBilling, billing: respx.MockRouter
) -> None:
    rota = billing.post(CAMINHO).mock(side_effect=httpx.ConnectError("recusada"))
    with pytest.raises(DependenciaIndisponivelException, match="3 tentativas"):
        _chamar(validador)
    assert rota.call_count == 3


@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(httpx.Response(500), id="500"),
        pytest.param(httpx.Response(501), id="501"),
        pytest.param(
            httpx.LocalProtocolError("pedido malformado"), id="protocolo-local"
        ),
        pytest.param(httpx.UnsupportedProtocol("sem esquema"), id="url-sem-esquema"),
    ],
)
def test_falha_que_repetir_nao_resolve_e_503_sem_retry(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    breaker: CircuitBreaker,
    falha: Exception | httpx.Response,
) -> None:
    rota = billing.post(CAMINHO).mock(side_effect=[falha])
    with pytest.raises(DependenciaIndisponivelException, match="nao respondeu"):
        _chamar(validador)
    assert rota.call_count == 1
    # Contou como falha do Billing: mais 4 abrem o circuito (limite 5).
    for _ in range(3):
        breaker.registrar_falha()
    assert not breaker.aberto
    breaker.registrar_falha()
    assert breaker.aberto


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_4xx_vira_502_sem_retry_e_conta_como_billing_de_pe(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    breaker: CircuitBreaker,
    status: int,
) -> None:
    for _ in range(4):
        breaker.registrar_falha()
    rota = billing.post(CAMINHO).respond(status)
    with pytest.raises(RespostaInvalidaDaDependenciaException, match=f"HTTP {status}"):
        _chamar(validador)
    assert rota.call_count == 1
    # O 4xx zerou a contagem: sem ele, esta seria a 5a falha seguida.
    breaker.registrar_falha()
    assert not breaker.aberto


@pytest.mark.parametrize(
    "resposta",
    [
        pytest.param(httpx.Response(200, text="nao e json"), id="nao-e-json"),
        pytest.param(httpx.Response(200, json={"outro": []}), id="sem-invalidos"),
        pytest.param(httpx.Response(200, json={"invalidos": "PEC-B"}), id="nao-lista"),
        pytest.param(httpx.Response(200, json={"invalidos": [1, 2]}), id="nao-texto"),
        pytest.param(httpx.Response(200, json=["PEC-B"]), id="lista-solta"),
        pytest.param(httpx.Response(204), id="204-sem-corpo"),
        pytest.param(httpx.Response(301), id="redirect"),
    ],
)
def test_resposta_fora_do_contrato_e_502(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    resposta: httpx.Response,
) -> None:
    billing.post(CAMINHO).mock(return_value=resposta)
    with pytest.raises(RespostaInvalidaDaDependenciaException):
        _chamar(validador)


def test_circuito_abre_e_corta_a_rede(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    breaker: CircuitBreaker,
    relogio: _Relogio,
) -> None:
    rota = billing.post(CAMINHO).respond(503)
    with pytest.raises(DependenciaIndisponivelException, match="3 tentativas"):
        _chamar(validador)
    # Quinta falha seguida abre o circuito no meio das tentativas da 2a chamada.
    with pytest.raises(DependenciaIndisponivelException, match="circuito aberto"):
        _chamar(validador)
    assert rota.call_count == 5
    assert breaker.aberto

    with pytest.raises(DependenciaIndisponivelException, match="em 30 s") as erro:
        _chamar(validador)
    assert erro.value.retry_after == 30
    assert rota.call_count == 5

    # Vencido o prazo, a prova passa e, com sucesso, fecha o circuito.
    relogio.agora += 30
    rota.respond(200, json={"invalidos": []})
    assert _chamar(validador) == []
    assert not breaker.aberto


def test_prova_que_falha_reabre_e_responde_na_hora(
    validador: ValidadorDeItensBilling,
    billing: respx.MockRouter,
    breaker: CircuitBreaker,
    relogio: _Relogio,
) -> None:
    for _ in range(5):
        breaker.registrar_falha()
    relogio.agora += 30
    rota = billing.post(CAMINHO).respond(503)

    # Uma chamada so (a prova); a falha reabre e as tentativas seguintes nem
    # chegam a rede.
    with pytest.raises(DependenciaIndisponivelException, match="circuito aberto") as e:
        _chamar(validador)
    assert rota.call_count == 1
    assert e.value.retry_after == 30
