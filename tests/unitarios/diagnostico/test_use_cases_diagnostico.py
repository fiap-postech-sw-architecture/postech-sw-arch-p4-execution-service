from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import structlog
from structlog.testing import capture_logs

import src.compartilhado.aplicacao.responsavel as responsavel
from src.compartilhado.aplicacao.outbox import dados_do_evento
from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelException,
    OperacaoNaoPermitidaException,
    TransicaoStatusInvalidaException,
)
from src.compartilhado.dominio.veiculo import Veiculo
from src.diagnostico.aplicacao.use_cases import (
    ConcluirDiagnostico,
    DescartarDiagnostico,
    IniciarDiagnostico,
    ListarDiagnosticos,
    RegistrarSolicitacaoDeDiagnostico,
)
from src.diagnostico.dominio.diagnostico import (
    Diagnostico,
    ItemDiagnostico,
    StatusDiagnostico,
    TipoItem,
)
from src.diagnostico.dominio.exceptions import (
    DiagnosticoNaoEncontradoException,
    ItensInvalidosException,
)
from tests.fakes import (
    CatalogoFake,
    DiagnosticosEmMemoria,
    FakeUnitOfWork,
    ValidadorFake,
)

MECANICO = uuid4()
VEICULO = Veiculo(
    veiculo_id=uuid4(), placa="ABC1D23", marca="Fiat", modelo="Uno", ano=2015
)
SERVICO = ItemDiagnostico(tipo=TipoItem.SERVICO, codigo="SRV-TROCA-OLEO", quantidade=1)
OLEO = ItemDiagnostico(tipo=TipoItem.PECA, codigo="PEC-OLEO-5W30", quantidade=4)


def _diagnostico(ordem_id: UUID | None = None, *, atraso: int = 0) -> Diagnostico:
    return Diagnostico.solicitar(
        ordem_id=ordem_id or uuid4(),
        veiculo=VEICULO,
        descricao_problema="Revisao",
        agora=datetime.now(UTC) + timedelta(seconds=atraso),
    )


def _em_andamento() -> Diagnostico:
    diagnostico = _diagnostico()
    diagnostico.iniciar(MECANICO, datetime.now(UTC))
    return diagnostico


class TestRegistrarSolicitacao:
    def test_poe_na_fila_sem_evento(self) -> None:
        repo, uow = DiagnosticosEmMemoria(), FakeUnitOfWork()
        ordem_id = uuid4()
        diagnostico = RegistrarSolicitacaoDeDiagnostico(repo, uow).executar(
            ordem_id, VEICULO, "Barulho no motor"
        )
        assert repo.diagnosticos[ordem_id] is diagnostico
        assert diagnostico.status is StatusDiagnostico.AGUARDANDO
        assert (uow.commits, uow.eventos) == (1, [])

    def test_reenvio_do_comando_devolve_o_existente(self) -> None:
        existente = _em_andamento()
        repo, uow = DiagnosticosEmMemoria(existente), FakeUnitOfWork()
        resultado = RegistrarSolicitacaoDeDiagnostico(repo, uow).executar(
            existente.ordem_id, VEICULO, "outra descricao"
        )
        assert resultado is existente
        assert resultado.status is StatusDiagnostico.EM_ANDAMENTO
        assert uow.commits == 0

    def test_solicitacao_atrasada_encontra_a_lapide_e_e_descartada(self) -> None:
        repo = DiagnosticosEmMemoria()
        ordem_id = uuid4()
        DescartarDiagnostico(repo, FakeUnitOfWork()).executar(ordem_id)
        lapide = repo.diagnosticos[ordem_id]
        uow = FakeUnitOfWork()

        resultado = RegistrarSolicitacaoDeDiagnostico(repo, uow).executar(
            ordem_id, VEICULO, "Barulho no motor"
        )

        assert resultado is lapide
        assert (resultado.status, resultado.veiculo) == (
            StatusDiagnostico.DESCARTADO,
            None,
        )
        assert (uow.eventos, uow.commits) == ([], 0)


def test_listar_filtra_por_status_em_ordem_de_chegada() -> None:
    primeiro, segundo, andamento = (
        _diagnostico(),
        _diagnostico(atraso=1),
        _em_andamento(),
    )
    repo = DiagnosticosEmMemoria(segundo, andamento, primeiro)
    diagnosticos, total = ListarDiagnosticos(repo).executar(
        StatusDiagnostico.AGUARDANDO, offset=0, limit=10
    )
    assert (diagnosticos, total) == ([primeiro, segundo], 2)


