from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.compartilhado.dominio.veiculo import Veiculo
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.diagnostico.aplicacao.use_cases import (
    DescartarDiagnostico,
    RegistrarSolicitacaoDeDiagnostico,
)
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.diagnostico.infraestrutura.validador_billing import CAMINHO_VALIDACAO
from src.estoque.infraestrutura.seed import semear

if TYPE_CHECKING:
    from collections.abc import Callable

    import respx
    from sqlalchemy import Engine
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
            Veiculo(
                veiculo_id=uuid4(),
                placa="BRA2E19",
                marca="Chevrolet",
                modelo="Onix",
                ano=2022,
            ),
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


def test_diagnostico_anonimizado_nao_derruba_a_lista(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    engine: Engine,
) -> None:
    ordem_id = _solicitar(session_factory)
    with engine.begin() as conexao:
        veiculo_id = conexao.execute(
            text("SELECT veiculo->>'veiculo_id' FROM diagnosticos")
        ).scalar_one()
        conexao.execute(
            text(
                "UPDATE diagnosticos SET veiculo = jsonb_set(veiculo, '{placa}', "
                "to_jsonb('ANONIMIZADO:' || (veiculo->>'veiculo_id')))"
            )
        )

    resposta = api.get(URL, headers=mecanico)

    assert resposta.status_code == 200
    [item] = resposta.json()["items"]
    assert item["ordem_id"] == str(ordem_id)
    assert item["veiculo"]["placa"] == f"ANONIMIZADO:{veiculo_id}"


def test_retrato_corrompido_no_banco_e_500_nao_422(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    engine: Engine,
) -> None:
    _solicitar(session_factory)
    with engine.begin() as conexao:
        conexao.execute(text("""UPDATE diagnosticos SET veiculo = '{"placa": "X"}'"""))

    resposta = TestClient(api.app, raise_server_exceptions=False).get(
        URL, headers=mecanico
    )

    assert resposta.status_code == 500
    assert resposta.json()["erro"]["codigo"] == "ERRO_INTERNO"


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


# Nome e placa no texto livre: nenhum dos dois pode chegar ao log.
_MARCADOR = "marcador-lgpd Joao da Silva ABC1D23"


@pytest.mark.parametrize(
    ("iniciar", "billing_responde", "observacoes", "status"),
    [
        pytest.param(True, 200, _MARCADOR, 422, id="422-codigo-sem-preco"),
        pytest.param(True, 200, _MARCADOR + "x" * 2000, 422, id="422-schema"),
        pytest.param(False, 200, _MARCADOR, 409, id="409-nao-iniciado"),
        pytest.param(True, 503, _MARCADOR, 503, id="503-billing-fora"),
    ],
)
def test_texto_livre_nunca_chega_ao_log(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    billing: respx.MockRouter,
    log_capturado: io.StringIO,
    iniciar: bool,
    billing_responde: int,
    observacoes: str,
    status: int,
) -> None:
    semear(session_factory)
    ordem_id = _solicitar(session_factory, descricao=f"descricao {_MARCADOR}")
    if iniciar:
        api.post(f"{URL}/{ordem_id}/inicio", headers=mecanico)
    billing.post(CAMINHO_VALIDACAO).respond(
        billing_responde, json={"invalidos": ["SRV-TROCA-PASTILHA"]}
    )

    resposta = api.post(
        f"{URL}/{ordem_id}/conclusao",
        json={"itens": ITENS, "observacoes": observacoes},
        headers=mecanico,
    )

    assert resposta.status_code == status
    assert "marcador-lgpd" not in resposta.text
    log = log_capturado.getvalue()
    assert log  # o pipeline real registrou o request
    assert "marcador-lgpd" not in log
    assert "Joao da Silva" not in log
    assert "ABC1D23" not in log


def test_observacoes_com_nul_e_422_antes_do_billing(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
) -> None:
    # NUL passava pelo schema e pelo dominio e o psycopg2 recusava o UPDATE: 500.
    rota = billing.post(CAMINHO_VALIDACAO).respond(200, json={"invalidos": []})
    resposta = api.post(
        f"{URL}/{em_andamento}/conclusao",
        json={"itens": ITENS, "observacoes": "pastilha\u0000gasta"},
        headers=mecanico,
    )
    assert resposta.status_code == 422
    assert resposta.json()["erro"]["codigo"] == "VALOR_INVALIDO"
    assert rota.call_count == 0
    lista = api.get(URL, headers=mecanico).json()["items"]
    assert [d["status"] for d in lista] == ["EM_ANDAMENTO"]


