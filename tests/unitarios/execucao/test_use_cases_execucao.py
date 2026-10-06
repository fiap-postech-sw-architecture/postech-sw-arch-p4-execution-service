from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from src.compartilhado.aplicacao.outbox import dados_do_evento
from src.compartilhado.dominio.exceptions import (
    OperacaoNaoPermitidaException,
    TransicaoStatusInvalidaException,
    ViolacaoRegraDeNegocioException,
)
from src.compartilhado.dominio.veiculo import Veiculo
from src.estoque.aplicacao.use_cases import ReservarPecas
from src.estoque.dominio.item_estoque import ItemEstoque
from src.estoque.dominio.reserva import ItemReserva, StatusReserva
from src.estoque.dominio.sku import Sku
from src.execucao.aplicacao.use_cases import (
    AgendarExecucao,
    CancelarExecucao,
    FinalizarExecucao,
    IniciarExecucao,
)
from src.execucao.dominio.exceptions import ExecucaoNaoEncontradaException
from src.execucao.dominio.execucao import Execucao, StatusExecucao
from tests.fakes import (
    EstoqueEmMemoria,
    ExecucoesEmMemoria,
    FakeUnitOfWork,
    FilaFixa,
    ItensEmMemoria,
    ReservasEmMemoria,
    VeiculosEmMemoria,
)

MECANICO, ADMIN = uuid4(), uuid4()
VELA = Sku("PEC-VELA")
VEICULO = Veiculo(
    veiculo_id=uuid4(), placa="ABC1D23", marca="Fiat", modelo="Uno", ano=2015
)


def _agendada(ordem_id: UUID | None = None) -> Execucao:
    return Execucao.agendar(
        ordem_id=ordem_id or uuid4(),
        prioridade=0,
        veiculo=None,
        agora=datetime.now(UTC),
    )


def _eventos(uow: FakeUnitOfWork) -> list[tuple[str, dict[str, object]]]:
    return [(e.tipo, dados_do_evento(e)) for e in uow.eventos]


@dataclass
class _Oficina:
    """Execucao agendada, estoque em memoria e os dois casos de uso do mecanico."""

    execucao: Execucao
    itens: ItensEmMemoria
    reservas: ReservasEmMemoria
    estoque: EstoqueEmMemoria
    uow: FakeUnitOfWork
    iniciar: IniciarExecucao
    finalizar: FinalizarExecucao


def _oficina(*, reservar: bool = True) -> _Oficina:
    execucao = _agendada()
    itens = ItensEmMemoria(
        ItemEstoque.criar(sku=VELA, nome="Vela", quantidade_disponivel=10)
    )
    reservas = ReservasEmMemoria()
    if reservar:
        ReservarPecas(itens, reservas, FakeUnitOfWork()).executar(
            execucao.ordem_id, [ItemReserva(VELA, 4)]
        )
    estoque, uow = EstoqueEmMemoria(itens, reservas), FakeUnitOfWork()
    repo = ExecucoesEmMemoria(execucao)
    return _Oficina(
        execucao,
        itens,
        reservas,
        estoque,
        uow,
        IniciarExecucao(repo, estoque, uow),
        FinalizarExecucao(repo, estoque, uow),
    )


def _em_execucao(*, reservar: bool = True) -> _Oficina:
    oficina = _oficina(reservar=reservar)
    oficina.execucao.iniciar(MECANICO, datetime.now(UTC))
    return oficina


