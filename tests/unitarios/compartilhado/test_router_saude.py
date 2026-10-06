from __future__ import annotations

import time
from typing import Self

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.compartilhado.interfaces.router_saude import router


class _Sessao:
    """Sessao de mentira: ``atraso`` simula banco lento, ``erro`` banco fora."""

    def __init__(self, *, atraso: float = 0.0, erro: Exception | None = None) -> None:
        self.atraso = atraso
        self.erro = erro
        self.consultas = 0

    def __call__(self) -> Self:
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, _stmt: object) -> None:
        self.consultas += 1
        time.sleep(self.atraso)
        if self.erro is not None:
            raise self.erro


def _cliente(sessao: _Sessao) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = sessao
    return TestClient(app)


def test_liveness_nao_consulta_o_banco() -> None:
    sessao = _Sessao(erro=OperationalError("SELECT 1", {}, Exception("fora")))
    resposta = _cliente(sessao).get("/api/v1/saude")
    assert resposta.json() == {"status": "ok"}
    assert sessao.consultas == 0


def test_readiness_com_o_banco_respondendo() -> None:
    sessao = _Sessao()
    resposta = _cliente(sessao).get("/api/v1/saude/pronto")
    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok"}
    assert sessao.consultas == 1


def test_readiness_com_o_banco_fora_e_503() -> None:
    sessao = _Sessao(erro=OperationalError("SELECT 1", {}, Exception("fora")))
    resposta = _cliente(sessao).get("/api/v1/saude/pronto")
    assert resposta.status_code == 503
    assert resposta.json() == {"status": "indisponivel"}


def test_readiness_desiste_do_banco_lento_em_2s() -> None:
    cliente = _cliente(_Sessao(atraso=3))
    inicio = time.monotonic()
    resposta = cliente.get("/api/v1/saude/pronto")
    assert resposta.status_code == 503
    assert 1.5 < time.monotonic() - inicio < 2.9
