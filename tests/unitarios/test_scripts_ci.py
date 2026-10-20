"""Scripts do CI (``scripts/ci/``) e a excecao do trivy (KSV-0109): a conferencia
que a estreita ao ConfigMap do ``papeis.sql``, o relatorio das imagens de
terceiros, que nunca reprova o job, o registro da saida do pip-audit e do trivy
no summary, os nomes das variaveis do ``make kind-deploy``, a guarda de frescor
do ``make diff-cover`` e o ``dependabot.yml``."""

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from tests.manifestos import renderizar

RAIZ = Path(__file__).resolve().parents[2]
CONFERENCIA = RAIZ / "scripts" / "ci" / "confere-senhas-do-script.sh"
RELATORIO_TERCEIROS = RAIZ / "scripts" / "ci" / "relatorio-trivy-terceiros.sh"
REGISTRA_NO_SUMMARY = RAIZ / "scripts" / "ci" / "registra-no-summary.sh"


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


def _registrar(
    tmp_path: Path, *comando: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    resumo = tmp_path / "resumo.md"
    resultado = subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(REGISTRA_NO_SUMMARY), "pip-audit", *comando],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(resumo)},
    )
    return resultado, resumo.read_text() if resumo.exists() else ""


def test_comando_que_passa_vai_para_o_log_e_para_o_summary(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path, "echo", "No known vulnerabilities found")

    assert resultado.returncode == 0
    assert resultado.stdout == "No known vulnerabilities found\n"
    assert resumo == (
        "### pip-audit: sem achado\n```text\nNo known vulnerabilities found\n```\n"
    )


def test_comando_que_reprova_registra_a_saida_e_mantem_o_codigo(
    tmp_path: Path,
) -> None:
    resultado, resumo = _registrar(
        tmp_path, "/bin/sh", "-c", "echo achado-no-stdout; echo erro >&2; exit 3"
    )

    assert resultado.returncode == 3
    assert "achado-no-stdout" in resultado.stdout
    assert resumo.startswith("### pip-audit: reprovou (codigo 3)\n```text\n")
    assert "achado-no-stdout" in resumo
    assert "erro" in resumo


def test_a_saida_aparece_no_log_enquanto_o_comando_roda(tmp_path: Path) -> None:
    """Com o comando ainda de pe (ele espera um arquivo que so o teste cria), a
    primeira linha ja tem de estar no log: trivy baixando o banco ou um
    pip-audit pendurado precisam mostrar o que fazem ao vivo."""
    fim = tmp_path / "fim"
    espera = f"echo comecou; until [ -e {fim} ]; do sleep 0.1; done; echo terminou"
    processo = subprocess.Popen(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(REGISTRA_NO_SUMMARY), "ao vivo", "/bin/sh", "-c", espera],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(tmp_path / "resumo.md")},
    )
    assert processo.stdout is not None
    try:
        pronto, _, _ = select.select([processo.stdout], [], [], 10)
        primeira = processo.stdout.readline() if pronto else ""
        fim.touch()
        resto, _ = processo.communicate(timeout=10)
    finally:
        processo.kill()

    assert primeira == "comecou\n"
    assert resto == "terminou\n"
    assert processo.returncode == 0


def test_comando_inexistente_reprova_e_aparece_no_summary(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path, "comando-que-nao-existe-aqui")

    assert resultado.returncode == 127
    assert "reprovou (codigo 127)" in resumo


def test_sem_comando_o_script_recusa(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path)

    assert resultado.returncode == 64
    assert "uso:" in resultado.stderr
    assert resumo == ""


