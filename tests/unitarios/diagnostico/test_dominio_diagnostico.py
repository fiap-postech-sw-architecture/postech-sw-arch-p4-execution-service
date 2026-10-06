from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.compartilhado.dominio.exceptions import (
    OperacaoNaoPermitidaException,
    TransicaoStatusInvalidaException,
)
from src.compartilhado.dominio.veiculo import Veiculo
from src.diagnostico.dominio.diagnostico import (
    Diagnostico,
    ItemDiagnostico,
    StatusDiagnostico,
    TipoItem,
)

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
MECANICO, OUTRO = uuid4(), uuid4()
ITENS = [
    ItemDiagnostico(tipo=TipoItem.SERVICO, codigo="SRV-TROCA-PASTILHA", quantidade=1),
    ItemDiagnostico(tipo=TipoItem.PECA, codigo="PEC-PASTILHA-FREIO", quantidade=1),
]


def _veiculo(placa: str = "ABC1D23") -> Veiculo:
    return Veiculo(
        veiculo_id=uuid4(), placa=placa, marca="Fiat", modelo="Uno", ano=2015
    )


def _diagnostico() -> Diagnostico:
    return Diagnostico.solicitar(
        ordem_id=uuid4(),
        veiculo=_veiculo(),
        descricao_problema=" Barulho ao frear ",
        agora=AGORA,
    )


def _em_andamento() -> Diagnostico:
    diagnostico = _diagnostico()
    diagnostico.iniciar(MECANICO, AGORA)
    return diagnostico


class TestItemDiagnostico:
    def test_tipo_precisa_ser_o_enum(self) -> None:
        with pytest.raises(ValueError, match="servico"):
            ItemDiagnostico(tipo="peca", codigo="PEC-X", quantidade=1)  # type: ignore[arg-type]

    @pytest.mark.parametrize("codigo", ["", "pec-x", "PEC X", "PEC-X\n", "P" * 51])
    def test_codigo_invalido(self, codigo: str) -> None:
        with pytest.raises(ValueError, match="Codigo"):
            ItemDiagnostico(tipo=TipoItem.PECA, codigo=codigo, quantidade=1)

    def test_codigo_no_limite_do_billing(self) -> None:
        item = ItemDiagnostico(tipo=TipoItem.PECA, codigo="P" * 50, quantidade=1)
        assert len(item.codigo) == 50

    def test_quantidade_positiva(self) -> None:
        with pytest.raises(ValueError, match="positiva"):
            ItemDiagnostico(tipo=TipoItem.PECA, codigo="PEC-X", quantidade=0)


