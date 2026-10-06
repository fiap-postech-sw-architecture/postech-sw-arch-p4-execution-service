from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.compartilhado.dominio.exceptions import (
    OperacaoNaoPermitidaException,
    TransicaoStatusInvalidaException,
)
from src.diagnostico.dominio.diagnostico import (
    Diagnostico,
    ItemDiagnostico,
    StatusDiagnostico,
    TipoItem,
)
from src.diagnostico.dominio.veiculo import Veiculo

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
MECANICO, OUTRO = uuid4(), uuid4()
ITENS = [
    ItemDiagnostico(tipo=TipoItem.SERVICO, codigo="SRV-TROCA-PASTILHA", quantidade=1),
    ItemDiagnostico(tipo=TipoItem.PECA, codigo="PEC-PASTILHA-FREIO", quantidade=1),
]


def _veiculo(**extras: object) -> Veiculo:
    dados: dict[str, object] = {
        "placa": "ABC1D23",
        "marca": "Fiat",
        "modelo": "Uno",
        "ano": 2015,
    }
    dados.update(extras)
    return Veiculo(**dados)  # type: ignore[arg-type]


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


class TestVeiculo:
    @pytest.mark.parametrize(
        ("placa", "normalizada"),
        [("abc-1234", "ABC1234"), ("ABC1D23", "ABC1D23"), (" abc1d23 ", "ABC1D23")],
    )
    def test_placa_antiga_e_mercosul_normalizadas(
        self, placa: str, normalizada: str
    ) -> None:
        assert _veiculo(placa=placa).placa == normalizada

    @pytest.mark.parametrize("placa", ["", "AB1234", "ABCD123", "ABC12D3", "1BC1234"])
    def test_placa_invalida_sem_ecoar_o_valor(self, placa: str) -> None:
        with pytest.raises(ValueError, match="Placa invalida") as erro:
            _veiculo(placa=placa)
        if placa:
            assert placa not in str(erro.value)

    @pytest.mark.parametrize("campo", ["marca", "modelo"])
    @pytest.mark.parametrize("valor", ["", "  ", "x" * 101])
    def test_marca_e_modelo_obrigatorios(self, campo: str, valor: str) -> None:
        with pytest.raises(ValueError, match="do veiculo"):
            _veiculo(**{campo: valor})

    def test_marca_e_modelo_aparados(self) -> None:
        veiculo = _veiculo(marca=" Fiat ", modelo=" Uno ")
        assert (veiculo.marca, veiculo.modelo) == ("Fiat", "Uno")

    @pytest.mark.parametrize("ano", [1886, datetime.now(UTC).year + 2])
    def test_ano_fora_da_faixa(self, ano: int) -> None:
        with pytest.raises(ValueError, match="Ano do veiculo"):
            _veiculo(ano=ano)

    def test_ano_modelo_seguinte_e_aceito(self) -> None:
        assert (
            _veiculo(ano=datetime.now(UTC).year + 1).ano == datetime.now(UTC).year + 1
        )

    def test_repr_mascara_a_placa(self) -> None:
        texto = repr(_veiculo(placa="ABC1D23"))
        assert "ABC1D23" not in texto
        assert "AB*****" in texto


class TestItemDiagnostico:
    def test_tipo_precisa_ser_o_enum(self) -> None:
        with pytest.raises(ValueError, match="servico"):
            ItemDiagnostico(tipo="peca", codigo="PEC-X", quantidade=1)  # type: ignore[arg-type]

    @pytest.mark.parametrize("codigo", ["", "pec-x", "PEC X", "PEC-X\n", "P" * 65])
    def test_codigo_invalido(self, codigo: str) -> None:
        with pytest.raises(ValueError, match="Codigo"):
            ItemDiagnostico(tipo=TipoItem.PECA, codigo=codigo, quantidade=1)

    def test_quantidade_positiva(self) -> None:
        with pytest.raises(ValueError, match="positiva"):
            ItemDiagnostico(tipo=TipoItem.PECA, codigo="PEC-X", quantidade=0)


class TestDiagnostico:
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
            diagnostico.validar_conclusao(MECANICO, [])

    def test_item_repetido(self) -> None:
        diagnostico = _em_andamento()
        with pytest.raises(ValueError, match="unica vez"):
            diagnostico.validar_conclusao(MECANICO, [ITENS[0], ITENS[0]])

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