class TestIniciar:
    def test_emite_diagnostico_iniciado(self) -> None:
        diagnostico = _diagnostico()
        uow = FakeUnitOfWork()
        IniciarDiagnostico(DiagnosticosEmMemoria(diagnostico), uow).executar(
            diagnostico.ordem_id, MECANICO
        )
        assert diagnostico.iniciado_em is not None
        assert [(e.tipo, dados_do_evento(e)) for e in uow.eventos] == [
            (
                "DiagnosticoIniciado",
                {
                    "ordem_id": str(diagnostico.ordem_id),
                    "mecanico_id": str(MECANICO),
                    "iniciado_em": diagnostico.iniciado_em.isoformat(),
                },
            )
        ]
        assert uow.eventos[0].ocorrido_em == diagnostico.iniciado_em

    def test_repetir_nao_emite_de_novo(self) -> None:
        diagnostico = _em_andamento()
        uow = FakeUnitOfWork()
        IniciarDiagnostico(DiagnosticosEmMemoria(diagnostico), uow).executar(
            diagnostico.ordem_id, MECANICO
        )
        assert (uow.eventos, uow.commits) == ([], 0)

    def test_ordem_desconhecida(self) -> None:
        uc, ordem_id = (
            IniciarDiagnostico(DiagnosticosEmMemoria(), FakeUnitOfWork()),
            uuid4(),
        )
        with pytest.raises(DiagnosticoNaoEncontradoException):
            uc.executar(ordem_id, MECANICO)