class TestDiagnostico:
    def test_solicitacao_valida_e_normaliza_o_retrato(self) -> None:
        diagnostico = Diagnostico.solicitar(
            ordem_id=uuid4(),
            veiculo=_veiculo(placa="abc-1234"),
            descricao_problema="x",
            agora=AGORA,
        )
        assert diagnostico.veiculo is not None
        assert diagnostico.veiculo.placa == "ABC1234"
        ordem_id, invalido = uuid4(), _veiculo(placa="ABC")
        with pytest.raises(ValueError, match="Placa invalida"):
            Diagnostico.solicitar(
                ordem_id=ordem_id, veiculo=invalido, descricao_problema="x", agora=AGORA
            )

    def test_descricao_com_caractere_de_controle_e_recusada(self) -> None:
        ordem_id, veiculo = uuid4(), _veiculo()
        with pytest.raises(ValueError, match="controle"):
            Diagnostico.solicitar(
                ordem_id=ordem_id,
                veiculo=veiculo,
                descricao_problema="freio\x00chiando",
                agora=AGORA,
            )

    def test_descricao_multilinha_e_aceita(self) -> None:
        diagnostico = Diagnostico.solicitar(
            ordem_id=uuid4(),
            veiculo=_veiculo(),
            descricao_problema="freio chiando\r\nao frear",
            agora=AGORA,
        )
        assert diagnostico.descricao_problema == "freio chiando\r\nao frear"

    def test_solicitacao_entra_aguardando_com_ordem_como_identidade(self) -> None:
        diagnostico = _diagnostico()
        assert diagnostico.status is StatusDiagnostico.AGUARDANDO
        assert diagnostico.ordem_id == diagnostico.id
        assert diagnostico.descricao_problema == "Barulho ao frear"
        assert diagnostico.solicitado_em == AGORA
        assert (diagnostico.mecanico_id, diagnostico.itens) == (None, ())

    @pytest.mark.parametrize("descricao", ["", "   ", "x" * 2001])
    def test_descricao_obrigatoria(self, descricao: str) -> None:
        ordem_id, veiculo = uuid4(), _veiculo()
        with pytest.raises(ValueError, match="Descricao"):
            Diagnostico.solicitar(
                ordem_id=ordem_id,
                veiculo=veiculo,
                descricao_problema=descricao,
                agora=AGORA,
            )

    def test_iniciar_registra_mecanico(self) -> None:
        diagnostico = _diagnostico()
        assert diagnostico.iniciar(MECANICO, AGORA) is True
        assert diagnostico.status is StatusDiagnostico.EM_ANDAMENTO
        assert (diagnostico.mecanico_id, diagnostico.iniciado_em) == (MECANICO, AGORA)

    def test_iniciar_de_novo_pelo_mesmo_mecanico_e_no_op(self) -> None:
        diagnostico = _em_andamento()
        assert diagnostico.iniciar(MECANICO, datetime.now(UTC)) is False
        assert diagnostico.iniciado_em == AGORA

    def test_iniciar_por_outro_mecanico_e_409(self) -> None:
        diagnostico = _em_andamento()
        with pytest.raises(TransicaoStatusInvalidaException, match="outro mecanico"):
            diagnostico.iniciar(OUTRO, AGORA)

    def test_concluir_registra_itens(self) -> None:
        diagnostico = _em_andamento()
        assert diagnostico.concluir(MECANICO, ITENS, " trocar ", AGORA) is True
        assert diagnostico.status is StatusDiagnostico.CONCLUIDO
        assert diagnostico.itens == tuple(ITENS)
        assert (diagnostico.observacoes, diagnostico.concluido_em) == ("trocar", AGORA)
        assert diagnostico.concluido_por(MECANICO)
        assert not diagnostico.concluido_por(OUTRO)

    def test_concluir_de_novo_pelo_mesmo_mecanico_e_no_op(self) -> None:
        diagnostico = _em_andamento()
        diagnostico.concluir(MECANICO, ITENS, "", AGORA)
        assert (
            diagnostico.concluir(MECANICO, ITENS[:1], "outra", datetime.now(UTC))
            is False
        )
        assert diagnostico.itens == tuple(ITENS)

    def test_concluir_sem_iniciar_e_409(self) -> None:
        diagnostico = _diagnostico()
        with pytest.raises(TransicaoStatusInvalidaException, match="AGUARDANDO"):
            diagnostico.concluir(MECANICO, ITENS, "", AGORA)

    def test_so_o_responsavel_conclui(self) -> None:
        diagnostico = _em_andamento()
        with pytest.raises(OperacaoNaoPermitidaException):
            diagnostico.concluir(OUTRO, ITENS, "", AGORA)
        assert diagnostico.status is StatusDiagnostico.EM_ANDAMENTO

    def test_conclusao_sem_itens(self) -> None:
        diagnostico = _em_andamento()
        with pytest.raises(ValueError, match="ao menos um"):
            diagnostico.validar_conclusao(MECANICO, [], "")

    def test_item_repetido(self) -> None:
        diagnostico = _em_andamento()
        with pytest.raises(ValueError, match="unica vez"):
            diagnostico.validar_conclusao(MECANICO, [ITENS[0], ITENS[0]], "")

    def test_observacoes_longas_nao_mudam_nada(self) -> None:
        diagnostico = _em_andamento()
        with pytest.raises(ValueError, match="Observacoes"):
            diagnostico.concluir(MECANICO, ITENS, "x" * 2001, AGORA)
        assert diagnostico.status is StatusDiagnostico.EM_ANDAMENTO

    @pytest.mark.parametrize("estado", ["aguardando", "em_andamento", "concluido"])
    def test_descarte_de_qualquer_estado_nao_final(self, estado: str) -> None:
        diagnostico = _diagnostico()
        if estado != "aguardando":
            diagnostico.iniciar(MECANICO, AGORA)
        if estado == "concluido":
            diagnostico.concluir(MECANICO, ITENS, "", AGORA)
        diagnostico.descartar(AGORA)
        assert diagnostico.status is StatusDiagnostico.DESCARTADO
        assert diagnostico.descartado_em == AGORA

    def test_lapide_nasce_descartada_sem_retrato(self) -> None:
        ordem_id = uuid4()
        lapide = Diagnostico.lapide(ordem_id=ordem_id, agora=AGORA)
        assert (lapide.ordem_id, lapide.status) == (
            ordem_id,
            StatusDiagnostico.DESCARTADO,
        )
        assert (lapide.veiculo, lapide.descricao_problema) == (None, None)
        assert (lapide.solicitado_em, lapide.descartado_em) == (AGORA, AGORA)

    def test_so_a_lapide_fica_sem_veiculo(self) -> None:
        ordem_id = uuid4()
        with pytest.raises(ValueError, match="lapide"):
            Diagnostico(
                id=ordem_id,
                _veiculo=None,
                _descricao_problema="x",
                _solicitado_em=AGORA,
            )

    def test_descarte_idempotente_e_final(self) -> None:
        diagnostico = _diagnostico()
        diagnostico.descartar(AGORA)
        diagnostico.descartar(datetime.now(UTC))
        assert diagnostico.descartado_em == AGORA
        with pytest.raises(TransicaoStatusInvalidaException, match="estado final"):
            diagnostico.iniciar(MECANICO, AGORA)

    def test_repr_nao_leva_texto_livre_nem_placa(self) -> None:
        # O repr aparece em traceback e log: descricao e observacoes podem
        # trazer nome ou placa do cliente.
        diagnostico = Diagnostico.solicitar(
            ordem_id=uuid4(),
            veiculo=_veiculo(placa="ABC1D23"),
            descricao_problema="cliente Joao reclamou",
            agora=AGORA,
        )
        diagnostico.iniciar(MECANICO, AGORA)
        diagnostico.concluir(MECANICO, ITENS, "falar com Maria", AGORA)
        texto = repr(diagnostico)
        assert "Joao" not in texto
        assert "Maria" not in texto
        assert "ABC1D23" not in texto
