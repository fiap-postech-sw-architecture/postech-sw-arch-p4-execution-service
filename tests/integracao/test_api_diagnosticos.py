from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
import pytest

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.diagnostico.aplicacao.use_cases import (
    DescartarDiagnostico,
    RegistrarSolicitacaoDeDiagnostico,
)
from src.diagnostico.dominio.veiculo import Veiculo
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.diagnostico.infraestrutura.validador_billing import CAMINHO_VALIDACAO
from src.estoque.infraestrutura.seed import semear

if TYPE_CHECKING:
    from collections.abc import Callable

    import respx
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session, sessionmaker

URL = "/api/v1/diagnosticos"
MECANICO = uuid4()
ITENS = [
    {"tipo": "servico", "codigo": "SRV-TROCA-PASTILHA", "quantidade": 1},
    {"tipo": "peca", "codigo": "PEC-PASTILHA-FREIO", "quantidade": 1},
]


def _solicitar(
    session_factory: sessionmaker[Session], descricao: str = "Freio chiando"
) -> UUID:
    ordem_id = uuid4()
    with session_factory() as session:
        RegistrarSolicitacaoDeDiagnostico(
            DiagnosticoSQLAlchemyRepository(session),
            SQLAlchemyUnitOfWork(lambda: session),
        ).executar(
            ordem_id,
            Veiculo(placa="BRA2E19", marca="Chevrolet", modelo="Onix", ano=2022),
            descricao,
        )
    return ordem_id


@pytest.fixture
def mecanico(autenticar: Callable[..., dict[str, str]]) -> dict[str, str]:
    return autenticar("mecanico", MECANICO)


@pytest.fixture
def em_andamento(
    api: TestClient, mecanico: dict[str, str], session_factory: sessionmaker[Session]
) -> UUID:
    semear(session_factory)
    ordem_id = _solicitar(session_factory)
    assert api.post(f"{URL}/{ordem_id}/inicio", headers=mecanico).status_code == 200
    return ordem_id


def test_fila_de_diagnosticos_com_filtro(
    api: TestClient, mecanico: dict[str, str], session_factory: sessionmaker[Session]
) -> None:
    primeira, segunda = _solicitar(session_factory), _solicitar(session_factory)
    api.post(f"{URL}/{segunda}/inicio", headers=mecanico)

    aguardando = api.get(URL, params={"status": "AGUARDANDO"}, headers=mecanico).json()
    assert [d["ordem_id"] for d in aguardando["items"]] == [str(primeira)]
    assert aguardando["items"][0]["veiculo"] == {
        "placa": "BRA2E19",
        "marca": "Chevrolet",
        "modelo": "Onix",
        "ano": 2022,
    }
    todos = api.get(URL, headers=mecanico).json()
    assert (todos["total"], [d["status"] for d in todos["items"]]) == (
        2,
        ["AGUARDANDO", "EM_ANDAMENTO"],
    )
    assert api.get(URL, params={"status": "OUTRO"}, headers=mecanico).status_code == 422


def test_lapide_aparece_descartada_sem_retrato(
    api: TestClient, mecanico: dict[str, str], session_factory: sessionmaker[Session]
) -> None:
    ordem_id = uuid4()
    with session_factory() as session:
        DescartarDiagnostico(
            DiagnosticoSQLAlchemyRepository(session),
            SQLAlchemyUnitOfWork(lambda: session),
        ).executar(ordem_id)

    resposta = api.get(URL, params={"status": "DESCARTADO"}, headers=mecanico)

    assert resposta.status_code == 200
    [lapide] = resposta.json()["items"]
    assert (lapide["ordem_id"], lapide["status"]) == (str(ordem_id), "DESCARTADO")
    assert (lapide["veiculo"], lapide["descricao_problema"]) == (None, None)