def test_erro_de_banco_no_meio_da_conclusao_nao_leva_texto_livre_ao_log(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
    engine: Engine,
    log_capturado: io.StringIO,
) -> None:
    # Uma constraint que recusa o UPDATE: o DETAIL do Postgres traz a linha
    # inteira (placa, descricao, observacoes); o 500 so pode logar o codigo.
    billing.post(CAMINHO_VALIDACAO).respond(200, json={"invalidos": []})
    restricao = "ck_teste_observacoes"
    with engine.begin() as conexao:
        conexao.execute(
            text(
                f"ALTER TABLE diagnosticos ADD CONSTRAINT {restricao} "
                "CHECK (observacoes NOT LIKE '%marcador-lgpd%')"
            )
        )
    # Mesmo app (lifespan ja rodou), mas devolvendo o 500 em vez de relancar.
    cliente = TestClient(api.app, raise_server_exceptions=False)
    try:
        resposta = cliente.post(
            f"{URL}/{em_andamento}/conclusao",
            json={"itens": ITENS, "observacoes": "cliente marcador-lgpd ligou"},
            headers=mecanico,
        )
    finally:
        with engine.begin() as conexao:
            conexao.execute(
                text(f"ALTER TABLE diagnosticos DROP CONSTRAINT {restricao}")
            )

    assert resposta.status_code == 500
    assert resposta.json()["erro"]["codigo"] == "ERRO_INTERNO"
    saida = log_capturado.getvalue()
    [erro] = [
        json.loads(linha) for linha in saida.splitlines() if "internal_error" in linha
    ]
    assert (erro["erro"], erro["pgcode"], erro["constraint"]) == (
        "IntegrityError",
        "23514",
        restricao,
    )
    assert "marcador-lgpd" not in saida
    assert "BRA2E19" not in saida
    assert "Freio chiando" not in saida


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


def test_billing_recusando_o_pedido_e_502_sem_retry(
    api: TestClient,
    mecanico: dict[str, str],
    em_andamento: UUID,
    billing: respx.MockRouter,
) -> None:
    # 4xx do Billing (token ou contrato) nao se resolve repetindo: 502, nao 503.
    rota = billing.post(CAMINHO_VALIDACAO).respond(401)
    resposta = api.post(
        f"{URL}/{em_andamento}/conclusao", json={"itens": ITENS}, headers=mecanico
    )
    assert resposta.status_code == 502
    assert resposta.json()["erro"]["codigo"] == "RESPOSTA_INVALIDA_DA_DEPENDENCIA"
    assert "HTTP 401" in resposta.json()["erro"]["mensagem"]
    assert rota.call_count == 1


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


def test_log_do_request_leva_a_ordem_como_correlation_id(
    api: TestClient, mecanico: dict[str, str], log_capturado: io.StringIO
) -> None:
    # Chave de busca da saga no Loki (ADR-043), inclusive no log do handler de
    # erro, que roda fora da thread da rota.
    ordem_id = uuid4()
    api.post(f"{URL}/{ordem_id}/inicio", headers=mecanico)
    registros = [json.loads(linha) for linha in log_capturado.getvalue().splitlines()]
    [negacao] = [r for r in registros if r["event"] == "domain_exception_handled"]
    assert negacao["correlation_id"] == str(ordem_id)


def test_ordem_fora_do_formato_e_422_sem_correlation_id(
    api: TestClient, mecanico: dict[str, str], log_capturado: io.StringIO
) -> None:
    resposta = api.post(f"{URL}/nao-e-uuid/inicio", headers=mecanico)
    assert resposta.status_code == 422
    assert resposta.json()["detail"][0]["loc"] == ["path", "ordem_id"]
    assert "correlation_id" not in log_capturado.getvalue()


def test_atendente_nao_mexe_em_diagnostico(
    api: TestClient,
    autenticar: Callable[..., dict[str, str]],
    session_factory: sessionmaker[Session],
) -> None:
    ordem_id = _solicitar(session_factory)
    atendente = autenticar("atendente")
    assert api.get(URL, headers=atendente).status_code == 403
    assert api.post(f"{URL}/{ordem_id}/inicio", headers=atendente).status_code == 403
