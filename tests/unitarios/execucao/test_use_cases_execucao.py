from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import structlog
from structlog.testing import capture_logs

import src.compartilhado.aplicacao.responsavel as responsavel
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
from src.execucao.aplicacao.ports import DiagnosticoAnonimizado, ItemDaFila
from src.execucao.aplicacao.use_cases import (
    AgendarExecucao,
    AnonimizarVeiculo,
    CancelarExecucao,
    FinalizarExecucao,
    IniciarExecucao,
    ListarFila,
)
from src.execucao.dominio.exceptions import ExecucaoNaoEncontradaException
from src.execucao.dominio.execucao import Execucao, Prioridade, StatusExecucao
from tests.fakes import (
    EstoqueEmMemoria,
    ExecucoesEmMemoria,
    FakeTransacaoDoComando,
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
        prioridade=Prioridade.NORMAL,
        veiculo=None,
        agora=datetime.now(UTC),
        agendamento_id=uuid4(),
    )


def _estoque_com_reserva(*ordens: UUID) -> EstoqueEmMemoria:
    """Estoque em memoria com uma reserva ATIVA (sem pecas) para cada ordem."""
    itens, reservas = ItensEmMemoria(), ReservasEmMemoria()
    for ordem_id in ordens:
        ReservarPecas(itens, reservas, FakeTransacaoDoComando()).executar(ordem_id, [])
    return EstoqueEmMemoria(itens, reservas)


