from __future__ import annotations

from typing import Annotated
from uuid import UUID  # noqa: TC003 - path param resolvido em runtime

from fastapi import APIRouter, Depends

# Runtime import: o FastAPI resolve `Annotated[Session, Depends(...)]` em runtime.
from sqlalchemy.orm import Session

from src.compartilhado.aplicacao.responsavel import registrar_auditoria
from src.compartilhado.infraestrutura.rastreamento import retomando
from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    exigir_papel,
)
from src.compartilhado.interfaces.dependencies import obter_session
from src.compartilhado.interfaces.schemas import Limite, Offset, Pagina, respostas
from src.execucao.infraestrutura.mapping import execucoes_table
from src.execucao.interfaces.dependencies import (
    obter_finalizar_execucao,
    obter_iniciar_execucao,
    obter_listar_fila,
)
from src.execucao.interfaces.schemas import ExecucaoResponse, ItemDaFilaResponse

router = APIRouter(prefix="/api/v1", tags=["Execucao"], responses=respostas(401, 403))

SessionDep = Annotated[Session, Depends(obter_session)]
Mecanico = Annotated[UsuarioAutenticado, Depends(exigir_papel(Papel.MECANICO))]


@router.get(
    "/fila",
    summary="Fila de execucao com a posicao de cada ordem",
    dependencies=[Depends(exigir_papel(Papel.MECANICO, Papel.ATENDENTE))],
)
def listar_fila(
    session: SessionDep, offset: Offset = 0, limit: Limite = 20
) -> Pagina[ItemDaFilaResponse]:
    """Ordens AGUARDANDO: ``alta`` antes de ``normal``, depois por chegada."""
    itens, total = obter_listar_fila(session).executar(offset=offset, limit=limit)
    return Pagina(
        items=[ItemDaFilaResponse.model_validate(item) for item in itens],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/execucoes/{ordem_id}/inicio",
    summary="Mecanico inicia o reparo (emite ExecucaoIniciada, pivot da saga)",
    responses=respostas(404, 409),
)
def iniciar_execucao(
    ordem_id: UUID, usuario: Mecanico, session: SessionDep
) -> ExecucaoResponse:
    """Exige a reserva de pecas ativa (409 sem ela): depois daqui a OS nao cancela.

    Repetir pelo mesmo mecanico e idempotente. O admin tambem pode iniciar (vira
    o responsavel), com log de auditoria.
    """
    with retomando(session, execucoes_table, ordem_id, "iniciar execucao"):
        execucao = obter_iniciar_execucao(session).executar(ordem_id, usuario.id)
    if usuario.papel is Papel.ADMIN:
        registrar_auditoria("iniciar_execucao", ator_id=usuario.id, alvo=str(ordem_id))
    return ExecucaoResponse.model_validate(execucao)


@router.post(
    "/execucoes/{ordem_id}/finalizacao",
    summary="Mecanico finaliza o reparo (baixa do estoque + ExecucaoFinalizada)",
    responses=respostas(404, 409),
)
def finalizar_execucao(
    ordem_id: UUID, usuario: Mecanico, session: SessionDep
) -> ExecucaoResponse:
    """Consome a reserva de pecas da ordem na mesma transacao.

    So o mecanico que iniciou finaliza; o admin finaliza em nome dele.
    """
    with retomando(session, execucoes_table, ordem_id, "finalizar execucao"):
        execucao = obter_finalizar_execucao(session).executar(
            ordem_id, usuario.id, pelo_admin=usuario.papel is Papel.ADMIN
        )
    return ExecucaoResponse.model_validate(execucao)
