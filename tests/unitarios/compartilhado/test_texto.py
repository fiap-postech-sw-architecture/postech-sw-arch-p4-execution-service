from __future__ import annotations

import pytest

from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.compartilhado.dominio.texto import texto_valido


def test_apara_e_devolve_o_texto() -> None:
    assert texto_valido("  Vela NGK  ", "Nome", maximo=10) == "Vela NGK"


@pytest.mark.parametrize(
    ("valor", "maximo", "obrigatorio"),
    [
        pytest.param("", 10, True, id="vazio-obrigatorio"),
        pytest.param("   ", 10, True, id="so-espacos"),
        pytest.param("x" * 11, 10, True, id="acima-do-maximo"),
        pytest.param("x" * 11, 10, False, id="acima-do-maximo-opcional"),
    ],
)
def test_tamanho_fora_da_faixa(valor: str, maximo: int, obrigatorio: bool) -> None:
    with pytest.raises(ValorInvalidoError, match="caracteres"):
        texto_valido(valor, "Campo", maximo=maximo, obrigatorio=obrigatorio)


def test_bordas_validas() -> None:
    assert texto_valido("x" * 10, "Campo", maximo=10) == "x" * 10
    assert texto_valido("", "Campo", maximo=10, obrigatorio=False) == ""


@pytest.mark.parametrize(
    "controle",
    [
        pytest.param("\x00", id="nul"),
        pytest.param("\x1b", id="escape"),
        pytest.param("\x7f", id="del"),
        pytest.param("\x08", id="backspace"),
    ],
)
@pytest.mark.parametrize("multilinha", [True, False], ids=["texto-livre", "nome"])
def test_caractere_de_controle_e_recusado_sem_ecoar_o_texto(
    controle: str, multilinha: bool
) -> None:
    with pytest.raises(ValorInvalidoError) as erro:
        texto_valido(f"segredo{controle}fim", "Campo", maximo=50, multilinha=multilinha)
    assert "segredo" not in str(erro.value)


def test_texto_livre_aceita_tabulacao_e_quebra_de_linha() -> None:
    texto = "linha 1\r\nlinha\t2"
    assert texto_valido(texto, "Obs", maximo=50, multilinha=True) == texto


@pytest.mark.parametrize("quebra", ["\n", "\t", "\r"], ids=["lf", "tab", "cr"])
def test_nome_de_uma_linha_recusa_quebra_e_tabulacao(quebra: str) -> None:
    with pytest.raises(ValorInvalidoError, match="controle"):
        texto_valido(f"Vela{quebra}NGK", "Nome", maximo=50)