def _eventos(
    uow: FakeUnitOfWork | FakeTransacaoDoComando,
) -> list[tuple[str, dict[str, object]]]:
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
        ReservarPecas(itens, reservas, FakeTransacaoDoComando()).executar(
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
        repo, uow = ExecucoesEmMemoria(), FakeTransacaoDoComando()
        ordem_id = uuid4()
        veiculos = VeiculosEmMemoria({ordem_id: VEICULO})
        execucao = AgendarExecucao(
            repo, FilaFixa(posicao=3), veiculos, _estoque_com_reserva(ordem_id), uow
        ).executar(ordem_id, Prioridade.ALTA, agendamento_id=uuid4())

        assert repo.execucoes[ordem_id] is execucao
        assert (execucao.status, execucao.prioridade) == (
            StatusExecucao.AGUARDANDO,
            Prioridade.ALTA,
        )
        assert execucao.veiculo == VEICULO
        assert _eventos(uow) == [
            ("ExecucaoAgendada", {"ordem_id": str(ordem_id), "posicao_na_fila": 3})
        ]

    def test_reenvio_com_execucao_na_fila_reemite_e_mantem_prioridade(self) -> None:
        existente = _agendada()
        uow = FakeTransacaoDoComando()
        resultado = AgendarExecucao(
            ExecucoesEmMemoria(existente),
            FilaFixa(posicao=2),
            VeiculosEmMemoria(),
            _estoque_com_reserva(existente.ordem_id),
            uow,
        ).executar(existente.ordem_id, Prioridade.ALTA, agendamento_id=uuid4())
        assert resultado is existente
        assert resultado.prioridade is Prioridade.NORMAL
        assert _eventos(uow) == [
            (
                "ExecucaoAgendada",
                {"ordem_id": str(existente.ordem_id), "posicao_na_fila": 2},
            )
        ]

    def test_reenvio_atrasado_apos_inicio_e_ignorado(self) -> None:
        existente = _agendada()
        existente.iniciar(MECANICO, datetime.now(UTC))
        uow = FakeTransacaoDoComando()
        AgendarExecucao(
            ExecucoesEmMemoria(existente),
            FilaFixa(),
            VeiculosEmMemoria(),
            _estoque_com_reserva(existente.ordem_id),
            uow,
        ).executar(existente.ordem_id, Prioridade.NORMAL, agendamento_id=uuid4())
        assert (uow.eventos, uow.descartado) == ([], True)

    def test_agendamento_atrasado_encontra_a_lapide_e_e_descartado(self) -> None:
        repo = ExecucoesEmMemoria()
        ordem_id = uuid4()
        CancelarExecucao(repo, FakeTransacaoDoComando()).executar(ordem_id)
        lapide = repo.execucoes[ordem_id]
        uow = FakeTransacaoDoComando()

        resultado = AgendarExecucao(
            repo, FilaFixa(), VeiculosEmMemoria(), _estoque_com_reserva(ordem_id), uow
        ).executar(ordem_id, Prioridade.NORMAL, agendamento_id=uuid4())

        assert resultado is lapide
        assert resultado.status is StatusExecucao.CANCELADA
        assert (uow.eventos, uow.descartado) == ([], True)

    def test_prioridade_fora_do_contrato(self) -> None:
        ordem_id, urgente = uuid4(), "urgente"
        uc = AgendarExecucao(
            ExecucoesEmMemoria(),
            FilaFixa(),
            VeiculosEmMemoria(),
            _estoque_com_reserva(ordem_id),
            FakeTransacaoDoComando(),
        )
        with pytest.raises(ValueError, match="Prioridade"):
            uc.executar(ordem_id, urgente, agendamento_id=uuid4())

    @pytest.mark.parametrize(
        "reserva",
        [
            pytest.param("nenhuma", id="sem-reserva"),
            pytest.param("liberada", id="reserva-liberada"),
            pytest.param("recusada", id="reserva-recusada"),
        ],
    )
    def test_sem_reserva_ativa_nao_entra_na_fila(self, reserva: str) -> None:
        # RN-027: so entra na fila a ordem com as pecas reservadas.
        ordem_id = uuid4()
        itens = ItensEmMemoria(
            ItemEstoque.criar(sku=VELA, nome="Vela", quantidade_disponivel=1)
        )
        reservas = ReservasEmMemoria()
        if reserva != "nenhuma":
            quantidade = 9 if reserva == "recusada" else 1
            ReservarPecas(itens, reservas, FakeTransacaoDoComando()).executar(
                ordem_id, [ItemReserva(VELA, quantidade)]
            )
        if reserva == "liberada":
            reservas.reservas[ordem_id].liberar(datetime.now(UTC))
        repo, uow = ExecucoesEmMemoria(), FakeTransacaoDoComando()
        uc = AgendarExecucao(
            repo,
            FilaFixa(),
            VeiculosEmMemoria(),
            EstoqueEmMemoria(itens, reservas),
            uow,
        )

        with pytest.raises(ViolacaoRegraDeNegocioException, match="reserva"):
            uc.executar(ordem_id, Prioridade.NORMAL, agendamento_id=uuid4())

        assert repo.execucoes == {}
        assert (uow.eventos, uow.desfeitas) == ([], 1)


class TestListarFila:
    def test_pagina_da_fila_e_o_tamanho_dela(self) -> None:
        agora = datetime.now(UTC)
        itens = [
            ItemDaFila(
                posicao=posicao,
                ordem_id=uuid4(),
                prioridade=Prioridade.NORMAL,
                enfileirada_em=agora,
                veiculo=None,
            )
            for posicao in (1, 2, 3)
        ]
        pagina, total = ListarFila(FilaFixa(itens=itens)).executar(offset=1, limit=1)
        assert (pagina, total) == ([itens[1]], 3)


class TestCancelar:
    def test_cancela_e_responde(self) -> None:
        execucao = _agendada()
        uow = FakeTransacaoDoComando()
        CancelarExecucao(ExecucoesEmMemoria(execucao), uow).executar(execucao.ordem_id)
        assert execucao.status is StatusExecucao.CANCELADA
        assert _eventos(uow) == [
            ("ExecucaoCancelada", {"ordem_id": str(execucao.ordem_id)})
        ]

    def test_repetido_reemite_a_resposta(self) -> None:
        execucao = _agendada()
        repo, uow = ExecucoesEmMemoria(execucao), FakeTransacaoDoComando()
        CancelarExecucao(repo, uow).executar(execucao.ordem_id)
        CancelarExecucao(repo, uow).executar(execucao.ordem_id)
        assert [e.tipo for e in uow.eventos] == 2 * ["ExecucaoCancelada"]

    def test_depois_do_pivot_nao_cancela(self) -> None:
        execucao = _agendada()
        execucao.iniciar(MECANICO, datetime.now(UTC))
        uow = FakeTransacaoDoComando()
        uc = CancelarExecucao(ExecucoesEmMemoria(execucao), uow)
        with pytest.raises(TransicaoStatusInvalidaException):
            uc.executar(execucao.ordem_id)
        assert uow.eventos == []

    def test_compensacao_antes_do_original_grava_lapide_e_responde(self) -> None:
        repo, uow = ExecucoesEmMemoria(), FakeTransacaoDoComando()
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
        # A guarda vem antes da mudanca: o agregado nem chega a EM_EXECUCAO.
        assert o.execucao.status is StatusExecucao.AGUARDANDO
        assert o.execucao.mecanico_id is None

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
        assert o.execucao.status is StatusExecucao.EM_EXECUCAO

    def test_reserva_liberada_bloqueia_a_finalizacao(self) -> None:
        o = _em_execucao()
        reserva = o.reservas.reservas[o.execucao.ordem_id]
        reserva.liberar(datetime.now(UTC))
        with pytest.raises(TransicaoStatusInvalidaException):
            o.finalizar.executar(o.execucao.ordem_id, MECANICO)
        assert reserva.status is StatusReserva.LIBERADA
        assert o.uow.eventos == []
        # A baixa vem antes de mudar o agregado: a execucao segue EM_EXECUCAO.
        assert o.execucao.status is StatusExecucao.EM_EXECUCAO
        assert o.execucao.finalizada_em is None

    def test_outro_mecanico_nao_finaliza_e_nada_e_baixado(self) -> None:
        o = _em_execucao()
        with pytest.raises(OperacaoNaoPermitidaException):
            o.finalizar.executar(o.execucao.ordem_id, uuid4())
        assert o.estoque.baixas == 0
        assert o.itens.itens[VELA].quantidade_reservada == 4

    def test_admin_finaliza_em_nome_do_responsavel_com_auditoria(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(responsavel, "_log", structlog.get_logger("teste"))
        o = _em_execucao()
        with capture_logs() as logs:
            o.finalizar.executar(o.execucao.ordem_id, ADMIN, pelo_admin=True)
            o.finalizar.executar(o.execucao.ordem_id, ADMIN, pelo_admin=True)
        assert o.execucao.status is StatusExecucao.FINALIZADA
        assert o.execucao.mecanico_id == MECANICO
        assert [e.tipo for e in o.uow.eventos] == ["ExecucaoFinalizada"]
        auditoria = [log for log in logs if log["event"] == "audit"]
        assert [
            (log["acao"], log["ator_id"], log["mecanico_id"]) for log in auditoria
        ] == [("finalizar_execucao", str(ADMIN), str(MECANICO))]

    def test_admin_nao_finaliza_o_que_nao_comecou(self) -> None:
        o = _oficina()
        with pytest.raises(TransicaoStatusInvalidaException):
            o.finalizar.executar(o.execucao.ordem_id, ADMIN, pelo_admin=True)


class TestCausaDosFatosDoMecanico:
    """Os fatos do mecanico respondem ao AgendarExecucao que abriu a execucao."""

    def test_agendamento_guarda_o_id_do_comando_e_o_reenvio_nao_troca(self) -> None:
        ordem_id, comando, reenvio = uuid4(), uuid4(), uuid4()
        repo, uow = ExecucoesEmMemoria(), FakeTransacaoDoComando()
        caso = AgendarExecucao(
            repo, FilaFixa(), VeiculosEmMemoria(), _estoque_com_reserva(ordem_id), uow
        )
        caso.executar(ordem_id, Prioridade.NORMAL, agendamento_id=comando)
        caso.executar(ordem_id, Prioridade.ALTA, agendamento_id=reenvio)
        assert repo.execucoes[ordem_id].agendamento_id == comando
        # A resposta republicada fica sem causa propria: a outbox usa a do reenvio.
        assert [e.causation_id for e in uow.eventos] == [None, None]

    def test_inicio_e_finalizacao_levam_o_id_do_agendamento(self) -> None:
        ordem_id, comando = uuid4(), uuid4()
        execucao = Execucao.agendar(
            ordem_id=ordem_id,
            prioridade=Prioridade.NORMAL,
            veiculo=None,
            agora=datetime.now(UTC),
            agendamento_id=comando,
        )
        repo, estoque = ExecucoesEmMemoria(execucao), _estoque_com_reserva(ordem_id)
        uow = FakeUnitOfWork()
        IniciarExecucao(repo, estoque, uow).executar(ordem_id, MECANICO)
        FinalizarExecucao(repo, estoque, uow).executar(ordem_id, MECANICO)
        assert [(e.tipo, e.causation_id) for e in uow.eventos] == [
            ("ExecucaoIniciada", comando),
            ("ExecucaoFinalizada", comando),
        ]


class _DiagnosticosDoVeiculo:
    """Port do contexto vizinho: devolve os anonimizados e registra os pedidos."""

    def __init__(self, *anonimizados: DiagnosticoAnonimizado) -> None:
        self._anonimizados = list(anonimizados)
        self.pedidos: list[UUID] = []

    def anonimizar(self, veiculo_id: UUID) -> list[DiagnosticoAnonimizado]:
        self.pedidos.append(veiculo_id)
        anonimizados, self._anonimizados = self._anonimizados, []
        return anonimizados


class _MensagensGuardadas:
    def __init__(self) -> None:
        self.ordens: list[list[UUID]] = []

    def anonimizar(self, ordens: Sequence[UUID]) -> int:
        self.ordens.append(list(ordens))
        return len(ordens)


def _com_retrato(veiculo: Veiculo, status: str = "cancelada") -> Execucao:
    execucao = Execucao.agendar(
        ordem_id=uuid4(),
        prioridade=Prioridade.NORMAL,
        veiculo=veiculo,
        agora=datetime.now(UTC),
        agendamento_id=uuid4(),
    )
    if status == "cancelada":
        execucao.cancelar(datetime.now(UTC))
    return execucao


class TestAnonimizarVeiculo:
    def test_troca_as_copias_do_veiculo_e_os_textos_das_mensagens(self) -> None:
        outro = Veiculo(
            veiculo_id=uuid4(), placa="RIO2A18", marca="VW", modelo="Gol", ano=2019
        )
        alvo, vizinha = _com_retrato(VEICULO), _com_retrato(outro)
        execucoes = ExecucoesEmMemoria(alvo, vizinha)
        diagnostico = DiagnosticoAnonimizado(ordem_id=uuid4(), em_andamento=False)
        diagnosticos, mensagens = (
            _DiagnosticosDoVeiculo(diagnostico),
            _MensagensGuardadas(),
        )
        uow = FakeTransacaoDoComando()

        trocados = AnonimizarVeiculo(execucoes, diagnosticos, mensagens, uow).executar(
            VEICULO.veiculo_id
        )

        assert trocados == 2
        assert alvo.veiculo is not None
        assert alvo.veiculo.placa == f"ANONIMIZADO:{VEICULO.veiculo_id}"
        assert vizinha.veiculo == outro
        assert execucoes.salvas == [alvo.ordem_id]
        assert diagnosticos.pedidos == [VEICULO.veiculo_id]
        assert mensagens.ordens == [[diagnostico.ordem_id]]
        assert (uow.eventos, uow.descartado) == ([], False)

    def test_repetido_nao_muda_nada_e_descarta_o_comando(self) -> None:
        execucoes = ExecucoesEmMemoria(_com_retrato(VEICULO))
        uow = FakeTransacaoDoComando()
        caso = AnonimizarVeiculo(
            execucoes, _DiagnosticosDoVeiculo(), _MensagensGuardadas(), uow
        )
        assert caso.executar(VEICULO.veiculo_id) == 1
        assert caso.executar(VEICULO.veiculo_id) == 0
        assert uow.descartado
        assert len(execucoes.salvas) == 1

    def test_registro_ainda_em_andamento_e_anonimizado_com_aviso(self) -> None:
        # O OS so elimina cliente sem OS ativa: se a premissa falhar, a placa
        # sai do mesmo jeito, e o log diz quais ordens estavam em andamento.
        na_fila = _com_retrato(VEICULO, status="aguardando")
        diagnostico = DiagnosticoAnonimizado(ordem_id=uuid4(), em_andamento=True)
        with capture_logs() as logs:
            AnonimizarVeiculo(
                ExecucoesEmMemoria(na_fila),
                _DiagnosticosDoVeiculo(diagnostico),
                _MensagensGuardadas(),
                FakeTransacaoDoComando(),
            ).executar(VEICULO.veiculo_id)
        (aviso,) = [
            log
            for log in logs
            if log["event"] == "vehicle_anonymized_while_in_progress"
        ]
        assert aviso["log_level"] == "warning"
        assert set(aviso["ordens"]) == {
            str(diagnostico.ordem_id),
            str(na_fila.ordem_id),
        }
