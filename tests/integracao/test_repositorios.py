from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from src.compartilhado.dominio.veiculo import Veiculo
from src.compartilhado.infraestrutura.tipos_sqlalchemy import (
    DadoPersistidoInvalidoError,
)
from src.diagnostico.dominio.diagnostico import (
    Diagnostico,
    ItemDiagnostico,
    StatusDiagnostico,
    TipoItem,
)
from src.diagnostico.infraestrutura.adapters import CatalogoDePecasSQLAlchemy
from src.diagnostico.infraestrutura.mapping import diagnosticos_table
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.estoque.dominio.item_estoque import ItemEstoque
from src.estoque.dominio.reserva import Faltante, ItemReserva, Reserva, StatusReserva
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)
from src.execucao.dominio.execucao import Execucao
from src.execucao.infraestrutura.adapters import EstoqueSQLAlchemyAdapter
from src.execucao.infraestrutura.repository import (
    ExecucaoSQLAlchemyRepository,
    FilaDeExecucaoSQLAlchemy,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _item(sku: str, quantidade: int = 5) -> ItemEstoque:
    return ItemEstoque.criar(sku=Sku(sku), nome=sku, quantidade_disponivel=quantidade)


def _gravar(session_factory: sessionmaker[Session], *objetos: object) -> None:
    with session_factory() as session:
        session.add_all(objetos)
        session.commit()


_VEICULO_GRAVADO = {
    "veiculo_id": "6f1d2a7e-0000-4000-8000-000000000001",
    "placa": "ABC1234",
    "marca": "VW",
    "modelo": "Gol",
    "ano": 2010,
}


class TestEstoque:
    def test_sku_corrompido_e_erro_do_servidor_nao_do_cliente(
        self, session_factory: sessionmaker[Session], engine: Engine
    ) -> None:
        _gravar(session_factory, _item("PEC-VELA", 3))
        with engine.begin() as conexao:
            conexao.execute(text("UPDATE itens_estoque SET sku = 'pec vela'"))

        with session_factory() as session:
            repo = ItemEstoqueSQLAlchemyRepository(session)
            with pytest.raises(DadoPersistidoInvalidoError):
                repo.listar(offset=0, limit=10)

    def test_item_ida_e_volta_com_sku_como_vo(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        item = _item("PEC-VELA", 3)
        item.reservar(1)
        _gravar(session_factory, item)

        with session_factory() as session:
            lido = ItemEstoqueSQLAlchemyRepository(session).obter_por_sku(
                Sku("PEC-VELA")
            )
            assert lido is not None
            assert lido == item
            assert lido.sku == Sku("PEC-VELA")
            assert (lido.quantidade_disponivel, lido.quantidade_reservada) == (3, 1)
            assert (
                ItemEstoqueSQLAlchemyRepository(session).obter_por_sku(Sku("PEC-X"))
                is None
            )

    def test_lock_em_lote_ignora_ausentes(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        _gravar(session_factory, _item("PEC-B"), _item("PEC-A"))
        with session_factory() as session:
            repo = ItemEstoqueSQLAlchemyRepository(session)
            travados = repo.obter_com_lock([Sku("PEC-B"), Sku("PEC-Z"), Sku("PEC-A")])
            assert list(travados) == [Sku("PEC-A"), Sku("PEC-B")]
            assert repo.obter_com_lock([]) == {}

    def test_listar_paginado_por_sku(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        _gravar(session_factory, _item("PEC-C"), _item("PEC-A"), _item("PEC-B"))
        with session_factory() as session:
            repo = ItemEstoqueSQLAlchemyRepository(session)
            assert [str(i.sku) for i in repo.listar(offset=1, limit=5)] == [
                "PEC-B",
                "PEC-C",
            ]
            assert repo.contar() == 3

    def test_reserva_ida_e_volta(self, session_factory: sessionmaker[Session]) -> None:
        ordem_id = uuid4()
        itens = [ItemReserva(Sku("PEC-A"), 2), ItemReserva(Sku("PEC-B"), 1)]
        _gravar(
            session_factory, Reserva.criar(ordem_id=ordem_id, itens=itens, agora=T0)
        )

        with session_factory() as session:
            lida = ReservaSQLAlchemyRepository(session).obter_por_ordem(
                ordem_id, com_lock=True
            )
            assert lida is not None
            assert (lida.status, lida.itens, lida.criada_em) == (
                StatusReserva.ATIVA,
                tuple(itens),
                T0,
            )
            assert ReservaSQLAlchemyRepository(session).obter_por_ordem(uuid4()) is None

    def test_recusa_guarda_os_faltantes(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = uuid4()
        faltantes = [Faltante(sku=Sku("PEC-VELA"), solicitado=4, disponivel=0)]
        recusada = Reserva.recusar(
            ordem_id=ordem_id,
            itens=[ItemReserva(Sku("PEC-VELA"), 4)],
            faltantes=faltantes,
            agora=T0,
        )
        _gravar(session_factory, recusada)

        with session_factory() as session:
            lida = ReservaSQLAlchemyRepository(session).obter_por_ordem(ordem_id)
            assert lida is not None
            assert (lida.status, lida.faltantes) == (
                StatusReserva.RECUSADA,
                tuple(faltantes),
            )
            assert lida.encerrada_em == T0

    def test_uma_reserva_por_ordem_no_banco(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = uuid4()
        _gravar(session_factory, Reserva.criar(ordem_id=ordem_id, itens=[], agora=T0))
        segunda = Reserva.criar(ordem_id=ordem_id, itens=[], agora=T0)
        with pytest.raises(IntegrityError):
            _gravar(session_factory, segunda)

    def test_catalogo_de_pecas_aponta_ausentes_e_inativos(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        inativo = _item("PEC-INATIVA")
        inativo.desativar()
        _gravar(session_factory, _item("PEC-ATIVA"), inativo)
        with session_factory() as session:
            catalogo = CatalogoDePecasSQLAlchemy(session)
            assert catalogo.skus_indisponiveis(
                ["PEC-NOVA", "PEC-ATIVA", "PEC-INATIVA"]
            ) == [
                "PEC-NOVA",
                "PEC-INATIVA",
            ]


class TestDiagnosticos:
    def _diagnostico(self, minutos: int, status: str = "AGUARDANDO") -> Diagnostico:
        diagnostico = Diagnostico.solicitar(
            ordem_id=uuid4(),
            veiculo=Veiculo(
                veiculo_id=uuid4(),
                placa="ABC1D23",
                marca="Fiat",
                modelo="Uno",
                ano=2015,
            ),
            descricao_problema="Revisao",
            agora=T0 + timedelta(minutes=minutos),
        )
        if status != "AGUARDANDO":
            diagnostico.iniciar(uuid4(), T0)
        return diagnostico

    def test_ida_e_volta_com_veiculo_e_itens(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        diagnostico = self._diagnostico(0)
        mecanico = uuid4()
        diagnostico.iniciar(mecanico, T0)
        itens = [ItemDiagnostico(tipo=TipoItem.PECA, codigo="PEC-VELA", quantidade=4)]
        diagnostico.concluir(mecanico, itens, "ok", T0)
        _gravar(session_factory, diagnostico)

        with session_factory() as session:
            lido = DiagnosticoSQLAlchemyRepository(session).obter(diagnostico.ordem_id)
            assert lido is not None
            assert lido.status is StatusDiagnostico.CONCLUIDO
            assert lido.veiculo == diagnostico.veiculo
            assert lido.itens == tuple(itens)
            assert (lido.mecanico_id, lido.concluido_em) == (mecanico, T0)

    def test_retrato_anonimizado_volta_sem_revalidar_a_placa(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        diagnostico = self._diagnostico(0)
        assert diagnostico.veiculo is not None
        anonimo = diagnostico.veiculo.anonimizar()
        _gravar(session_factory, diagnostico)
        with session_factory() as session:
            session.execute(
                update(diagnosticos_table)
                .where(diagnosticos_table.c.ordem_id == diagnostico.ordem_id)
                .values(veiculo=anonimo)
            )
            session.commit()

        with session_factory() as session:
            repo = DiagnosticoSQLAlchemyRepository(session)
            lido = repo.obter(diagnostico.ordem_id)
            assert lido is not None
            assert lido.veiculo == anonimo
            assert lido.veiculo.placa == f"ANONIMIZADO:{anonimo.veiculo_id}"
            assert repo.listar(None, offset=0, limit=10) == [lido]

    @pytest.mark.parametrize(
        "veiculo",
        [
            pytest.param({"placa": "ABC1234"}, id="sem-chaves"),
            pytest.param("texto", id="nao-e-objeto"),
            pytest.param({**_VEICULO_GRAVADO, "veiculo_id": "x"}, id="id-invalido"),
        ],
    )
    def test_linha_corrompida_e_erro_do_servidor_nao_do_cliente(
        self, session_factory: sessionmaker[Session], engine: Engine, veiculo: object
    ) -> None:
        diagnostico = self._diagnostico(0)
        _gravar(session_factory, diagnostico)
        with engine.begin() as conexao:
            conexao.execute(
                text("UPDATE diagnosticos SET veiculo = CAST(:v AS jsonb)"),
                {"v": json.dumps(veiculo)},
            )

        with session_factory() as session:
            repo = DiagnosticoSQLAlchemyRepository(session)
            with pytest.raises(DadoPersistidoInvalidoError):
                repo.obter(diagnostico.ordem_id)

    def test_listar_por_status_em_ordem_de_chegada(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        tarde, cedo = self._diagnostico(10), self._diagnostico(1)
        andamento = self._diagnostico(5, status="EM_ANDAMENTO")
        _gravar(session_factory, tarde, cedo, andamento)

        with session_factory() as session:
            repo = DiagnosticoSQLAlchemyRepository(session)
            aguardando = repo.listar(StatusDiagnostico.AGUARDANDO, offset=0, limit=10)
            assert [d.ordem_id for d in aguardando] == [cedo.ordem_id, tarde.ordem_id]
            todos = repo.listar(None, offset=1, limit=10)
            assert [d.ordem_id for d in todos] == [andamento.ordem_id, tarde.ordem_id]
            assert repo.contar(StatusDiagnostico.AGUARDANDO) == 2
            assert repo.contar(None) == 3


class TestFilaDeExecucao:
    def _agendar(
        self, prioridade: int, minutos: int, ordem_id: UUID | None = None
    ) -> Execucao:
        return Execucao.agendar(
            ordem_id=ordem_id or uuid4(),
            prioridade=prioridade,
            veiculo=None,
            agora=T0 + timedelta(minutes=minutos),
        )

    def test_prioridade_desc_chegada_asc_e_desempate_por_ordem(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        normal_cedo = self._agendar(0, 1)
        normal_tarde = self._agendar(0, 5)
        urgente = self._agendar(10, 9)
        empate_a = self._agendar(3, 2, UUID(int=1))
        empate_b = self._agendar(3, 2, UUID(int=2))
        iniciada = self._agendar(50, 0)
        iniciada.iniciar(uuid4(), T0)
        _gravar(
            session_factory,
            normal_cedo,
            normal_tarde,
            urgente,
            empate_b,
            empate_a,
            iniciada,
        )

        with session_factory() as session:
            fila = FilaDeExecucaoSQLAlchemy(session)
            esperada = [urgente, empate_a, empate_b, normal_cedo, normal_tarde]
            itens = fila.listar(offset=0, limit=10)
            assert [i.ordem_id for i in itens] == [e.ordem_id for e in esperada]
            assert [i.posicao for i in itens] == [1, 2, 3, 4, 5]
            assert [i.posicao for i in fila.listar(offset=3, limit=10)] == [4, 5]
            assert fila.contar() == 5
            repo = ExecucaoSQLAlchemyRepository(session)
            for posicao, execucao in enumerate(esperada, start=1):
                lida = repo.obter(execucao.ordem_id)
                assert lida is not None
                assert fila.posicao(lida) == posicao


class TestEstoqueDaExecucao:
    def test_sem_reserva(self, session_factory: sessionmaker[Session]) -> None:
        with session_factory() as session:
            estoque = EstoqueSQLAlchemyAdapter(session)
            assert estoque.tem_reserva_ativa(uuid4()) is False
            assert estoque.consumir_reserva(uuid4(), T0) is None

    def test_consome_reserva_ativa(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        item = _item("PEC-VELA", 5)
        item.reservar(2)
        ordem_id = uuid4()
        reserva = Reserva.criar(
            ordem_id=ordem_id, itens=[ItemReserva(Sku("PEC-VELA"), 2)], agora=T0
        )
        _gravar(session_factory, item, reserva)

        with session_factory() as session:
            estoque = EstoqueSQLAlchemyAdapter(session)
            assert estoque.tem_reserva_ativa(ordem_id) is True
            pecas = estoque.consumir_reserva(ordem_id, T0)
            session.commit()

        assert pecas is not None
        assert [(p.sku, p.quantidade) for p in pecas] == [("PEC-VELA", 2)]
        with session_factory() as session:
            lido = ItemEstoqueSQLAlchemyRepository(session).obter_por_sku(
                Sku("PEC-VELA")
            )
            assert lido is not None
            assert (lido.quantidade_disponivel, lido.quantidade_reservada) == (3, 0)
            consumida = ReservaSQLAlchemyRepository(session).obter_por_ordem(ordem_id)
            assert consumida is not None
            assert consumida.status is StatusReserva.CONSUMIDA
