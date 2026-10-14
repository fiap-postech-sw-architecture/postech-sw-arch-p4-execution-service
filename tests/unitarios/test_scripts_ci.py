"""Scripts do CI (``scripts/ci/``) e a excecao do trivy (KSV-0109): a conferencia
que a estreita ao ConfigMap do ``papeis.sql`` e o relatorio das imagens de
terceiros, que nunca reprova o job."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.manifestos import renderizar

RAIZ = Path(__file__).resolve().parents[2]
CONFERENCIA = RAIZ / "scripts" / "ci" / "confere-senhas-do-script.sh"
RELATORIO_TERCEIROS = RAIZ / "scripts" / "ci" / "relatorio-trivy-terceiros.sh"


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
        "\\getenv senha POSTGRES_APP_PASSWORD\nSELECT 'literal' AS senha \\gset\n"
        "CREATE ROLE a LOGIN PASSWORD :'senha';",
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
        "variavel-sobrescrita-por-gset",
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


def _trivy_config(configmap: str) -> subprocess.CompletedProcess[str]:
    """O ``trivy config`` do ``make manifests`` (mesma imagem, mesma politica) sobre
    um ConfigMap de nome ``configmap`` com ``DB_PASSWORD`` nos dados."""
    docker = shutil.which("docker")
    if docker is None:
        motivo = "docker nao encontrado: o trivy do make manifests roda numa imagem"
        if os.environ.get("CI"):
            pytest.fail(motivo)
        pytest.skip(motivo)
    versao = re.search(
        r"^TRIVY_VERSION := (\S+)$", (RAIZ / "Makefile").read_text(), re.MULTILINE
    )
    assert versao is not None
    nome = f"execucao-teste-trivy-{uuid.uuid4().hex[:8]}"
    manifesto = (
        "apiVersion: v1\nkind: ConfigMap\n"
        f"metadata:\n  name: {configmap}\n  namespace: pytstop-execucao\n"
        "data:\n  DB_PASSWORD: valor-de-teste\n"
    )
    try:
        return subprocess.run(  # noqa: S603 - docker do PATH, imagem pinada no Makefile
            [
                docker,
                "run",
                "--rm",
                "-i",
                "--name",
                nome,
                "-v",
                f"{RAIZ / 'k8s' / 'trivy-ignore.rego'}:/trivy-ignore.rego:ro",
                "--entrypoint",
                "sh",
                f"aquasec/trivy:{versao[1]}",
                "-c",
                "cat > /tmp/manifestos.yaml && trivy config --quiet "
                "--severity HIGH,CRITICAL --exit-code 1 "
                "--ignore-policy /trivy-ignore.rego /tmp/manifestos.yaml",
            ],
            input=manifesto,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    finally:
        subprocess.run(  # noqa: S603 - garante que o container nao fica para tras
            [docker, "rm", "-f", nome], capture_output=True, check=False, timeout=60
        )


@pytest.mark.rede
@pytest.mark.parametrize(
    ("configmap", "reprova"),
    [
        ("outro-configmap", True),
        ("execucao-postgres-init-evil", True),
        ("execucao-postgres-init-72kb2dk7cf-evil", True),
        ("execucao-postgres-init-72kb2dk7cf", False),
    ],
    ids=["outro-nome", "so-o-prefixo", "hash-com-sufixo", "o-do-kustomize"],
)
def test_a_excecao_do_ksv_0109_vale_so_para_o_configmap_do_papeis_sql(
    configmap: str, reprova: bool
) -> None:
    resultado = _trivy_config(configmap)

    # O nome inteiro, com o hash do kustomize: o prefixo sozinho deixaria passar
    # senha num ConfigMap de nome parecido.
    assert resultado.returncode == (1 if reprova else 0), (
        resultado.stdout + resultado.stderr
    )
    assert ("KSV-0109" in resultado.stdout) is reprova


def test_o_make_manifests_roda_a_conferencia_antes_do_trivy() -> None:
    makefile = (RAIZ / "Makefile").read_text()
    alvo = makefile.split("\nmanifests:\n", 1)[1]

    assert alvo.index("scripts/ci/confere-senhas-do-script.sh") < alvo.index(
        "TRIVY_CONFIG"
    )
    assert "--ignore-policy" in makefile


def test_a_versao_do_trivy_tem_uma_fonte_so_no_makefile() -> None:
    versao = re.search(
        r"^TRIVY_VERSION := (\S+)$", (RAIZ / "Makefile").read_text(), re.MULTILINE
    )
    assert versao is not None

    for workflow in (RAIZ / ".github" / "workflows").glob("*.yml"):
        assert versao[1] not in workflow.read_text(), workflow.name
    security = (RAIZ / ".github" / "workflows" / "security.yml").read_text()
    assert "make -s print-trivy-version" in security


# O trivy de mentira repete o do CI: o relatorio sai no stdout e o codigo e 1
# quando ha achado, salvo com --exit-code 0; a imagem de FALHA_EM nao chega a
# ser varrida (pull recusado), e o comando sai com 1 mesmo com --exit-code 0.
TRIVY_FALSO = """#!/usr/bin/env bash
echo "trivy $*" >> "$TRIVY_LOG"
imagem="${*: -1}"
if [ -n "$TRIVY_FALHA_EM" ] && [[ "$imagem" == *"$TRIVY_FALHA_EM"* ]]; then
  echo "FATAL pull recusado: $imagem" >&2
  exit 1
