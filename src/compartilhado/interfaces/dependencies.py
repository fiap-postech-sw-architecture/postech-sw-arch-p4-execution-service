from __future__ import annotations

from typing import TYPE_CHECKING

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
