from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

from src.compartilhado.interfaces.error_handler import resposta_erro_interno

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

_log = structlog.get_logger(__name__)

_CSP_DEFAULT = "default-src 'none'"
# Swagger UI / ReDoc dependem de scripts e estilos inline que o CSP bloquearia.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

# X-Request-ID vindo da borda (Kong, plugin correlation-id) e aceito quando
# "sano": ate 128 chars de um charset seguro para logs e headers. Qualquer outra
# coisa (vazio, longo, CRLF, unicode) e trocada por um uuid4 novo.
_REQUEST_ID_EXTERNO_VALIDO = re.compile(r"[A-Za-z0-9._=-]{1,128}")


def _caminho_de_docs(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _DOCS_PATHS)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Propaga o X-Request-ID (logs e envelope de erro) e anexa headers de seguranca.

    Inclusive no 500 de erro nao tratado: o handler de ``Exception`` do app roda
    no ``ServerErrorMiddleware``, por fora deste, e o 500 sairia sem os headers;
    por isso a excecao vira aqui o 500 do envelope (``resposta_erro_interno``).
    Tambem e o access log estruturado (metodo, caminho, status, duracao e
    ``request_id``), no lugar da linha de texto do uvicorn (``--no-access-log``).
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        recebido = request.headers.get("X-Request-ID", "")
        request_id = (
            recebido if _REQUEST_ID_EXTERNO_VALIDO.fullmatch(recebido) else str(uuid4())
        )
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        inicio = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - vira o 500 do envelope
            response = resposta_erro_interno(request, exc)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["Cache-Control"] = "no-store"
        if not _caminho_de_docs(request.url.path):
            response.headers["Content-Security-Policy"] = _CSP_DEFAULT
        response.headers["X-Request-ID"] = request_id
        _log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - inicio) * 1000, 1),
        )
        return response
