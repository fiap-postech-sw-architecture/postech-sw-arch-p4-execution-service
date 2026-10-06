"""Autenticacao (JWT RS256 do OS Service) e RBAC das rotas.

Papeis (brief secao 7): ``admin`` pode tudo; ``mecanico`` opera diagnostico e
execucao e le fila/estoque; ``atendente`` le fila/estoque.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Runtime import: o FastAPI resolve as annotations das dependencies em runtime.
from starlette.requests import Request  # noqa: TC002

from src.compartilhado.infraestrutura.jwks import (
    JwksIndisponivelError,
    TokenExpiradoError,
    TokenInvalidoError,
    ValidadorDeTokenJWKS,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_log = structlog.get_logger(__name__)
_bearer = HTTPBearer(auto_error=False)
_JWKS_INDISPONIVEL = (
    "Validacao de token indisponivel: o JWKS do OS Service nao respondeu. "
    "Tente novamente em instantes."
)


class Papel(StrEnum):
    ADMIN = "admin"
    ATENDENTE = "atendente"
    MECANICO = "mecanico"


@dataclass(frozen=True, slots=True)
class UsuarioAutenticado:
    id: UUID
    papel: Papel
    # Header recebido, repassado ao Billing na validacao de itens (brief secao 5).
    authorization: str = field(repr=False)


def _nao_autenticado(mensagem: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=mensagem,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _validar(request: Request, token: str) -> dict[str, object]:
    validador: ValidadorDeTokenJWKS = request.app.state.validador_token
    try:
        return validador.validar(token)
    except TokenExpiradoError:
        raise _nao_autenticado("Token expirado") from None
    except TokenInvalidoError:
        raise _nao_autenticado("Token invalido") from None
    except JwksIndisponivelError:
        _log.warning("jwks_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_JWKS_INDISPONIVEL,
        ) from None


def obter_usuario_autenticado(
    request: Request,
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UsuarioAutenticado:
    if credenciais is None:
        raise _nao_autenticado("Token de autenticacao nao fornecido")
    claims = _validar(request, credenciais.credentials)
    # Defesa em profundidade: um refresh token (type=refresh, padrao do p3) nao
    # autentica requisicao. Token sem `type` e tratado como access.
    if claims.get("type", "access") != "access":
        raise _nao_autenticado("Token nao e do tipo access")
    try:
        usuario_id = UUID(str(claims["sub"]))
    except ValueError:
        raise _nao_autenticado("Token sem identificador de usuario valido") from None
    try:
        papel = Papel(str(claims.get("papel")))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Papel nao autorizado"
        ) from None
    return UsuarioAutenticado(
        id=usuario_id,
        papel=papel,
        authorization=f"Bearer {credenciais.credentials}",
    )


def exigir_papel(*papeis: Papel) -> Callable[..., UsuarioAutenticado]:
    """Dependency de RBAC; ``admin`` sempre passa."""
    permitidos = frozenset({Papel.ADMIN, *papeis})

    def verificar(
        usuario: Annotated[UsuarioAutenticado, Depends(obter_usuario_autenticado)],
    ) -> UsuarioAutenticado:
        if usuario.papel not in permitidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Papel nao autorizado para esta operacao",
            )
        return usuario

    return verificar
