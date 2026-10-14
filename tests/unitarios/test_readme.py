"""README: os comandos dos blocos de shell podem ser colados num terminal."""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


def test_nenhum_comando_do_readme_leva_comentario_na_linha() -> None:
    # No zsh interativo (o padrao do macOS) o `#` nao e comentario: o `make`
    # receberia `#` e as palavras seguintes como alvos.
    readme = (RAIZ / "README.md").read_text()
    blocos = re.findall(r"```bash\n(.*?)```", readme, flags=re.DOTALL)
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