fi
echo "RELATORIO $imagem: Total: 3 (HIGH: 3)"
case " $* " in *" --exit-code 0 "*) exit 0 ;; esac
exit 1
"""
# O kubectl de mentira devolve o render que o teste mandou, como o
# `kubectl kustomize`: o kustomize ordena as chaves, e o image: abre o item da
# lista de containers ("- image: x") quando nada vem antes dele.
KUBECTL_FALSO = """#!/usr/bin/env bash
cat "$RENDER"
"""


def _render(imagens: list[str]) -> str:
    itens = "".join(
        f"      - image: {imagem}\n        name: c{i}\n"
        for i, imagem in enumerate(imagens)
    )
    return (
        f"kind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n{itens}"
    )


def _relatar(
    tmp_path: Path, imagens: list[str], falha_em: str = ""
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Roda o relatorio sobre o render com ``imagens``, o kubectl e o trivy de
    mentira; devolve o processo, o summary e o log de chamadas."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for nome, conteudo in (("trivy", TRIVY_FALSO), ("kubectl", KUBECTL_FALSO)):
        (bin_ / nome).write_text(conteudo)
        (bin_ / nome).chmod(0o755)
    render, resumo, log = (
        tmp_path / "render.yaml",
        tmp_path / "resumo.md",
        tmp_path / "trivy.log",
    )
    render.write_text(_render(imagens))
    resultado = subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(RELATORIO_TERCEIROS), "k8s/overlays/qualquer"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={
            **os.environ,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "RENDER": str(render),
            "GITHUB_STEP_SUMMARY": str(resumo),
            "TRIVY_LOG": str(log),
            "TRIVY_FALHA_EM": falha_em,
        },
    )
    return (
        resultado,
        resumo.read_text() if resumo.exists() else "",
        log.read_text() if log.exists() else "",
    )


def test_achado_do_trivy_nao_reprova_e_entra_no_relatorio(tmp_path: Path) -> None:
    resultado, resumo, chamadas = _relatar(
        tmp_path,
        [
            "pytstop-execution-service:dev",
            "terceiro/a:1",
            "terceiro/b:2",
            "terceiro/a:1",
        ],
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "RELATORIO terceiro/a:1" in resumo
    assert "RELATORIO terceiro/b:2" in resumo
    assert "falhou" not in resumo
    # A imagem do servico tem o gate proprio, e cada terceira e varrida uma vez.
    assert "pytstop-execution-service" not in chamadas
    assert chamadas.count("terceiro/a:1") == 1


def test_falha_da_varredura_vira_aviso_e_nao_reprova(tmp_path: Path) -> None:
    resultado, resumo, _ = _relatar(
        tmp_path, ["terceiro/a:1", "terceiro/b:2"], falha_em="terceiro/a"
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "terceiro/a:1: varredura falhou (codigo 1)" in resumo
    assert "::warning::" in resultado.stdout
    # A falha de uma imagem nao impede o relatorio da seguinte.
    assert "RELATORIO terceiro/b:2" in resumo


def test_todas_as_varreduras_falhando_tambem_nao_reprova(tmp_path: Path) -> None:
    resultado, resumo, _ = _relatar(
        tmp_path, ["terceiro/a:1", "terceiro/b:2"], falha_em="terceiro"
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resumo.count("varredura falhou") == 2


@pytest.mark.parametrize(
    "imagens", [[], ["pytstop-execution-service:dev"]], ids=["vazio", "so-o-servico"]
)
def test_sem_imagem_de_terceiro_reprova_porque_o_relatorio_nao_prova_nada(
    tmp_path: Path, imagens: list[str]
) -> None:
    resultado, resumo, chamadas = _relatar(tmp_path, imagens)

    assert resultado.returncode == 1
    assert "nenhuma imagem de terceiro" in resultado.stderr
    assert resumo == chamadas == ""


def test_o_relatorio_varre_o_postgres_e_o_exporter_do_render_real(
    tmp_path: Path,
) -> None:
    renderizar("kind")  # pula sem kubectl (e reprova no CI)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "trivy").write_text(TRIVY_FALSO)
    (bin_ / "trivy").chmod(0o755)
    log = tmp_path / "trivy.log"
    resultado = subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(RELATORIO_TERCEIROS)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=RAIZ,
        env={
            **os.environ,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "resumo.md"),
            "TRIVY_LOG": str(log),
            "TRIVY_FALHA_EM": "",
        },
    )

    assert resultado.returncode == 0, resultado.stderr
    varridas = [linha.rsplit(" ", 1)[1] for linha in log.read_text().splitlines()]
    assert varridas == [
        "postgres:16.15",
        "prometheuscommunity/postgres-exporter:v0.20.1",
    ]