def test_inicio_emite_evento_e_e_idempotente(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    ordem_id = _solicitar(session_factory)
    resposta = api.post(f"{URL}/{ordem_id}/inicio", headers=mecanico)
    repetida = api.post(f"{URL}/{ordem_id}/inicio", headers=mecanico)

    assert resposta.status_code == repetida.status_code == 200
    corpo = resposta.json()
    assert (corpo["status"], corpo["mecanico_id"]) == ("EM_ANDAMENTO", str(MECANICO))
    assert repetida.json()["iniciado_em"] == corpo["iniciado_em"]
    linhas = outbox()
    assert [(linha["tipo"], linha["correlation_id"]) for linha in linhas] == [
        ("DiagnosticoIniciado", ordem_id)
    ]
    assert linhas[0]["dados"]["mecanico_id"] == str(MECANICO)


def test_outro_mecanico_nao_assume_diagnostico_em_andamento(
    api: TestClient, em_andamento: UUID, autenticar: Callable[..., dict[str, str]]
) -> None:
    resposta = api.post(f"{URL}/{em_andamento}/inicio", headers=autenticar("mecanico"))
    assert resposta.status_code == 409
    assert (
        resposta.json()["erro"]["mensagem"]
        == "Diagnostico ja iniciado por outro mecanico"
    )


def test_conclusao_valida_no_billing_com_o_token_do_mecanico(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO).respond(200, json={"invalidos": []})

    resposta = api.post(
        f"{URL}/{em_andamento}/conclusao",
        json={"itens": ITENS, "observacoes": "pastilhas no limite"},
        headers=mecanico,
    )

    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["status"] == "CONCLUIDO"
    assert resposta.json()["itens"] == ITENS
    chamada = rota.calls.last.request
    assert chamada.headers["Authorization"] == mecanico["Authorization"]
    assert json.loads(chamada.content) == {
        "servicos": ["SRV-TROCA-PASTILHA"],
        "pecas": ["PEC-PASTILHA-FREIO"],
    }
    concluido = outbox()[-1]
    assert concluido["tipo"] == "DiagnosticoConcluido"
    assert concluido["dados"]["itens"] == ITENS
    assert concluido["dados"]["observacoes"] == "pastilhas no limite"


def test_nenhuma_conexao_do_banco_fica_presa_durante_a_chamada_ao_billing(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
) -> None:
    pool = api.app.state.session_factory.kw["bind"].pool
    conexoes_em_uso: list[int] = []

    def responder(_requisicao: httpx.Request) -> httpx.Response:
        conexoes_em_uso.append(pool.checkedout())
        return httpx.Response(200, json={"invalidos": []})

    billing.post(CAMINHO_VALIDACAO).mock(side_effect=responder)
    resposta = api.post(
        f"{URL}/{em_andamento}/conclusao", json={"itens": ITENS}, headers=mecanico
    )
    assert resposta.status_code == 200
    assert conexoes_em_uso == [0]


def test_codigo_sem_preco_no_billing_e_422(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
) -> None:
    billing.post(CAMINHO_VALIDACAO).respond(
        200, json={"invalidos": ["SRV-TROCA-PASTILHA"]}
    )
    resposta = api.post(
        f"{URL}/{em_andamento}/conclusao", json={"itens": ITENS}, headers=mecanico
    )
    assert resposta.status_code == 422
    assert resposta.json()["erro"]["codigo"] == "ITENS_INVALIDOS"
    assert "SRV-TROCA-PASTILHA" in resposta.json()["erro"]["mensagem"]


def test_peca_sem_cadastro_no_estoque_e_422_sem_chamar_o_billing(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO).respond(200, json={"invalidos": []})
    itens = [{"tipo": "peca", "codigo": "PEC-INEXISTENTE", "quantidade": 1}]
    resposta = api.post(
        f"{URL}/{em_andamento}/conclusao", json={"itens": itens}, headers=mecanico
    )
    assert resposta.status_code == 422
    assert "PEC-INEXISTENTE" in resposta.json()["erro"]["mensagem"]
    assert rota.call_count == 0


def test_billing_fora_do_ar_e_503_e_depois_circuito_aberto(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO).mock(
        side_effect=httpx.ConnectError("recusada")
    )
    corpo = {"itens": ITENS}

    primeira = api.post(f"{URL}/{em_andamento}/conclusao", json=corpo, headers=mecanico)
    segunda = api.post(f"{URL}/{em_andamento}/conclusao", json=corpo, headers=mecanico)
    terceira = api.post(f"{URL}/{em_andamento}/conclusao", json=corpo, headers=mecanico)

    assert primeira.status_code == segunda.status_code == terceira.status_code == 503
    assert primeira.json()["erro"]["codigo"] == "DEPENDENCIA_INDISPONIVEL"
    assert "circuito aberto" in segunda.json()["erro"]["mensagem"]
    assert "Tente novamente em" in terceira.json()["erro"]["mensagem"]
    assert "Retry-After" not in primeira.headers  # sem circuito aberto: sem prazo
    assert 1 <= int(terceira.headers["Retry-After"]) <= 30
    assert rota.call_count == 5  # 3 + 2 ate abrir; a terceira nem tocou a rede
    assert [linha["tipo"] for linha in outbox()] == ["DiagnosticoIniciado"]


def test_so_quem_iniciou_conclui(
    api: TestClient,
    em_andamento: UUID,
    autenticar: Callable[..., dict[str, str]],
    billing: respx.MockRouter,
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO).respond(200, json={"invalidos": []})
    resposta = api.post(
        f"{URL}/{em_andamento}/conclusao",
        json={"itens": ITENS},
        headers=autenticar("mecanico"),
    )
    assert resposta.status_code == 403
    assert resposta.json()["erro"]["codigo"] == "OPERACAO_NAO_PERMITIDA"
    assert rota.call_count == 0


def test_admin_conclui_em_nome_do_mecanico_responsavel(
    api: TestClient,
    em_andamento: UUID,
    autenticar: Callable[..., dict[str, str]],
    billing: respx.MockRouter,
) -> None:
    billing.post(CAMINHO_VALIDACAO).respond(200, json={"invalidos": []})
    resposta = api.post(
        f"{URL}/{em_andamento}/conclusao",
        json={"itens": ITENS},
        headers=autenticar("admin"),
    )
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "CONCLUIDO"
    assert resposta.json()["mecanico_id"] == str(MECANICO)


def test_conclusao_repetida_devolve_o_mesmo_resultado(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    rota = billing.post(CAMINHO_VALIDACAO).respond(200, json={"invalidos": []})
    url = f"{URL}/{em_andamento}/conclusao"
    primeira = api.post(url, json={"itens": ITENS}, headers=mecanico)
    segunda = api.post(url, json={"itens": ITENS[:1]}, headers=mecanico)
    assert primeira.json() == segunda.json()
    assert rota.call_count == 1
    assert [linha["tipo"] for linha in outbox()].count("DiagnosticoConcluido") == 1


@pytest.mark.parametrize(
    "corpo",
    [
        {"itens": []},
        {"itens": [{"tipo": "outro", "codigo": "X-1", "quantidade": 1}]},
        {"itens": [{"tipo": "peca", "codigo": "pec-1", "quantidade": 1}]},
        {"itens": [{"tipo": "peca", "codigo": "PEC-1", "quantidade": 0}]},
        {"itens": [ITENS[0], ITENS[0]]},
    ],
)
def test_corpo_invalido(
    api: TestClient, mecanico: dict[str, str], em_andamento: UUID, corpo: dict[str, Any]
) -> None:
    resposta = api.post(f"{URL}/{em_andamento}/conclusao", json=corpo, headers=mecanico)
    assert resposta.status_code == 422


def test_ordem_sem_diagnostico_e_404(api: TestClient, mecanico: dict[str, str]) -> None:
    resposta = api.post(f"{URL}/{uuid4()}/inicio", headers=mecanico)
    assert resposta.status_code == 404
    assert resposta.json()["erro"]["codigo"] == "ENTIDADE_NAO_ENCONTRADA"


def test_atendente_nao_mexe_em_diagnostico(
    api: TestClient,
    autenticar: Callable[..., dict[str, str]],
    session_factory: sessionmaker[Session],
) -> None:
    ordem_id = _solicitar(session_factory)
    atendente = autenticar("atendente")
    assert api.get(URL, headers=atendente).status_code == 403
    assert api.post(f"{URL}/{ordem_id}/inicio", headers=atendente).status_code == 403
