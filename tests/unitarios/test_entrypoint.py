"""entrypoint.sh e o prefixo da borda: o uvicorn sobe com --root-path do ROOT_PATH.

Atras do Kong, com strip-path, a API recebe ``/docs`` e o Swagger tem de buscar
``/execucao/openapi.json``; as sondas do kubelet e o Prometheus chegam direto ao
pod, sem o prefixo (ADR-038).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from src.main import criar_app

RAIZ = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("root_path", "esperado"),
    [pytest.param("/execucao", "/execucao", id="kubernetes"), (None, "")],
)
def test_entrypoint_sobe_o_uvicorn_com_o_root_path_do_ambiente(
    tmp_path: Path, root_path: str | None, esperado: str
) -> None:
    # uvicorn falso no PATH: devolve os argumentos que recebeu, um por linha.
    falso = tmp_path / "uvicorn"
    falso.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    falso.chmod(0o755)
    ambiente = {
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PYTSTOP_GIT_SHA": "0123456789abcdef",
    }
    if root_path is not None:
        ambiente["ROOT_PATH"] = root_path
    saida = subprocess.run(  # noqa: S603 - o entrypoint do repositorio
        ["/bin/bash", str(RAIZ / "entrypoint.sh")],
        env=ambiente,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    posicao = saida.index("--root-path")
    assert saida[posicao + 1] == esperado


def test_uvicorn_com_o_prefixo_serve_o_swagger_sob_ele_e_as_sondas_sem_ele() -> None:
    servidor = uvicorn.Server(
        uvicorn.Config(
            criar_app(),
            host="127.0.0.1",
            port=0,
            root_path="/execucao",
            lifespan="off",
            log_config=None,
        )
    )
    thread = threading.Thread(target=servidor.run, daemon=True)
    thread.start()
    try:
        prazo = time.monotonic() + 10
        while not servidor.started and time.monotonic() < prazo:
            time.sleep(0.05)
        assert servidor.started
        porta = servidor.servers[0].sockets[0].getsockname()[1]
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=5) as cliente:
            # O que o Kong entrega depois do strip-path.
            docs = cliente.get("/docs")
            openapi = cliente.get("/openapi.json")
            # Sonda e raspagem, direto no pod.
            saude = cliente.get("/api/v1/saude")
            metricas = cliente.get("/metrics")
    finally:
        servidor.should_exit = True
        thread.join(timeout=10)
    assert docs.status_code == 200
    assert "'/execucao/openapi.json'" in docs.text
    # A pagina do Swagger precisa dos scripts dela: sem o CSP restrito da API.
    assert "Content-Security-Policy" not in docs.headers
    assert openapi.json()["servers"] == [{"url": "/execucao"}]
    assert saude.json() == {"status": "ok"}
    assert saude.headers["Content-Security-Policy"] == "default-src 'none'"
    assert metricas.status_code == 200
