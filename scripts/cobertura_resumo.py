"""Resumo da cobertura em Markdown a partir do coverage.xml (formato Cobertura).

Uso no summary do CI:
    python scripts/cobertura_resumo.py coverage.xml >> "$GITHUB_STEP_SUMMARY"
Uso no README (troca o trecho entre os marcadores pela tabela e pelo link do run):
    python scripts/cobertura_resumo.py coverage.xml README.md <URL do run do CI>

Agrupa por contexto e camada (`src/<contexto>/<camada>`) para a evidencia de
cobertura pedida no enunciado ficar legivel no summary do CI e no README, onde
a tabela e gerada, nunca digitada: o coverage.xml e o do artefato
`relatorios-de-teste` do run que o link aponta. O XML lido e o gerado pelo
proprio pytest-cov, nao entrada externa (por isso o parser da stdlib basta).
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree  # nosec B405

PROFUNDIDADE = 2  # <contexto>/<camada>, relativo ao <source> (relative_files = True)
INICIO = "<!-- cobertura:inicio -->"
FIM = "<!-- cobertura:fim -->"
_RUN = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/(\d+)")
_ARGUMENTOS = 2  # programa + coverage.xml
_COM_README = 4  # programa + coverage.xml + README + URL do run
_USO = "uso: python scripts/cobertura_resumo.py coverage.xml [README.md URL-do-run]"


def _grupo(nome_arquivo: str, prefixo: str) -> str:
    partes = Path(nome_arquivo).parts[:-1][:PROFUNDIDADE]
    return "/".join([prefixo, *partes]) if partes else prefixo


def resumir(caminho: Path) -> str:
    # Arquivo gerado pelo proprio pytest-cov no mesmo job, nao entrada externa.
    raiz = ElementTree.parse(caminho).getroot()  # noqa: S314  # nosec B314
    fontes = [s.text or "." for s in raiz.iter("source")]
    prefixo = Path(fontes[0]).name if len(fontes) == 1 else "."
    linhas: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for classe in raiz.iter("class"):
        grupo = _grupo(classe.get("filename", ""), prefixo)
        for linha in classe.iter("line"):
            linhas[grupo][0] += 1
            if int(linha.get("hits", "0")) > 0:
                linhas[grupo][1] += 1
    total_linhas = sum(v[0] for v in linhas.values())
    total_cobertas = sum(v[1] for v in linhas.values())
    pct_total = 100.0 * total_cobertas / total_linhas if total_linhas else 0.0
    ramo = float(raiz.get("branch-rate", "0")) * 100
    # O numero do gate (fail_under, branch = True) soma linhas e ramos.
    validos = int(raiz.get("lines-valid", "0")) + int(raiz.get("branches-valid", "0"))
    cobertos = int(raiz.get("lines-covered", "0")) + int(
        raiz.get("branches-covered", "0")
    )
    gate = 100.0 * cobertos / validos if validos else 0.0
    saida = [
        f"### Cobertura de testes: {gate:.1f}% no gate (linhas e ramos)",
        "",
        f"{pct_total:.1f}% de linhas e {ramo:.1f}% de ramos.",
        "",
        "| Pacote | Linhas | Cobertas | Cobertura |",
        "|---|---:|---:|---:|",
    ]
    for grupo in sorted(linhas):
        tot, cob = linhas[grupo]
        pct = 100.0 * cob / tot if tot else 0.0
        saida.append(f"| `{grupo}` | {tot} | {cob} | {pct:.1f}% |")
    saida.append(
        f"| **total** | {total_linhas} | {total_cobertas} | {pct_total:.1f}% |"
    )
    return "\n".join(saida) + "\n"


def atualizar_readme(xml: Path, readme: Path, url_do_run: str) -> int:
    """Troca o trecho entre ``INICIO`` e ``FIM`` pela tabela e pelo link do run."""
    run = _RUN.fullmatch(url_do_run)
    if run is None:
        print(f"{url_do_run} nao e o link de um run do GitHub Actions", file=sys.stderr)
        return 2
    texto = readme.read_text()
    inicio, fim = texto.find(INICIO), texto.find(FIM)
    if inicio < 0 or fim < inicio:
        print(f"{readme} sem os marcadores {INICIO} e {FIM}", file=sys.stderr)
        return 2
    link = (
        "Tabela gerada por `scripts/cobertura_resumo.py` a partir do `coverage.xml` "
        f"do [run {run[1]} do CI]({url_do_run})."
    )
    secao = f"{INICIO}\n{resumir(xml)}\n{link}\n{FIM}"
    readme.write_text(texto[:inicio] + secao + texto[fim + len(FIM) :])
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == _COM_README:
        return atualizar_readme(Path(argv[1]), Path(argv[2]), argv[3])
    if len(argv) != _ARGUMENTOS:
        print(_USO, file=sys.stderr)
        return 2
    caminho = Path(argv[1])
    if not caminho.exists():
        aviso = f"`{caminho}` nao foi gerado (a suite falhou antes)."
        print(f"### Cobertura de testes\n\n{aviso}\n")
        return 0
    sys.stdout.write(resumir(caminho))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
