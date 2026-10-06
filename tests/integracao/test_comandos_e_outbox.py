"""Casos de uso dos comandos da saga contra o Postgres: estado + outbox atomicos."""

from __future__ import annotations

import select
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from src.compartilhado.dominio.exceptions import ViolacaoRegraDeNegocioException
from src.compartilhado.dominio.veiculo import Veiculo
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.diagnostico.aplicacao.use_cases import (
    DescartarDiagnostico,
    RegistrarSolicitacaoDeDiagnostico,
)
from src.diagnostico.dominio.diagnostico import StatusDiagnostico
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.estoque.aplicacao.use_cases import (
    AjustarQuantidade,
    CriarItemEstoque,
    LiberarReserva,
    ReservarPecas,
)
from src.estoque.dominio.reserva import ItemReserva, StatusReserva
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)
from src.execucao.aplicacao.use_cases import AgendarExecucao, CancelarExecucao
from src.execucao.dominio.execucao import Prioridade, StatusExecucao
from src.execucao.infraestrutura.adapters import (
    EstoqueSQLAlchemyAdapter,
    VeiculosSQLAlchemy,
)
from src.execucao.infraestrutura.repository import (
    ExecucaoSQLAlchemyRepository,
    FilaDeExecucaoSQLAlchemy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

VELA = Sku("PEC-VELA")


def _uow(session: Session) -> SQLAlchemyUnitOfWork:
    return SQLAlchemyUnitOfWork(lambda: session)


def _criar_item(
    session_factory: sessionmaker[Session], sku: Sku, quantidade: int
) -> None:
    with session_factory() as session:
        CriarItemEstoque(
            ItemEstoqueSQLAlchemyRepository(session), _uow(session)
        ).executar(sku=sku, nome=str(sku), quantidade_disponivel=quantidade)


def _reservar(
    session_factory: sessionmaker[Session], ordem_id: Any, quantidade: int
) -> Any:
    """Reserva ``quantidade`` velas (0 = reserva sem pecas, de servico puro)."""
    pecas = [ItemReserva(VELA, quantidade)] if quantidade else []
    with session_factory() as session:
        return ReservarPecas(
            ItemEstoqueSQLAlchemyRepository(session),
            ReservaSQLAlchemyRepository(session),
            _uow(session),
        ).executar(ordem_id, pecas)


def _agendador(
    session: Session, fila: FilaDeExecucaoSQLAlchemy | None = None
) -> AgendarExecucao:
    return AgendarExecucao(
        ExecucaoSQLAlchemyRepository(session),
        fila or FilaDeExecucaoSQLAlchemy(session),
        VeiculosSQLAlchemy(session),
        EstoqueSQLAlchemyAdapter(session),
        _uow(session),
    )


def _liberar(session_factory: sessionmaker[Session], ordem_id: Any) -> None:
    with session_factory() as session:
        LiberarReserva(
            ItemEstoqueSQLAlchemyRepository(session),
            ReservaSQLAlchemyRepository(session),
            _uow(session),
        ).executar(ordem_id)


def _saldo(session_factory: sessionmaker[Session]) -> tuple[int, int]:
    with session_factory() as session:
        item = ItemEstoqueSQLAlchemyRepository(session).obter_por_sku(VELA)
        assert item is not None
        return item.quantidade_disponivel, item.quantidade_reservada


def test_reserva_e_liberacao_gravam_estado_e_outbox_juntos(
    session_factory: sessionmaker[Session], outbox: Callable[[], list[dict[str, Any]]]
) -> None:
    _criar_item(session_factory, VELA, 5)
    ordem_id = uuid4()

    reserva = _reservar(session_factory, ordem_id, 3)
    assert _saldo(session_factory) == (5, 3)
    _liberar(session_factory, ordem_id)
    _liberar(session_factory, ordem_id)  # reenvio da compensacao
    assert _saldo(session_factory) == (5, 0)

    linhas = outbox()
    assert [(linha["tipo"], linha["dados"]) for linha in linhas] == [
        ("PecasReservadas", {"ordem_id": str(ordem_id), "reserva_id": str(reserva.id)}),
        ("ReservaLiberada", {"ordem_id": str(ordem_id)}),
        ("ReservaLiberada", {"ordem_id": str(ordem_id)}),
    ]
    assert {linha["correlation_id"] for linha in linhas} == {ordem_id}
    assert {linha["status"] for linha in linhas} == {"pendente"}
    assert len({linha["mensagem_id"] for linha in linhas}) == 3


def test_falta_de_peca_registra_a_recusa_e_responde_igual_no_reenvio(
    session_factory: sessionmaker[Session], outbox: Callable[[], list[dict[str, Any]]]
) -> None:
    _criar_item(session_factory, VELA, 0)
    ordem_id = uuid4()
    assert _reservar(session_factory, ordem_id, 2).status is StatusReserva.RECUSADA
    # Estoque reposto antes do reenvio do comando: a decisao nao muda.
    with session_factory() as session:
        AjustarQuantidade(
            ItemEstoqueSQLAlchemyRepository(session), _uow(session)
        ).executar(VELA, 10)
    assert _reservar(session_factory, ordem_id, 2).status is StatusReserva.RECUSADA
    assert _saldo(session_factory) == (10, 0)
    falha = (
        "ReservaDePecasFalhou",
        {
            "ordem_id": str(ordem_id),
            "faltantes": [{"sku": "PEC-VELA", "solicitado": 2, "disponivel": 0}],
        },
    )
    assert [(linha["tipo"], linha["dados"]) for linha in outbox()] == [
        falha,
        falha,
    ]


def test_erro_no_meio_do_caso_de_uso_nao_deixa_rastro(
    session_factory: sessionmaker[Session], outbox: Callable[[], list[dict[str, Any]]]
) -> None:
    ordem_id = uuid4()
    _reservar(session_factory, ordem_id, 0)
    respostas = outbox()
    with session_factory() as session:
        uc = _agendador(session, _FilaQueQuebra(session))
        with pytest.raises(RuntimeError):
            uc.executar(ordem_id, Prioridade.NORMAL)
    with session_factory() as session:
        assert ExecucaoSQLAlchemyRepository(session).obter(ordem_id) is None
    assert outbox() == respostas  # so a PecasReservadas de antes


def test_agendamento_sem_reserva_ativa_nao_entra_na_fila(
    session_factory: sessionmaker[Session], outbox: Callable[[], list[dict[str, Any]]]
) -> None:
    # RN-027: sem pecas reservadas a execucao nunca poderia ser finalizada.
    ordem_id = uuid4()
    with session_factory() as session:
        uc = _agendador(session)
        with pytest.raises(ViolacaoRegraDeNegocioException, match="reserva"):
            uc.executar(ordem_id, Prioridade.NORMAL)
    with session_factory() as session:
        assert ExecucaoSQLAlchemyRepository(session).obter(ordem_id) is None
    assert outbox() == []


class _FilaQueQuebra(FilaDeExecucaoSQLAlchemy):
    def posicao(self, execucao: Any) -> int:
        raise RuntimeError


def test_agendamento_e_cancelamento(
    session_factory: sessionmaker[Session], outbox: Callable[[], list[dict[str, Any]]]
) -> None:
    primeira, segunda = uuid4(), uuid4()
    for ordem_id, prioridade in [
        (primeira, Prioridade.NORMAL),
        (segunda, Prioridade.ALTA),
    ]:
        _reservar(session_factory, ordem_id, 0)
        with session_factory() as session:
            _agendador(session).executar(ordem_id, prioridade)
    with session_factory() as session:
        CancelarExecucao(ExecucaoSQLAlchemyRepository(session), _uow(session)).executar(
            segunda
        )

    agendamentos = [
        (linha["tipo"], linha["dados"])
        for linha in outbox()
        if linha["tipo"] != "PecasReservadas"
    ]
    assert agendamentos == [
        ("ExecucaoAgendada", {"ordem_id": str(primeira), "posicao_na_fila": 1}),
        # Prioridade alta fura a fila: entra na frente da primeira.
        ("ExecucaoAgendada", {"ordem_id": str(segunda), "posicao_na_fila": 1}),
        ("ExecucaoCancelada", {"ordem_id": str(segunda)}),
    ]


def test_solicitacao_e_descarte_de_diagnostico(
    session_factory: sessionmaker[Session], outbox: Callable[[], list[dict[str, Any]]]
) -> None:
    ordem_id = uuid4()
    veiculo = Veiculo(
        veiculo_id=uuid4(), placa="ABC1234", marca="VW", modelo="Gol", ano=2010
    )
    for _ in range(2):  # reenvio do comando nao duplica
        with session_factory() as session:
            RegistrarSolicitacaoDeDiagnostico(
                DiagnosticoSQLAlchemyRepository(session), _uow(session)
            ).executar(ordem_id, veiculo, "Nao liga")
    with session_factory() as session:
        DescartarDiagnostico(
            DiagnosticoSQLAlchemyRepository(session), _uow(session)
        ).executar(ordem_id)
    assert [linha["tipo"] for linha in outbox()] == ["DiagnosticoDescartado"]


def test_compensacoes_antes_dos_originais_gravam_lapides_e_descartam_os_atrasados(
    session_factory: sessionmaker[Session], outbox: Callable[[], list[dict[str, Any]]]
) -> None:
    # Passos em voo compensados: a compensacao chega primeiro, o original depois.
    _criar_item(session_factory, VELA, 5)
    ordem_id = uuid4()
    with session_factory() as session:
        DescartarDiagnostico(
            DiagnosticoSQLAlchemyRepository(session), _uow(session)
        ).executar(ordem_id)
    with session_factory() as session:
        CancelarExecucao(ExecucaoSQLAlchemyRepository(session), _uow(session)).executar(
            ordem_id
        )
    _liberar(session_factory, ordem_id)
    respostas = [(linha["tipo"], linha["dados"]) for linha in outbox()]

    with session_factory() as session:
        RegistrarSolicitacaoDeDiagnostico(
            DiagnosticoSQLAlchemyRepository(session), _uow(session)
        ).executar(
            ordem_id,
            Veiculo(
                veiculo_id=uuid4(), placa="ABC1234", marca="VW", modelo="Gol", ano=2010
            ),
            "Nao liga",
        )
    reserva = _reservar(session_factory, ordem_id, 2)
    with session_factory() as session:
        _agendador(session).executar(ordem_id, Prioridade.NORMAL)

    assert respostas == [
        ("DiagnosticoDescartado", {"ordem_id": str(ordem_id)}),
        ("ExecucaoCancelada", {"ordem_id": str(ordem_id)}),
        ("ReservaLiberada", {"ordem_id": str(ordem_id)}),
    ]
    assert [(linha["tipo"], linha["dados"]) for linha in outbox()] == respostas
    assert (reserva.status, reserva.itens) == (StatusReserva.LIBERADA, ())
    assert _saldo(session_factory) == (5, 0)
    with session_factory() as session:
        diagnostico = DiagnosticoSQLAlchemyRepository(session).obter(ordem_id)
        execucao = ExecucaoSQLAlchemyRepository(session).obter(ordem_id)
        fila = FilaDeExecucaoSQLAlchemy(session).contar()
    assert diagnostico is not None
    assert (diagnostico.status, diagnostico.veiculo) == (
        StatusDiagnostico.DESCARTADO,
        None,
    )
    assert execucao is not None
    assert execucao.status is StatusExecucao.CANCELADA
    assert fila == 0


def test_commit_com_evento_acorda_o_relay(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    dsn = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    ouvinte = psycopg2.connect(dsn)
    try:
        ouvinte.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        ouvinte.cursor().execute("LISTEN outbox_novo")
        _criar_item(session_factory, VELA, 1)  # sem evento: nao notifica
        _reservar(session_factory, uuid4(), 1)

        prontos, _, _ = select.select([ouvinte], [], [], 5)
        assert prontos
        ouvinte.poll()
        assert [n.channel for n in ouvinte.notifies] == ["outbox_novo"]
    finally:
        ouvinte.close()
