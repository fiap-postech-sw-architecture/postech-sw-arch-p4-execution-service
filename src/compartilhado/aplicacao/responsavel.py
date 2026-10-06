"""Quem responde por uma acao do mecanico e o rastro das acoes do admin."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from uuid import UUID

_log = structlog.get_logger(__name__)


def responsavel_efetivo(
    responsavel_atual: UUID | None, usuario_id: UUID, *, pelo_admin: bool
) -> UUID:
    """Quem conclui ou finaliza: o proprio mecanico ou, pelo admin, quem iniciou.

    O admin (pode tudo) age em nome do mecanico responsavel sem trocar o
    ``mecanico_id``. Sem responsavel ainda (nada iniciado), vale o proprio id e
    a guarda do agregado recusa a transicao.
    """
    if pelo_admin and responsavel_atual is not None:
        return responsavel_atual
    return usuario_id


def registrar_auditoria(
    acao: str, *, ator_id: UUID, alvo: str, **contexto: str
) -> None:
    """Log de auditoria de acao privilegiada (RFC-004, secao 8): quem, o que, onde.

    So identificadores: nada de texto livre nem dado do cliente.
    """
    _log.info("audit", acao=acao, ator_id=str(ator_id), alvo=alvo, **contexto)
