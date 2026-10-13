"""Scripts do CI (``scripts/ci/``): a conferencia que estreita a excecao do
trivy (KSV-0109) ao ConfigMap do ``papeis.sql``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
CONFERENCIA = RAIZ / "scripts" / "ci" / "confere-senhas-do-script.sh"


def _conferir(arquivo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(CONFERENCIA), str(arquivo)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _script(tmp_path: Path, sql: str) -> Path:
    arquivo = tmp_path / "papeis.sql"
    arquivo.write_text(sql)
    return arquivo


def test_o_papeis_do_repositorio_so_le_senha_do_ambiente() -> None:
    resultado = _conferir(RAIZ / "k8s" / "base" / "papeis.sql")

    assert resultado.returncode == 0, resultado.stderr


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE ROLE a LOGIN PASSWORD 'senha-literal';",
        "create role a login password 'senha-literal';",
        "CREATE ROLE a LOGIN PASSWORD\n  'senha-literal';",
        "ALTER ROLE a PASSWORD 'senha-literal';",
        "\\getenv senha POSTGRES_APP_PASSWORD\nCREATE ROLE a PASSWORD 'literal';",
        "\\getenv senha POSTGRES_APP_PASSWORD\nCREATE ROLE a PASSWORD :senha;",
        "CREATE ROLE a LOGIN PASSWORD :'senha';",
        "\\set senha 'literal'\nCREATE ROLE a LOGIN PASSWORD :'senha';",
        "\\getenv senha POSTGRES_APP_PASSWORD\n\\set senha 'literal'\n"
        "CREATE ROLE a LOGIN PASSWORD :'senha';",
        "\\getenv senha POSTGRES_APP_PASSWORD\n"
        "CREATE ROLE a PASSWORD 'lit' || :'senha';",
    ],
    ids=[
        "literal",
        "minuscula",
        "literal-na-linha-de-baixo",
        "alter-role",
        "literal-ao-lado-de-getenv",
        "variavel-sem-aspas",
        "variavel-sem-getenv",
        "variavel-do-set",
        "variavel-sobrescrita-por-set",
        "literal-concatenado",
    ],
)
def test_password_que_nao_vem_de_variavel_lida_do_ambiente_reprova(
    tmp_path: Path, sql: str
) -> None:
    resultado = _conferir(_script(tmp_path, sql))

    assert resultado.returncode == 1
    assert "PASSWORD sem a variavel do psql" in resultado.stderr


def test_um_literal_entre_dois_password_corretos_reprova_so_ele(
    tmp_path: Path,
) -> None:
    resultado = _conferir(
        _script(
            tmp_path,
            "\\getenv a POSTGRES_OWNER_PASSWORD\n\\getenv b POSTGRES_APP_PASSWORD\n"
            "CREATE ROLE x LOGIN PASSWORD :'a';\n"
            "CREATE ROLE y LOGIN PASSWORD 'literal';\n"
            "CREATE ROLE z LOGIN PASSWORD :'b';\n",
        )
    )

    assert resultado.returncode == 1
    assert "literal" in resultado.stderr
    assert ":'a'" not in resultado.stderr
    assert ":'b'" not in resultado.stderr


@pytest.mark.parametrize(
    "sql",
    [
        "\\getenv senha POSTGRES_APP_PASSWORD\nCREATE ROLE a LOGIN PASSWORD :'senha';",
        "\\getenv senha POSTGRES_APP_PASSWORD\n"
        "CREATE ROLE a LOGIN PASSWORD\n  :'senha';",
        "\\getenv senha SENHA_NOVA\nALTER ROLE a PASSWORD :'senha';",
        "\\getenv senha POSTGRES_APP_PASSWORD\n-- PASSWORD 'so em comentario'\n"
        "CREATE ROLE a LOGIN PASSWORD :'senha'; -- PASSWORD 'outro'",
    ],
    ids=["mesma-linha", "linha-de-baixo", "alter-role", "literal-so-em-comentario"],
)
def test_password_com_variavel_lida_do_ambiente_passa(tmp_path: Path, sql: str) -> None:
    assert _conferir(_script(tmp_path, sql)).returncode == 0


def test_script_sem_password_reprova_porque_nao_prova_nada(tmp_path: Path) -> None:
    resultado = _conferir(_script(tmp_path, "SELECT 1;\n"))

    assert resultado.returncode == 1
    assert "nenhum PASSWORD" in resultado.stderr


def test_o_make_manifests_roda_a_conferencia_antes_do_trivy() -> None:
    makefile = (RAIZ / "Makefile").read_text()
    alvo = makefile.split("\nmanifests:\n", 1)[1]

    assert alvo.index("scripts/ci/confere-senhas-do-script.sh") < alvo.index(
        "TRIVY_CONFIG"
    )
    assert "--ignore-policy" in makefile