class TestConcluir:
    def _uc(
        self,
        diagnostico: Diagnostico,
        catalogo: CatalogoFake | None = None,
        validador: ValidadorFake | None = None,
    ) -> tuple[ConcluirDiagnostico, FakeUnitOfWork, CatalogoFake, ValidadorFake]:
        catalogo = catalogo or CatalogoFake()
        validador = validador or ValidadorFake()
        uow = FakeUnitOfWork()
        uc = ConcluirDiagnostico(
            DiagnosticosEmMemoria(diagnostico), catalogo, validador, uow
        )
        return uc, uow, catalogo, validador

    def test_valida_codigos_e_emite_diagnostico_concluido(self) -> None:
        diagnostico = _em_andamento()
        uc, uow, catalogo, validador = self._uc(diagnostico)

        uc.executar(diagnostico.ordem_id, MECANICO, [SERVICO, OLEO], " trocar oleo ")

        assert catalogo.chamadas == [["PEC-OLEO-5W30"]]
        assert validador.chamadas == [
            {"servicos": ["SRV-TROCA-OLEO"], "pecas": ["PEC-OLEO-5W30"]}
        ]
        assert diagnostico.concluido_em is not None
        assert [(e.tipo, dados_do_evento(e)) for e in uow.eventos] == [
            (
                "DiagnosticoConcluido",
                {
                    "ordem_id": str(diagnostico.ordem_id),
                    "itens": [
                        {
                            "tipo": "servico",
                            "codigo": "SRV-TROCA-OLEO",
                            "quantidade": 1,
                        },
                        {"tipo": "peca", "codigo": "PEC-OLEO-5W30", "quantidade": 4},
                    ],
                    "observacoes": "trocar oleo",
                    "concluido_em": diagnostico.concluido_em.isoformat(),
                },
            )
        ]

    def test_so_servicos_nao_consulta_o_estoque(self) -> None:
        diagnostico = _em_andamento()
        uc, _, catalogo, validador = self._uc(diagnostico)
        uc.executar(diagnostico.ordem_id, MECANICO, [SERVICO], "")
        assert catalogo.chamadas == []
        assert validador.chamadas == [{"servicos": ["SRV-TROCA-OLEO"], "pecas": []}]

    def test_peca_sem_estoque_recusa_sem_chamar_o_billing(self) -> None:
        diagnostico = _em_andamento()
        uc, uow, _, validador = self._uc(
            diagnostico, catalogo=CatalogoFake(["PEC-OLEO-5W30"])
        )
        with pytest.raises(ItensInvalidosException, match="PEC-OLEO-5W30") as erro:
            uc.executar(diagnostico.ordem_id, MECANICO, [SERVICO, OLEO], "")
        assert erro.value.codigo == "ITENS_INVALIDOS"
        assert validador.chamadas == []
        assert diagnostico.status is StatusDiagnostico.EM_ANDAMENTO
        assert uow.eventos == []

    def test_codigo_sem_preco_no_billing(self) -> None:
        diagnostico = _em_andamento()
        uc, uow, _, _ = self._uc(
            diagnostico, validador=ValidadorFake(["SRV-TROCA-OLEO"])
        )
        with pytest.raises(
            ItensInvalidosException, match="tabela do Billing: SRV-TROCA-OLEO"
        ):
            uc.executar(diagnostico.ordem_id, MECANICO, [SERVICO], "")
        assert uow.eventos == []

    def test_billing_fora_do_ar_propaga_503(self) -> None:
        diagnostico = _em_andamento()
        uc, uow, _, _ = self._uc(
            diagnostico, validador=ValidadorFake(indisponivel=True)
        )
        with pytest.raises(DependenciaIndisponivelException):
            uc.executar(diagnostico.ordem_id, MECANICO, [SERVICO], "")
        assert diagnostico.status is StatusDiagnostico.EM_ANDAMENTO
        assert uow.eventos == []

    @pytest.mark.parametrize(
        ("iniciado_por_outro", "erro"),
        [
            pytest.param(
                False, TransicaoStatusInvalidaException, id="ainda-aguardando"
            ),
            pytest.param(True, OperacaoNaoPermitidaException, id="de-outro-mecanico"),
        ],
    )
    def test_estado_e_responsavel_conferidos_antes_da_rede(
        self, iniciado_por_outro: bool, erro: type[Exception]
    ) -> None:
        diagnostico = _diagnostico()
        if iniciado_por_outro:
            diagnostico.iniciar(uuid4(), datetime.now(UTC))
        uc, _, catalogo, validador = self._uc(diagnostico)
        with pytest.raises(erro):
            uc.executar(diagnostico.ordem_id, MECANICO, [SERVICO, OLEO], "")
        assert (catalogo.chamadas, validador.chamadas) == ([], [])

    def test_repetir_pelo_mesmo_mecanico_nao_revalida_nem_emite(self) -> None:
        diagnostico = _em_andamento()
        diagnostico.concluir(MECANICO, [SERVICO], "", datetime.now(UTC))
        uc, uow, catalogo, validador = self._uc(diagnostico)
        resultado = uc.executar(diagnostico.ordem_id, MECANICO, [OLEO], "outra")
        assert resultado.itens == (SERVICO,)
        assert (catalogo.chamadas, validador.chamadas, uow.eventos) == ([], [], [])

    def test_conclusao_concorrente_durante_a_validacao_nao_duplica_evento(
        self,
    ) -> None:
        # Le sem lock (EM_ANDAMENTO), valida no Billing e, ao reler sob lock, o
        # mesmo mecanico ja concluiu por outro request: devolve sem novo evento.
        diagnostico = _em_andamento()
        concluido = _em_andamento()
        concluido.concluir(MECANICO, [SERVICO], "", datetime.now(UTC))

        class _RepoQueMudaNoLock(DiagnosticosEmMemoria):
            def obter(self, ordem_id: UUID, *, com_lock: bool = False) -> Diagnostico:
                return concluido if com_lock else diagnostico

        uow = FakeUnitOfWork()
        uc = ConcluirDiagnostico(
            _RepoQueMudaNoLock(), CatalogoFake(), ValidadorFake(), uow
        )
        assert uc.executar(diagnostico.ordem_id, MECANICO, [SERVICO], "") is concluido
        assert (uow.eventos, uow.commits) == ([], 0)

    def test_admin_conclui_em_nome_do_responsavel_com_auditoria(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(responsavel, "_log", structlog.get_logger("teste"))
        diagnostico = _em_andamento()
        uc, uow, _, _ = self._uc(diagnostico)
        admin = uuid4()
        with capture_logs() as logs:
            uc.executar(diagnostico.ordem_id, admin, [SERVICO], "", pelo_admin=True)
            uc.executar(diagnostico.ordem_id, admin, [SERVICO], "", pelo_admin=True)
        assert diagnostico.status is StatusDiagnostico.CONCLUIDO
        assert diagnostico.mecanico_id == MECANICO
        assert [e.tipo for e in uow.eventos] == ["DiagnosticoConcluido"]
        auditoria = [log for log in logs if log["event"] == "audit"]
        assert [
            (log["acao"], log["ator_id"], log["mecanico_id"]) for log in auditoria
        ] == [("concluir_diagnostico", str(admin), str(MECANICO))]

    def test_ordem_desconhecida(self) -> None:
        uc = ConcluirDiagnostico(
            DiagnosticosEmMemoria(), CatalogoFake(), ValidadorFake(), FakeUnitOfWork()
        )
        with pytest.raises(DiagnosticoNaoEncontradoException):
            uc.executar(uuid4(), MECANICO, [SERVICO], "")


class TestDescartar:
    def test_descarta_e_responde(self) -> None:
        diagnostico = _em_andamento()
        uow = FakeUnitOfWork()
        DescartarDiagnostico(DiagnosticosEmMemoria(diagnostico), uow).executar(
            diagnostico.ordem_id
        )
        assert diagnostico.status is StatusDiagnostico.DESCARTADO
        assert [(e.tipo, dados_do_evento(e)) for e in uow.eventos] == [
            ("DiagnosticoDescartado", {"ordem_id": str(diagnostico.ordem_id)})
        ]

    def test_repetido_so_reemite_a_resposta(self) -> None:
        diagnostico = _diagnostico()
        repo, uow = DiagnosticosEmMemoria(diagnostico), FakeUnitOfWork()
        DescartarDiagnostico(repo, uow).executar(diagnostico.ordem_id)
        descartado_em = diagnostico.descartado_em
        DescartarDiagnostico(repo, uow).executar(diagnostico.ordem_id)
        assert diagnostico.descartado_em == descartado_em
        assert [e.tipo for e in uow.eventos] == 2 * ["DiagnosticoDescartado"]

    def test_compensacao_antes_do_original_grava_lapide_e_responde(self) -> None:
        repo, uow = DiagnosticosEmMemoria(), FakeUnitOfWork()
        ordem_id = uuid4()

        DescartarDiagnostico(repo, uow).executar(ordem_id)

        lapide = repo.diagnosticos[ordem_id]
        assert lapide.status is StatusDiagnostico.DESCARTADO
        assert (lapide.veiculo, lapide.descricao_problema) == (None, None)
        assert lapide.descartado_em is not None
        assert [(e.tipo, dados_do_evento(e)) for e in uow.eventos] == [
            ("DiagnosticoDescartado", {"ordem_id": str(ordem_id)})
        ]


class TestCausaDosFatosDoMecanico:
    """Os fatos do mecanico respondem ao SolicitarDiagnostico que abriu o fluxo."""

    def test_solicitacao_guarda_o_id_do_comando_e_o_reenvio_nao_troca(self) -> None:
        repo, ordem_id = DiagnosticosEmMemoria(), uuid4()
        comando, reenvio = uuid4(), uuid4()
        caso = RegistrarSolicitacaoDeDiagnostico(repo, FakeUnitOfWork())
        caso.executar(ordem_id, VEICULO, "Freio", solicitacao_id=comando)
        caso.executar(ordem_id, VEICULO, "Freio", solicitacao_id=reenvio)
        assert repo.diagnosticos[ordem_id].solicitacao_id == comando

    def test_inicio_e_conclusao_levam_o_id_da_solicitacao(self) -> None:
        comando = uuid4()
        diagnostico = Diagnostico.solicitar(
            ordem_id=uuid4(),
            veiculo=VEICULO,
            descricao_problema="Revisao",
            agora=datetime.now(UTC),
            solicitacao_id=comando,
        )
        repo, uow = DiagnosticosEmMemoria(diagnostico), FakeUnitOfWork()
        IniciarDiagnostico(repo, uow).executar(diagnostico.ordem_id, MECANICO)
        ConcluirDiagnostico(repo, CatalogoFake(), ValidadorFake(), uow).executar(
            diagnostico.ordem_id, MECANICO, [SERVICO], ""
        )
        assert [(e.tipo, e.causation_id) for e in uow.eventos] == [
            ("DiagnosticoIniciado", comando),
            ("DiagnosticoConcluido", comando),
        ]
