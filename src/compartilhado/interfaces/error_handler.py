from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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
from src.compartilhado.infraestrutura.logging import redigir_pii_erro

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request

logger = structlog.get_logger(__name__)

_EXCEPTION_STATUS_MAP: dict[type[DomainException], int] = {
    EntidadeNaoEncontradaException: 404,
    ViolacaoRegraDeNegocioException: 409,
    TransicaoStatusInvalidaException: 409,
    EstoqueInsuficienteException: 409,
    EntidadeDuplicadaException: 409,
    OperacaoNaoPermitidaException: 403,
    DadosInvalidosException: 422,
    DependenciaIndisponivelException: 503,
}

# DomainException fora do mapa e, por definicao, regra de negocio violada.
_STATUS_DEFAULT = 409

# Erros HTTP levantados pelo framework ou pela autenticacao (HTTPException).
_CODIGOS_HTTP: dict[int, str] = {
    401: "NAO_AUTENTICADO",
    403: "ACESSO_NEGADO",
    404: "RECURSO_NAO_ENCONTRADO",
    405: "METODO_NAO_PERMITIDO",
    503: "SERVICO_INDISPONIVEL",
}
# O roteamento do Starlette usa a frase HTTP em ingles como detail ("Not Found");
# a API responde em portugues.
_MENSAGENS_PADRAO: dict[int, str] = {
    404: "Recurso nao encontrado",
    405: "Metodo nao permitido para este recurso",
}


def _status_para(exc: DomainException) -> int:
    """Resolve o status pela hierarquia (MRO): a subclasse mais especifica vence."""
    for classe in type(exc).__mro__:
        code = _EXCEPTION_STATUS_MAP.get(classe)
        if code is not None:
            return code
    return _STATUS_DEFAULT


def _obter_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "desconhecido")


def _criar_envelope(codigo: str, mensagem: str, request_id: str) -> dict[str, object]:
    return {
        "erro": {
            "codigo": codigo,
            "mensagem": mensagem,
            "id_requisicao": request_id,
        }
    }


def _mensagem_http(exc: StarletteHTTPException) -> str:
    detalhe = str(exc.detail)
    if detalhe == HTTPStatus(exc.status_code).phrase:
        return _MENSAGENS_PADRAO.get(exc.status_code, detalhe)
    return detalhe


def registrar_error_handlers(app: FastAPI) -> None:
    """Mapeia excecoes para o envelope ``{erro: {codigo, mensagem, id_requisicao}}``.

    DomainException vira 403/404/409/422/503 pelo mapa; HTTPException
    (autenticacao, rota inexistente) mantem o status e os headers;
    ``ValorInvalidoError`` (invariante de value object) vira 422 VALOR_INVALIDO;
    o resto, inclusive ``ValueError`` de biblioteca, vira 500 com traceback no
    log. O 422 de schema do FastAPI mantem o formato do p3 (``detail`` +
    ``id_requisicao``).
    """

    @app.exception_handler(DomainException)
    async def _domain_exception_handler(
        request: Request, exc: DomainException
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        status_code = _status_para(exc)
        # So o codigo estavel vai para o log, nunca a mensagem (pode ter dado
        # do request).
        logger.warning(
            "domain_exception_handled",
            codigo=exc.codigo,
            status=status_code,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=status_code,
            content=_criar_envelope(exc.codigo, exc.mensagem, request_id),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        codigo = _CODIGOS_HTTP.get(exc.status_code, "ERRO_HTTP")
        logger.warning(
            "http_exception_handled",
            codigo=codigo,
            status=exc.status_code,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_criar_envelope(codigo, _mensagem_http(exc), request_id),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # O detail default ecoa o `input` cru de cada campo invalido (PII de um
        # campo malformado voltaria no corpo): cada item carrega so type/loc/msg.
        request_id = _obter_request_id(request)
        detalhes = [
            {"type": erro.get("type"), "loc": erro.get("loc"), "msg": erro.get("msg")}
            for erro in exc.errors()
        ]
        logger.warning(
            "request_validation_handled",
            request_id=request_id,
            erros=[(d["type"], d["loc"]) for d in detalhes],
        )
        return JSONResponse(
            status_code=422,
            content={"detail": detalhes, "id_requisicao": request_id},
        )

    @app.exception_handler(ValorInvalidoError)
    async def _valor_invalido_handler(
        request: Request, exc: ValorInvalidoError
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        logger.warning("invalid_value_handled", request_id=request_id, exc_info=exc)
        # str(exc) pode ecoar o valor recebido: redige PII antes de devolver.
        return JSONResponse(
            status_code=422,
            content=_criar_envelope(
                "VALOR_INVALIDO", redigir_pii_erro(str(exc)), request_id
            ),
        )

    @app.exception_handler(Exception)
    async def _generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        logger.exception("internal_error", request_id=request_id)
        return JSONResponse(
            status_code=500,
            content=_criar_envelope(
                "ERRO_INTERNO", "Erro interno do servidor", request_id
            ),
        )
