"""Autenticacao (JWT RS256 do OS Service) e RBAC das rotas (ADR-039).

Papeis: ``admin`` pode tudo; ``mecanico`` opera diagnostico e execucao e le
fila/estoque; ``atendente`` le fila/estoque. Toda falha de credencial responde
o mesmo 401 (o motivo vai so para o log); 403 e so papel valido sem permissao.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final
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
CREDENCIAL_INVALIDA: Final = "Credencial ausente, invalida ou expirada"
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
    # Header recebido, repassado ao Billing na validacao de itens (RFC-004 sec. 6).
    authorization: str = field(repr=False)


def _nao_autenticado(motivo: str) -> HTTPException:
    # Mesma resposta para toda falha de credencial: quem testa tokens nao
    # descobre o que esta errado. O motivo fica so no log (sem o token).
    _log.info("authentication_failed", motivo=motivo)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=CREDENCIAL_INVALIDA,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _validar(request: Request, token: str) -> dict[str, object]:
    validador: ValidadorDeTokenJWKS = request.app.state.validador_token
    try:
        return validador.validar(token)
    except TokenExpiradoError:
        raise _nao_autenticado("token_expirado") from None
    except TokenInvalidoError:
        raise _nao_autenticado("token_invalido") from None
    except JwksIndisponivelError as exc:
        _log.warning("jwks_unavailable", retry_after=exc.retry_after)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_JWKS_INDISPONIVEL,
            headers={"Retry-After": str(exc.retry_after)},
        ) from None


def obter_usuario_autenticado(
    request: Request,
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UsuarioAutenticado:
    """Usuario do bearer token emitido pelo OS Service.

    401 uniforme para credencial ausente, invalida ou expirada, ``type``
    diferente de ``access`` (refresh token nao autentica requisicao), ``sub``
    que nao e UUID e ``papel`` ausente ou desconhecido. 503 com ``Retry-After``
    quando o JWKS nao responde e nao ha chave em cache.
    """
    if credenciais is None:
        raise _nao_autenticado("token_ausente")
    claims = _validar(request, credenciais.credentials)
    if claims.get("type") != "access":
        raise _nao_autenticado("tipo_nao_access")
    try:
        usuario_id = UUID(str(claims["sub"]))
    except ValueError:
        raise _nao_autenticado("sub_invalido") from None
    try:
        papel = Papel(str(claims.get("papel")))
    except ValueError:
        raise _nao_autenticado("papel_invalido") from None
    return UsuarioAutenticado(
        id=usuario_id,
        papel=papel,
        authorization=f"Bearer {credenciais.credentials}",
    )


def exigir_papel(*papeis: Papel) -> Callable[..., UsuarioAutenticado]:
    """Dependency de RBAC: 403 para papel valido fora da lista; ``admin`` passa."""
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