class TestAgendar:
    def test_enfileira_com_o_retrato_e_responde_com_a_posicao(self) -> None:
        repo, uow = ExecucoesEmMemoria(), FakeUnitOfWork()
        ordem_id = uuid4()
        veiculos = VeiculosEmMemoria({ordem_id: VEICULO})
        execucao = AgendarExecucao(repo, FilaFixa(posicao=3), veiculos, uow).executar(
            ordem_id, 7
        )

        assert repo.execucoes[ordem_id] is execucao
        assert (execucao.status, execucao.prioridade) == (StatusExecucao.AGUARDANDO, 7)
        assert execucao.veiculo == VEICULO
        assert _eventos(uow) == [
            ("ExecucaoAgendada", {"ordem_id": str(ordem_id), "posicao_na_fila": 3})
        ]

    def test_reenvio_com_execucao_na_fila_reemite_e_mantem_prioridade(self) -> None:
        existente = _agendada()
        uow = FakeUnitOfWork()
        resultado = AgendarExecucao(
            ExecucoesEmMemoria(existente), FilaFixa(posicao=2), VeiculosEmMemoria(), uow
        ).executar(existente.ordem_id, 99)
        assert resultado is existente
        assert resultado.prioridade == 0
        assert _eventos(uow) == [
            (
                "ExecucaoAgendada",
                {"ordem_id": str(existente.ordem_id), "posicao_na_fila": 2},
            )
        ]

    def test_reenvio_atrasado_apos_inicio_e_ignorado(self) -> None:
        existente = _agendada()
        existente.iniciar(MECANICO, datetime.now(UTC))
        uow = FakeUnitOfWork()
        AgendarExecucao(
            ExecucoesEmMemoria(existente), FilaFixa(), VeiculosEmMemoria(), uow
        ).executar(existente.ordem_id, 0)
        assert (uow.eventos, uow.commits) == ([], 0)

    def test_agendamento_atrasado_encontra_a_lapide_e_e_descartado(self) -> None:
        repo = ExecucoesEmMemoria()
        ordem_id = uuid4()
        CancelarExecucao(repo, FakeUnitOfWork()).executar(ordem_id)
        lapide = repo.execucoes[ordem_id]
        uow = FakeUnitOfWork()

        resultado = AgendarExecucao(
            repo, FilaFixa(), VeiculosEmMemoria(), uow
        ).executar(ordem_id, 0)

        assert resultado is lapide
        assert resultado.status is StatusExecucao.CANCELADA
        assert (uow.eventos, uow.commits) == ([], 0)

    def test_prioridade_invalida(self) -> None:
        uc = AgendarExecucao(
            ExecucoesEmMemoria(), FilaFixa(), VeiculosEmMemoria(), FakeUnitOfWork()
        )
        ordem_id = uuid4()
        with pytest.raises(ValueError, match="Prioridade"):
            uc.executar(ordem_id, 101)


class TestCancelar:
    def test_cancela_e_responde(self) -> None:
        execucao = _agendada()
        uow = FakeUnitOfWork()
        CancelarExecucao(ExecucoesEmMemoria(execucao), uow).executar(execucao.ordem_id)
        assert execucao.status is StatusExecucao.CANCELADA
        assert _eventos(uow) == [
            ("ExecucaoCancelada", {"ordem_id": str(execucao.ordem_id)})
        ]

    def test_repetido_reemite_a_resposta(self) -> None:
        execucao = _agendada()
        repo, uow = ExecucoesEmMemoria(execucao), FakeUnitOfWork()
        CancelarExecucao(repo, uow).executar(execucao.ordem_id)
        CancelarExecucao(repo, uow).executar(execucao.ordem_id)
        assert [e.tipo for e in uow.eventos] == 2 * ["ExecucaoCancelada"]

    def test_depois_do_pivot_nao_cancela(self) -> None:
        execucao = _agendada()
        execucao.iniciar(MECANICO, datetime.now(UTC))
        uow = FakeUnitOfWork()
        uc = CancelarExecucao(ExecucoesEmMemoria(execucao), uow)
        with pytest.raises(TransicaoStatusInvalidaException):
            uc.executar(execucao.ordem_id)
        assert uow.eventos == []

    def test_compensacao_antes_do_original_grava_lapide_e_responde(self) -> None:
        repo, uow = ExecucoesEmMemoria(), FakeUnitOfWork()
        ordem_id = uuid4()

        CancelarExecucao(repo, uow).executar(ordem_id)

        lapide = repo.execucoes[ordem_id]
        assert lapide.status is StatusExecucao.CANCELADA
        assert lapide.cancelada_em is not None
        assert _eventos(uow) == [("ExecucaoCancelada", {"ordem_id": str(ordem_id)})]


