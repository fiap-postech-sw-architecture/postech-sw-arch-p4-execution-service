from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog

# Runtime import: com a annotation so como string, o FastAPI nao reconheceria
# `request` e o trataria como query param obrigatorio.
from starlette.requests import Request  # noqa: TC002

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.orm import Session


def obter_session(request: Request) -> Generator[Session]:
    """Abre uma sessao por request (factory criada no lifespan) e a fecha no fim."""
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


async def correlacionar_pela_ordem(  # NOSONAR - async de proposito (S7503)
    request: Request,
) -> None:
    """``correlation_id`` = ``ordem_id`` da rota em toda linha de log do request.

    Chave de busca da saga no Loki (ADR-043). ``async`` de proposito: roda no
    contexto do request, o mesmo dos handlers de erro; numa dependency sync o
    bind ficaria so na copia do contexto da thread. Id fora do formato fica de
    fora (a rota responde 422).
    """
    bruto = request.path_params.get("ordem_id")
    if bruto is None:
        return
    try:
        ordem_id = UUID(str(bruto))
    except ValueError:
        return
    structlog.contextvars.bind_contextvars(correlation_id=str(ordem_id))
