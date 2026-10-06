from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import pytest
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from src.compartilhado.dominio.exceptions import (
    DadosInvalidosException,
    DependenciaIndisponivelException,
    DomainException,
    EntidadeDuplicadaException,
    EntidadeNaoEncontradaException,
    EstoqueInsuficienteException,
    OperacaoNaoPermitidaException,
    TransicaoStatusInvalidaException,
    ValorInvalidoError,
    ViolacaoRegraDeNegocioException,
)
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.interfaces.error_handler import registrar_error_handlers
from src.compartilhado.interfaces.middleware import SecurityHeadersMiddleware
from src.diagnostico.dominio.exceptions import ItensInvalidosException

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Corpo(BaseModel):
    nome: str = Field(max_length=5)


def _app_com_excecao(exc: Exception) -> TestClient:
    app = FastAPI()
    registrar_error_handlers(app)

    @app.get("/test")
    def _endpoint() -> None:
        raise exc

    @app.post("/corpo")
    def _corpo(corpo: _Corpo) -> dict[str, str]:
        return {"nome": corpo.nome}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("exc", "status_code"),
    [
        pytest.param(EntidadeNaoEncontradaException(), 404, id="nao-encontrada"),
        pytest.param(ViolacaoRegraDeNegocioException(), 409, id="violacao-regra"),
        pytest.param(TransicaoStatusInvalidaException(), 409, id="transicao"),
        pytest.param(EstoqueInsuficienteException(), 409, id="estoque"),
        pytest.param(EntidadeDuplicadaException(), 409, id="duplicada"),
        pytest.param(OperacaoNaoPermitidaException(), 403, id="nao-permitida"),
        pytest.param(DadosInvalidosException("x"), 422, id="dados-invalidos"),
        pytest.param(DependenciaIndisponivelException("x"), 503, id="dependencia"),
        # Subclasse sem entrada propria resolve pelo ancestral (MRO).
        pytest.param(ItensInvalidosException("x"), 422, id="subclasse-mro"),
        pytest.param(DomainException(codigo="X", mensagem="y"), 409, id="default"),
    ],
)
def test_excecao_de_dominio_vira_envelope(
    exc: DomainException, status_code: int
) -> None:
    resposta = _app_com_excecao(exc).get("/test")
    assert resposta.status_code == status_code
    assert resposta.json() == {
        "erro": {
            "codigo": exc.codigo,
            "mensagem": exc.mensagem,
            "id_requisicao": "desconhecido",
        }
    }


def test_request_id_do_middleware_vai_para_o_envelope() -> None:
    app = FastAPI()
    registrar_error_handlers(app)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/test")
    def _endpoint() -> None:
        raise EntidadeNaoEncontradaException()

    resposta = TestClient(app).get("/test", headers={"X-Request-ID": "req-123"})
    assert resposta.json()["erro"]["id_requisicao"] == "req-123"


def test_http_exception_mantem_status_e_headers_no_envelope() -> None:
    exc = HTTPException(
        401, detail="Token expirado", headers={"WWW-Authenticate": "Bearer"}
    )
    resposta = _app_com_excecao(exc).get("/test")
    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == "Bearer"
    assert resposta.json()["erro"] == {
        "codigo": "NAO_AUTENTICADO",
        "mensagem": "Token expirado",
        "id_requisicao": "desconhecido",
    }


def test_rota_inexistente_responde_em_portugues() -> None:
    resposta = _app_com_excecao(RuntimeError()).get("/nao-existe")
    assert resposta.status_code == 404
    assert resposta.json()["erro"]["codigo"] == "RECURSO_NAO_ENCONTRADO"
    assert resposta.json()["erro"]["mensagem"] == "Recurso nao encontrado"


def test_metodo_nao_permitido() -> None:
    resposta = _app_com_excecao(RuntimeError()).delete("/test")
    assert resposta.status_code == 405
    assert resposta.json()["erro"]["codigo"] == "METODO_NAO_PERMITIDO"


def test_http_exception_de_status_sem_codigo_proprio() -> None:
    resposta = _app_com_excecao(HTTPException(418, detail="Bule")).get("/test")
    assert resposta.status_code == 418
    assert resposta.json()["erro"]["codigo"] == "ERRO_HTTP"


def test_validacao_de_schema_nao_ecoa_o_valor_recebido() -> None:
    resposta = _app_com_excecao(RuntimeError()).post(
        "/corpo", json={"nome": "joao@example.com"}
    )
    assert resposta.status_code == 422
    assert "joao@example.com" not in resposta.text
    corpo = resposta.json()
    assert corpo["id_requisicao"] == "desconhecido"
    assert set(corpo["detail"][0]) == {"type", "loc", "msg"}


def test_valor_invalido_vira_422_com_pii_redigida() -> None:
    resposta = _app_com_excecao(
        ValorInvalidoError("CPF invalido: 123.456.789-00 (contato joao@example.com)")
    ).get("/test")
    assert resposta.status_code == 422
    assert resposta.json()["erro"]["codigo"] == "VALOR_INVALIDO"
    assert "123.456.789-00" not in resposta.text
    assert "joao@example.com" not in resposta.text


def test_value_error_de_biblioteca_e_defeito_do_servidor() -> None:
    # Ex.: ValidationError do Pydantic ao montar a resposta (subclasse de
    # ValueError): nao e culpa do cliente e nao pode ecoar o valor interno.
    resposta = _app_com_excecao(ValueError("input_value='interno'")).get("/test")
    assert resposta.status_code == 500
    assert resposta.json()["erro"]["codigo"] == "ERRO_INTERNO"
    assert "interno'" not in resposta.text


def test_excecao_generica_vira_500_sem_detalhe_interno() -> None:
    resposta = _app_com_excecao(RuntimeError("segredo interno")).get("/test")
    assert resposta.status_code == 500
    assert resposta.json()["erro"]["codigo"] == "ERRO_INTERNO"
    assert "segredo interno" not in resposta.text


@pytest.fixture
def pipeline_buffer() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    handlers_anteriores = root.handlers[:]
    nivel_anterior = root.level
    config_anterior = structlog.get_config()
    buffer = io.StringIO()
    configurar_logging(stream=buffer)
    try:
        yield buffer
    finally:
        root.handlers = handlers_anteriores
        root.setLevel(nivel_anterior)
        structlog.configure(**config_anterior)


def test_negacao_de_dominio_loga_so_o_codigo(pipeline_buffer: io.StringIO) -> None:
    _app_com_excecao(EntidadeNaoEncontradaException("ordem do joao@x.com")).get("/test")
    log = pipeline_buffer.getvalue()
    assert "domain_exception_handled" in log
    assert "ENTIDADE_NAO_ENCONTRADA" in log
    assert "joao@x.com" not in log


def test_handler_500_mascara_pii_no_traceback(pipeline_buffer: io.StringIO) -> None:
    _app_com_excecao(RuntimeError("falha com CPF 123.456.789-00")).get("/test")
    log = pipeline_buffer.getvalue()
    assert "internal_error" in log
    assert "123.456.789-00" not in log