class TestIniciar:
    def test_emite_execucao_iniciada(self) -> None:
        o = _oficina()
        o.iniciar.executar(o.execucao.ordem_id, MECANICO)
        assert o.execucao.iniciada_em is not None
        assert _eventos(o.uow) == [
            (
                "ExecucaoIniciada",
                {
                    "ordem_id": str(o.execucao.ordem_id),
                    "mecanico_id": str(MECANICO),
                    "iniciada_em": o.execucao.iniciada_em.isoformat(),
                },
            )
        ]

    def test_repetir_nao_emite_de_novo(self) -> None:
        o = _em_execucao()
        o.iniciar.executar(o.execucao.ordem_id, MECANICO)
        assert o.uow.eventos == []

    def test_sem_reserva_ativa_nao_cruza_o_pivot(self) -> None:
        o = _oficina(reservar=False)
        with pytest.raises(ViolacaoRegraDeNegocioException, match="reserva"):
            o.iniciar.executar(o.execucao.ordem_id, MECANICO)
        assert (o.uow.eventos, o.uow.rollbacks) == ([], 1)

    def test_ordem_desconhecida(self) -> None:
        o = _oficina()
        with pytest.raises(ExecucaoNaoEncontradaException):
            o.iniciar.executar(uuid4(), MECANICO)

    def test_reserva_liberada_tambem_barra_o_inicio(self) -> None:
        o = _oficina()
        o.reservas.reservas[o.execucao.ordem_id].liberar(datetime.now(UTC))
        with pytest.raises(ViolacaoRegraDeNegocioException):
            o.iniciar.executar(o.execucao.ordem_id, MECANICO)


class TestFinalizar:
    def test_consome_a_reserva_e_emite_execucao_finalizada(self) -> None:
        o = _em_execucao()
        o.finalizar.executar(o.execucao.ordem_id, MECANICO)

        vela = o.itens.itens[VELA]
        assert (vela.quantidade_disponivel, vela.quantidade_reservada) == (6, 0)
        assert o.execucao.finalizada_em is not None
        assert _eventos(o.uow) == [
            (
                "ExecucaoFinalizada",
                {
                    "ordem_id": str(o.execucao.ordem_id),
                    "finalizada_em": o.execucao.finalizada_em.isoformat(),
                    "pecas_consumidas": [{"sku": "PEC-VELA", "quantidade": 4}],
                },
            )
        ]

    def test_repetir_nao_baixa_de_novo(self) -> None:
        o = _em_execucao()
        o.finalizar.executar(o.execucao.ordem_id, MECANICO)
        o.finalizar.executar(o.execucao.ordem_id, MECANICO)
        assert o.estoque.baixas == 1
        assert o.itens.itens[VELA].quantidade_disponivel == 6
        assert len(o.uow.eventos) == 1

    def test_reserva_some_depois_do_inicio_e_409(self) -> None:
        o = _em_execucao(reservar=False)
        with pytest.raises(ViolacaoRegraDeNegocioException, match="reserva"):
            o.finalizar.executar(o.execucao.ordem_id, MECANICO)
        assert (o.uow.eventos, o.uow.rollbacks) == ([], 1)

    def test_reserva_liberada_bloqueia_a_finalizacao(self) -> None:
        o = _em_execucao()
        reserva = o.reservas.reservas[o.execucao.ordem_id]
        reserva.liberar(datetime.now(UTC))
        with pytest.raises(TransicaoStatusInvalidaException):
            o.finalizar.executar(o.execucao.ordem_id, MECANICO)
        assert reserva.status is StatusReserva.LIBERADA
        assert o.uow.eventos == []

    def test_outro_mecanico_nao_finaliza_e_nada_e_baixado(self) -> None:
        o = _em_execucao()
        with pytest.raises(OperacaoNaoPermitidaException):
            o.finalizar.executar(o.execucao.ordem_id, uuid4())
        assert o.estoque.baixas == 0
        assert o.itens.itens[VELA].quantidade_reservada == 4

    def test_admin_finaliza_em_nome_do_responsavel(self) -> None:
        o = _em_execucao()
        o.finalizar.executar(o.execucao.ordem_id, ADMIN, pelo_admin=True)
        o.finalizar.executar(o.execucao.ordem_id, ADMIN, pelo_admin=True)
        assert o.execucao.status is StatusExecucao.FINALIZADA
        assert o.execucao.mecanico_id == MECANICO
        assert [e.tipo for e in o.uow.eventos] == ["ExecucaoFinalizada"]

    def test_admin_nao_finaliza_o_que_nao_comecou(self) -> None:
        o = _oficina()
        with pytest.raises(TransicaoStatusInvalidaException):
            o.finalizar.executar(o.execucao.ordem_id, ADMIN, pelo_admin=True)
