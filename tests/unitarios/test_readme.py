"""README e docs: os comandos dos blocos de shell podem ser colados num terminal."""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
DOCUMENTOS = [RAIZ / "README.md", *sorted((RAIZ / "docs").glob("**/*.md"))]


def test_nenhum_comando_dos_documentos_leva_comentario_na_linha() -> None:
    # No zsh interativo (o padrao do macOS) o `#` nao e comentario: o `make`
    # receberia `#` e as palavras seguintes como alvos.
    blocos = [
        bloco
        for documento in DOCUMENTOS
        for bloco in re.findall(
            r"```bash\n(.*?)```", documento.read_text(), flags=re.DOTALL
        )
    ]
    comentados = [
        linha
        for bloco in blocos
        for linha in bloco.splitlines()
        if linha.strip()
        and not linha.lstrip().startswith("#")
        and re.search(r"\s#", linha)
    ]

    assert blocos
    assert comentados == []
