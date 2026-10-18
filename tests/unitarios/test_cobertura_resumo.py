"""O script do summary de cobertura do CI (scripts/cobertura_resumo.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cobertura_resumo.py"
_XML = """<?xml version="1.0" ?>
<coverage branch-rate="0.5" line-rate="0.75" lines-valid="6" lines-covered="4"
          branches-valid="4" branches-covered="2">
  <sources><source>/repo/src</source></sources>
  <packages><package name="x"><classes>
    <class filename="estoque/dominio/sku.py">
      <lines><line number="1" hits="1"/><line number="2" hits="1"/></lines>
    </class>
    <class filename="estoque/dominio/reserva.py">
      <lines><line number="1" hits="1"/><line number="2" hits="0"/></lines>
    </class>
    <class filename="main.py">
      <lines><line number="1" hits="1"/><line number="2" hits="0"/></lines>
    </class>
  </classes></package></packages>
</coverage>
"""


@pytest.fixture
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cobertura_resumo", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_agrupa_por_contexto_e_camada_com_total(
    script: ModuleType, tmp_path: Path
) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(_XML)

    linhas = script.resumir(xml).splitlines()

    # O gate (fail_under com branch = True) soma linhas e ramos: (4 + 2) / (6 + 4).
    assert linhas[0] == "### Cobertura de testes: 60.0% no gate (linhas e ramos)"
    assert linhas[2] == "66.7% de linhas e 50.0% de ramos."
    assert "| `src` | 2 | 1 | 50.0% |" in linhas
    assert "| `src/estoque/dominio` | 4 | 3 | 75.0% |" in linhas
    assert linhas[-1] == "| **total** | 6 | 4 | 66.7% |"


def test_xml_ausente_avisa_sem_falhar_o_job(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["prog", str(tmp_path / "coverage.xml")]) == 0
    assert "nao foi gerado" in capsys.readouterr().out


def test_uso_errado_sai_com_2(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["prog"]) == 2
    assert "uso:" in capsys.readouterr().err


def test_main_escreve_o_resumo(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(_XML)
    assert script.main(["prog", str(xml)]) == 0
    assert capsys.readouterr().out.startswith("### Cobertura de testes: 60.0% no gate")


_URL = "https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service/actions/runs/123"


def test_readme_recebe_a_tabela_entre_os_marcadores_com_o_link_do_run(
    script: ModuleType, tmp_path: Path
) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(_XML)
    readme = tmp_path / "README.md"
    readme.write_text(f"antes\n{script.INICIO}\ntabela velha\n{script.FIM}\ndepois\n")

    assert script.main(["prog", str(xml), str(readme), _URL]) == 0

    texto = readme.read_text()
    assert texto.startswith(f"antes\n{script.INICIO}\n{script.resumir(xml)}\n")
    assert texto.endswith(f"[run 123 do CI]({_URL}).\n{script.FIM}\ndepois\n")
    assert "tabela velha" not in texto


@pytest.mark.parametrize(
    ("conteudo", "url", "motivo"),
    [
        ("sem marcadores\n", _URL, "sem os marcadores"),
        (
            "<!-- cobertura:fim -->\n<!-- cobertura:inicio -->\n",
            _URL,
            "sem os marcadores",
        ),
        (
            "<!-- cobertura:inicio -->\n<!-- cobertura:fim -->\n",
            "https://x/1",
            "nao e o link",
        ),
    ],
    ids=["sem marcadores", "marcadores invertidos", "link que nao e run"],
)
def test_readme_sem_marcadores_ou_link_que_nao_e_run_fica_como_estava(
    script: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    conteudo: str,
    url: str,
    motivo: str,
) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(_XML)
    readme = tmp_path / "README.md"
    readme.write_text(conteudo)

    assert script.main(["prog", str(xml), str(readme), url]) == 2
    assert motivo in capsys.readouterr().err
    assert readme.read_text() == conteudo
