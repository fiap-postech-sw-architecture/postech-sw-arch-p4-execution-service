"""Os manifestos de ``k8s/`` como o cluster os recebe: o ``kubectl kustomize``.

Quem le o YAML cru da base nao enxerga o que o kustomize muda (a tag da imagem,
o patch do overlay, o nome com hash do ConfigMap). Sem o ``kubectl`` o teste e
pulado na maquina local e reprova no CI, onde ele vem no runner: um teste
pulado em silencio la deixaria o contrato dos manifestos sem guarda.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
OVERLAYS = ("kind", "kind-ci", "k3s")


@functools.cache
def renderizar(overlay: str) -> tuple[dict[str, Any], ...]:
    """Os objetos do ``kubectl kustomize k8s/overlays/<overlay>``."""
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        motivo = "kubectl nao encontrado: o kustomize vem com ele"
        if os.environ.get("CI"):
            pytest.fail(motivo)
        pytest.skip(motivo)
    saida = subprocess.run(  # noqa: S603 - kubectl do PATH, sobre o proprio repositorio
        [kubectl, "kustomize", str(RAIZ / "k8s" / "overlays" / overlay)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return tuple(documento for documento in yaml.safe_load_all(saida) if documento)


def objeto(overlay: str, tipo: str, nome: str) -> dict[str, Any]:
    """O objeto de ``tipo`` e ``nome``; o ConfigMap gerado leva um hash no nome."""
    padrao = re.compile(
        rf"{re.escape(nome)}(-[a-z0-9]{{10}})?"
        if tipo == "ConfigMap"
        else re.escape(nome)
    )
    [achado] = [
        documento
        for documento in renderizar(overlay)
        if documento["kind"] == tipo and padrao.fullmatch(documento["metadata"]["name"])
    ]
    return achado
