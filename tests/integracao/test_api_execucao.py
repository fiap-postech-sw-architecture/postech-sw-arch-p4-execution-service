from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException
from src.compartilhado.dominio.veiculo import Veiculo
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.diagnostico.aplicacao.use_cases import RegistrarSolicitacaoDeDiagnostico
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.estoque.aplicacao.use_cases import ReservarPecas
from src.estoque.dominio.reserva import ItemReserva
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)
from src.estoque.infraestrutura.seed import semear
from src.execucao.aplicacao.use_cases import AgendarExecucao, CancelarExecucao
from src.execucao.dominio.execucao import Prioridade
from src.execucao.infraestrutura.adapters import VeiculosSQLAlchemy
from src.execucao.infraestrutura.repository import (
    ExecucaoSQLAlchemyRepository,
    FilaDeExecucaoSQLAlchemy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session, sessionmaker

MECANICO = uuid4()


def _agendar(
    session_factory: sessionmaker[Session],
    prioridade: Prioridade = Prioridade.NORMAL,
    ordem_id: UUID | None = None,
) -> UUID:
    ordem_id = ordem_id or uuid4()
    with session_factory() as session:
        AgendarExecucao(
            ExecucaoSQLAlchemyRepository(session),
            FilaDeExecucaoSQLAlchemy(session),
            VeiculosSQLAlchemy(session),
            SQLAlchemyUnitOfWork(lambda: session),
        ).executar(ordem_id, prioridade)
    return ordem_id


def _reservar(
    session_factory: sessionmaker[Session], ordem_id: UUID, **pecas: int
) -> None:
    with session_factory() as session:
        ReservarPecas(
            ItemEstoqueSQLAlchemyRepository(session),
            ReservaSQLAlchemyRepository(session),
            SQLAlchemyUnitOfWork(lambda: session),
        ).executar(ordem_id, [ItemReserva(Sku(sku), q) for sku, q in pecas.items()])


@pytest.fixture
def mecanico(autenticar: Callable[..., dict[str, str]]) -> dict[str, str]:
    return autenticar("mecanico", MECANICO)


def test_fila_com_posicao_para_mecanico_e_atendente(
    api: TestClient,
    session_factory: sessionmaker[Session],
    autenticar: Callable[..., dict[str, str]],
) -> None:
    normal = _agendar(session_factory, Prioridade.NORMAL)
    urgente = _agendar(session_factory, Prioridade.ALTA)
    for papel in ["mecanico", "atendente", "admin"]:
        resposta = api.get("/api/v1/fila", headers=autenticar(papel))
        assert resposta.status_code == 200
        corpo = resposta.json()
        assert [(i["posicao"], i["ordem_id"]) for i in corpo["items"]] == [
            (1, str(urgente)),
            (2, str(normal)),
        ]
        assert corpo["total"] == 2
    pagina = api.get(
        "/api/v1/fila", params={"offset": 1}, headers=autenticar("atendente")
    )
    assert [i["posicao"] for i in pagina.json()["items"]] == [2]


def test_fila_mostra_o_retrato_copiado_do_diagnostico(
    api: TestClient,
    session_factory: sessionmaker[Session],
    autenticar: Callable[..., dict[str, str]],
) -> None:
    ordem_id = uuid4()
    veiculo = Veiculo(
        veiculo_id=uuid4(), placa="bra-2e19", marca="Chevrolet", modelo="Onix", ano=2022
    )
    with session_factory() as session:
        RegistrarSolicitacaoDeDiagnostico(
            DiagnosticoSQLAlchemyRepository(session),
            SQLAlchemyUnitOfWork(lambda: session),
        ).executar(ordem_id, veiculo, "Freio chiando")
    _agendar(session_factory, ordem_id=ordem_id)
    sem_diagnostico = _agendar(session_factory)

    corpo = api.get("/api/v1/fila", headers=autenticar("mecanico")).json()

    retratos = {item["ordem_id"]: item["veiculo"] for item in corpo["items"]}
    assert retratos == {
        str(ordem_id): {
            "placa": "BRA2E19",
            "marca": "Chevrolet",
            "modelo": "Onix",
            "ano": 2022,
        },
        str(sem_diagnostico): None,
    }


def test_inicio_e_finalizacao_com_baixa_do_estoque(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
    autenticar: Callable[..., dict[str, str]],
) -> None:
    semear(session_factory)
    ordem_id = _agendar(session_factory)
    _reservar(session_factory, ordem_id, **{"PEC-OLEO-5W30": 4, "PEC-FILTRO-OLEO": 1})

    inicio = api.post(f"/api/v1/execucoes/{ordem_id}/inicio", headers=mecanico)
    assert inicio.status_code == 200
    assert (inicio.json()["status"], inicio.json()["mecanico_id"]) == (
        "EM_EXECUCAO",
        str(MECANICO),
    )
    assert api.get("/api/v1/fila", headers=mecanico).json()["total"] == 0

    fim = api.post(f"/api/v1/execucoes/{ordem_id}/finalizacao", headers=mecanico)
    repetido = api.post(f"/api/v1/execucoes/{ordem_id}/finalizacao", headers=mecanico)
    assert fim.status_code == repetido.status_code == 200
    assert fim.json()["status"] == "FINALIZADA"
    assert repetido.json() == fim.json()

    admin = autenticar("admin")
    oleo = api.get("/api/v1/estoque/PEC-OLEO-5W30", headers=admin).json()
    assert (oleo["quantidade_disponivel"], oleo["quantidade_reservada"]) == (36, 0)
    linhas = [linha for linha in outbox() if linha["correlation_id"] == ordem_id]
    assert [linha["tipo"] for linha in linhas] == [
        "ExecucaoAgendada",
        "PecasReservadas",
        "ExecucaoIniciada",
        "ExecucaoFinalizada",
    ]
    assert linhas[-1]["dados"]["pecas_consumidas"] == [
        {"sku": "PEC-OLEO-5W30", "quantidade": 4},
        {"sku": "PEC-FILTRO-OLEO", "quantidade": 1},
    ]


def _em_execucao(
    api: TestClient, mecanico: dict[str, str], session_factory: sessionmaker[Session]
) -> UUID:
    ordem_id = _agendar(session_factory)
    _reservar(session_factory, ordem_id)  # servico sem peca: reserva vazia e valida
    resposta = api.post(f"/api/v1/execucoes/{ordem_id}/inicio", headers=mecanico)
    assert resposta.status_code == 200, resposta.text
    return ordem_id


def test_inicio_sem_reserva_ativa_e_409_e_nao_cruza_o_pivot(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    ordem_id = _agendar(session_factory)
    resposta = api.post(f"/api/v1/execucoes/{ordem_id}/inicio", headers=mecanico)
    assert resposta.status_code == 409
    assert "reserva" in resposta.json()["erro"]["mensagem"]
    with session_factory() as session:
        execucao = ExecucaoSQLAlchemyRepository(session).obter(ordem_id)
        assert execucao is not None
        assert execucao.status == "AGUARDANDO"
    assert "ExecucaoIniciada" not in [linha["tipo"] for linha in outbox()]


def test_pivot_depois_de_iniciar_nao_cancela(
    api: TestClient, mecanico: dict[str, str], session_factory: sessionmaker[Session]
) -> None:
    ordem_id = _em_execucao(api, mecanico, session_factory)
    with session_factory() as session:
        uc = CancelarExecucao(
            ExecucaoSQLAlchemyRepository(session), SQLAlchemyUnitOfWork(lambda: session)
        )
        with pytest.raises(TransicaoStatusInvalidaException):
            uc.executar(ordem_id)


def test_outro_mecanico_nao_finaliza_mas_o_admin_finaliza_em_nome_dele(
    api: TestClient,
    mecanico: dict[str, str],
    session_factory: sessionmaker[Session],
    autenticar: Callable[..., dict[str, str]],
) -> None:
    ordem_id = _em_execucao(api, mecanico, session_factory)
    url = f"/api/v1/execucoes/{ordem_id}/finalizacao"
    assert api.post(url, headers=autenticar("mecanico")).status_code == 403
    resposta = api.post(url, headers=autenticar("admin"))
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "FINALIZADA"
    assert resposta.json()["mecanico_id"] == str(MECANICO)


def test_atendente_so_le_a_fila(
    api: TestClient,
    session_factory: sessionmaker[Session],
    autenticar: Callable[..., dict[str, str]],
) -> None:
    ordem_id = _agendar(session_factory)
    atendente = autenticar("atendente")
    assert (
        api.post(f"/api/v1/execucoes/{ordem_id}/inicio", headers=atendente).status_code
        == 403
    )
    assert (
        api.post(
            f"/api/v1/execucoes/{ordem_id}/finalizacao", headers=atendente
        ).status_code
        == 403
    )


def test_execucao_desconhecida_e_404(api: TestClient, mecanico: dict[str, str]) -> None:
    resposta = api.post(f"/api/v1/execucoes/{uuid4()}/inicio", headers=mecanico)
    assert resposta.status_code == 404
