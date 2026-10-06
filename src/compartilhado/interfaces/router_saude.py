"""Probes do kubelet: liveness sem dependencias e readiness com o banco."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import structlog
from fastapi import APIRouter
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

# Runtime import: o FastAPI resolve as annotations das rotas em runtime.
from starlette.requests import Request  # noqa: TC002
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

router = APIRouter(tags=["Saude"])
_log = structlog.get_logger(__name__)

# A readiness responde em ate 2 s com o banco fora, preso ou lento.
_TIMEOUT_PRONTO_S: Final = 2.0


@router.get("/api/v1/saude", summary="Liveness (processo de pe, sem dependencias)")
async def saude() -> dict[str, str]:
    """Responde 200 com o processo de pe, sem tocar banco, JWKS ou Billing.

    E o HEALTHCHECK da imagem: dependencia fora nao deve reiniciar o pod (isso
    e papel da readiness, que so o tira do balanceamento). ``async`` de
    proposito (licao do p3): as rotas de negocio sao sync e rodam no
    threadpool; sob carga que satura o pool, uma saude sync ficaria na fila e o
    kubelet reiniciaria o pod justamente no pico.
    """
    return {"status": "ok"}


def _consultar_banco(session_factory: Callable[[], Session]) -> None:
    with session_factory() as sessao:
        sessao.execute(text("SELECT 1"))


@router.get(
    "/api/v1/saude/pronto",
    summary="Readiness (banco respondendo)",
    responses={503: {"description": "Banco inacessivel ou acima de 2 s"}},
)
async def pronto(request: Request) -> JSONResponse:
    """200 so com o banco respondendo ``SELECT 1`` em ate 2 s; senao 503.

    So o banco: JWKS e Billing fora do ar nao tiram o pod do Service (as rotas
    respondem 503 com ``Retry-After`` por conta propria).
    """
    try:
        await asyncio.wait_for(
            run_in_threadpool(_consultar_banco, request.app.state.session_factory),
            timeout=_TIMEOUT_PRONTO_S,
        )
    except Exception as exc:  # noqa: BLE001 - qualquer falha = nao pronto
        _log.warning("readiness_failed", error=type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "indisponivel"})
    return JSONResponse(content={"status": "ok"})