@pytest.mark.parametrize(
    ("job", "ferramenta", "titulo"),
    [
        ("pip-audit", "uvx pip-audit", "dependencias de runtime"),
        ("trivy", "trivy image", "HIGH e CRITICAL com correcao"),
    ],
)
def test_o_security_yml_registra_o_pip_audit_e_o_trivy_no_summary(
    job: str, ferramenta: str, titulo: str
) -> None:
    """O titulo do trivy diz o filtro do passo: sem ele, "sem achado" leria como
    "sem CVE nenhuma"."""
    fluxo = yaml.safe_load(
        (RAIZ / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    )
    comandos = [
        " ".join(passo["run"].split())
        for passo in fluxo["jobs"][job]["steps"]
        if ferramenta in passo.get("run", "")
    ]

    assert len(comandos) == 1
    assert comandos[0].startswith("scripts/ci/registra-no-summary.sh ")
    assert titulo in comandos[0].split(ferramenta)[0]


def test_dependabot_tem_agenda_prefixo_grupos_e_ignore_de_cada_bloco() -> None:
    config = yaml.safe_load(
        (RAIZ / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )
    agenda = {
        "interval": "weekly",
        "day": "monday",
        "time": "06:00",
        "timezone": "America/Sao_Paulo",
    }
    patch_e_minor = {"update-types": ["minor", "patch"]}
    # O Python sobe a mao; o uv da imagem sobe pelo Dependabot, sem ignore.
    so_patch_do_python = [
        {
            "dependency-name": "python",
            "update-types": [
                "version-update:semver-major",
                "version-update:semver-minor",
            ],
        }
    ]
    # O teto do pyproject.toml nao segura o Dependabot (ele o alarga): a minor do
    # sqlalchemy (troca o driver do PostgreSQL) e a dos opentelemetry-* (o
    # telemetria.py mexe em atributos privados do SDK) so entram a mao.
    sem_minor = ["version-update:semver-major", "version-update:semver-minor"]
    sem_minor_de_sqlalchemy_e_opentelemetry = [
        {"dependency-name": "sqlalchemy", "update-types": sem_minor},
        {"dependency-name": "opentelemetry-*", "update-types": sem_minor},
    ]
    # Um grupo por nome (o do OpenTelemetry) ficaria vazio: o Dependabot prefere
    # o de update-types, entao cada bloco tem so o de patch e minor.
    esperado = {
        "uv": (
            "chore(deps)",
            {"python-patch-e-minor": patch_e_minor},
            sem_minor_de_sqlalchemy_e_opentelemetry,
        ),
        "github-actions": ("ci", {"actions-patch-e-minor": patch_e_minor}, []),
        "docker": (
            "chore(deps)",
            {"docker-patch-e-minor": patch_e_minor},
            so_patch_do_python,
        ),
    }

    blocos = {bloco["package-ecosystem"]: bloco for bloco in config["updates"]}

    assert config["version"] == 2
    assert set(config) == {"version", "updates"}
    # Um bloco por ecossistema: o dict acima esconderia um repetido.
    assert len(config["updates"]) == len(esperado)
    assert set(blocos) == set(esperado)
    for nome, (prefixo, grupos, ignore) in esperado.items():
        # O bloco inteiro: chave a mais (allow, cooldown, target-branch,
        # versioning-strategy...) reprova. Sem `labels`: rotulo declarado e
        # inexistente no repositorio vira comentario em cada PR do Dependabot,
        # que cria os padrao se faltarem.
        assert blocos[nome] == {
            "package-ecosystem": nome,
            "directory": "/",
            "schedule": agenda,
            "open-pull-requests-limit": 3,
            "commit-message": {"prefix": prefixo},
            "groups": grupos,
            **({"ignore": ignore} if ignore else {}),
        }, nome


PADRAO = "../postech-sw-arch-p4-platform"


@pytest.mark.parametrize(
    ("variaveis", "platform", "overlay"),
    [
        ([], PADRAO, "kind"),
        (["KIND_OVERLAY=kind-ci"], PADRAO, "kind-ci"),
        (["KIND_OVERLAY=k3s"], PADRAO, "k3s"),
        (["PLATFORM_DIR=/clone/platform"], "/clone/platform", "kind"),
    ],
    ids=["padroes", "kind-overlay", "k3s", "platform-dir"],
)
def test_kind_deploy_aceita_platform_dir_e_kind_overlay(
    variaveis: list[str], platform: str, overlay: str
) -> None:
    """``PLATFORM_DIR`` (o clone do platform) e ``KIND_OVERLAY`` (kind, kind-ci ou
    k3s) sao os nomes do alvo nos tres servicos. ``--no-print-directory``: o
    make 4 dos runners escreve "Entering directory" na saida do ``-n`` quando o
    teste roda dentro do ``make test``."""
    resultado = subprocess.run(  # noqa: S603 - o Makefile do proprio repositorio
        ["make", "-n", "--no-print-directory", "kind-deploy", *variaveis],  # noqa: S607
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert resultado.returncode == 0, resultado.stderr
    comando = " ".join(resultado.stdout.replace("\\\n", " ").split())
    assert comando.startswith(f"{platform}/scripts/ci/implantar-servicos.sh")
    assert f" --overlay {overlay} execution-service=" in comando


def _diff_cover(
    tmp_path: Path, xml_mais_novo: bool, *, sem_xml: bool = False
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """``make diff-cover`` numa arvore minuscula, com um ``uvx`` falso no PATH que
    so registra os argumentos."""
    shutil.copy(RAIZ / "Makefile", tmp_path / "Makefile")
    (tmp_path / "src").mkdir()
    codigo, xml = tmp_path / "src" / "modulo.py", tmp_path / "coverage.xml"
    codigo.write_text("X = 1\n", encoding="utf-8")
    xml.write_text("<coverage/>", encoding="utf-8")
    os.utime(codigo, (2_000_000_000, 2_000_000_000))
    do_xml = 2_000_000_100 if xml_mais_novo else 1_999_999_900
    os.utime(xml, (do_xml, do_xml))
    if sem_xml:
        xml.unlink()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    argumentos = tmp_path / "argumentos-do-uvx"
    falso = bin_ / "uvx"
    falso.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {argumentos}\n', encoding="utf-8"
    )
    falso.chmod(0o755)
    ambiente = {
        chave: valor for chave, valor in os.environ.items() if chave != "MAKEFLAGS"
    }
    resultado = subprocess.run(
        ["make", "--no-print-directory", "diff-cover"],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={**ambiente, "PATH": f"{bin_}:{ambiente['PATH']}", "MAKELEVEL": "1"},
    )
    return resultado, argumentos


def test_diff_cover_recusa_coverage_xml_mais_velho_que_o_codigo(
    tmp_path: Path,
) -> None:
    resultado, argumentos = _diff_cover(tmp_path, xml_mais_novo=False)

    assert resultado.returncode != 0
    assert "coverage.xml is older than src/, run make test first" in resultado.stderr
    assert not argumentos.exists()


def test_diff_cover_sem_coverage_xml_manda_rodar_o_make_test(tmp_path: Path) -> None:
    # Sem o XML o `find -newer` nao imprime nada e so a primeira guarda o pega.
    resultado, argumentos = _diff_cover(tmp_path, xml_mais_novo=True, sem_xml=True)

    assert resultado.returncode != 0
    assert "coverage.xml missing, run make test first" in resultado.stderr
    assert not argumentos.exists()


def test_diff_cover_com_xml_fresco_mede_tambem_o_arquivo_novo(
    tmp_path: Path,
) -> None:
    resultado, argumentos = _diff_cover(tmp_path, xml_mais_novo=True)

    assert resultado.returncode == 0, resultado.stderr
    assert argumentos.read_text(encoding="utf-8").split() == [
        "diff-cover==10.6.0",
        "coverage.xml",
        "--branch-coverage",
        "--include-untracked",
        "--compare-branch=origin/main",
        "--fail-under=90",
        "--format",
        "markdown:reports/diff-cover.md",
    ]
