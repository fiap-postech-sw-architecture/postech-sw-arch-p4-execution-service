from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.compartilhado.dominio.veiculo import Veiculo

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _veiculo(
    placa: str = "ABC1D23", marca: str = "Fiat", modelo: str = "Uno", ano: int = 2015
) -> Veiculo:
    return Veiculo(veiculo_id=uuid4(), placa=placa, marca=marca, modelo=modelo, ano=ano)


@pytest.mark.parametrize(
    ("placa", "normalizada"),
    [
        pytest.param("abc-1234", "ABC1234", id="antiga-com-hifen"),
        pytest.param("ABC1D23", "ABC1D23", id="mercosul"),
        pytest.param(" abc1d23 ", "ABC1D23", id="espacos-e-minusculas"),
    ],
)
def test_placa_antiga_e_mercosul_normalizadas(placa: str, normalizada: str) -> None:
    assert _veiculo(placa=placa).validado(AGORA).placa == normalizada


@pytest.mark.parametrize(
    "placa",
    [
        pytest.param("", id="vazia"),
        pytest.param("AB1234", id="curta"),
        pytest.param("ABCD123", id="quatro-letras"),
        pytest.param("ABC12D3", id="letra-fora-do-lugar"),
        pytest.param("1BC1234", id="digito-no-inicio"),
        pytest.param("ANONIMIZADO:x", id="marcador-da-lgpd-nao-e-entrada"),
    ],
)
def test_placa_invalida_sem_ecoar_o_valor(placa: str) -> None:
    veiculo = _veiculo(placa=placa)
    with pytest.raises(ValueError, match="Placa invalida") as erro:
        veiculo.validado(AGORA)
    if placa:
        assert placa not in str(erro.value)


@pytest.mark.parametrize("campo", ["marca", "modelo"])
@pytest.mark.parametrize(
    "valor",
    [
        pytest.param("", id="vazio"),
        pytest.param("  ", id="espacos"),
        pytest.param("x" * 101, id="101-caracteres"),
        pytest.param("Fi\x00at", id="nul"),
        pytest.param("Fiat\nUno", id="quebra-de-linha"),
    ],
)
def test_marca_e_modelo_obrigatorios_e_limpos(campo: str, valor: str) -> None:
    veiculo = _veiculo(**{campo: valor})
    with pytest.raises(ValueError, match="do veiculo"):
        veiculo.validado(AGORA)


def test_marca_e_modelo_aparados_e_no_limite() -> None:
    veiculo = _veiculo(marca=" Fiat ", modelo="x" * 100).validado(AGORA)
    assert (veiculo.marca, veiculo.modelo) == ("Fiat", "x" * 100)


@pytest.mark.parametrize(
    "ano",
    [pytest.param(1886, id="antes-do-primeiro-carro"), pytest.param(2028, id="2-anos")],
)
def test_ano_fora_da_faixa(ano: int) -> None:
    veiculo = _veiculo(ano=ano)
    with pytest.raises(ValueError, match="Ano do veiculo"):
        veiculo.validado(AGORA)


@pytest.mark.parametrize(
    "ano",
    [
        pytest.param(1887, id="primeiro-ano"),
        pytest.param(2027, id="ano-modelo-seguinte"),
    ],
)
def test_bordas_validas_do_ano(ano: int) -> None:
    # O teto vem do relogio injetado (2026 + 1), nao do relogio da maquina.
    assert _veiculo(ano=ano).validado(AGORA).ano == ano


def test_construtor_reidrata_sem_validar_o_estado_anonimizado() -> None:
    veiculo_id = uuid4()
    gravado = Veiculo(
        veiculo_id=veiculo_id,
        placa=f"ANONIMIZADO:{veiculo_id}",
        marca="Fiat",
        modelo="Uno",
        ano=2015,
    )
    assert gravado.anonimizado


def test_anonimizar_troca_so_a_placa_pelo_marcador() -> None:
    original = _veiculo().validado(AGORA)
    anonimo = original.anonimizar()
    assert anonimo.placa == f"ANONIMIZADO:{original.veiculo_id}"
    assert (anonimo.marca, anonimo.modelo, anonimo.ano) == ("Fiat", "Uno", 2015)
    assert anonimo.anonimizado
    assert not original.anonimizado


def test_repr_mascara_a_placa() -> None:
    texto = repr(_veiculo(placa="ABC1D23"))
    assert "ABC1D23" not in texto
    assert "AB*****" in texto


def test_repr_do_anonimizado_mostra_o_marcador() -> None:
    anonimo = _veiculo().anonimizar()
    assert anonimo.placa in repr(anonimo)
