from __future__ import annotations

from typing import Annotated
from uuid import UUID  # noqa: TC003 - path param resolvido em runtime

from fastapi import APIRouter, Depends, Query

# Runtime import: o FastAPI resolve `Annotated[Session, Depends(...)]` em runtime.
from sqlalchemy.orm import Session

from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    exigir_papel,
)
from src.compartilhado.interfaces.dependencies import obter_session
from src.compartilhado.interfaces.schemas import respostas
from src.execucao.interfaces.dependencies import (
    obter_fila,
    obter_finalizar_execucao,
    obter_iniciar_execucao,
)
from src.execucao.interfaces.schemas import (
    ExecucaoResponse,
    FilaResponse,
    ItemDaFilaResponse,
)

router = APIRouter(prefix="/api/v1", tags=["Execucao"], responses=respostas(401, 403))

SessionDep = Annotated[Session, Depends(obter_session)]
Mecanico = Annotated[UsuarioAutenticado, Depends(exigir_papel(Papel.MECANICO))]


@router.get(
    "/fila",
    summary="Fila de execucao com a posicao de cada ordem",
    dependencies=[Depends(exigir_papel(Papel.MECANICO, Papel.ATENDENTE))],
)
def listar_fila(
    session: SessionDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> FilaResponse:
    """Ordens AGUARDANDO: ``alta`` antes de ``normal``, depois por chegada."""
    fila = obter_fila(session)
    return FilaResponse(
        items=[
            ItemDaFilaResponse.model_validate(item)
            for item in fila.listar(offset=offset, limit=limit)
        ],
        total=fila.contar(),
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

    Repetir pelo mesmo mecanico e idempotente.
    """
    execucao = obter_iniciar_execucao(session).executar(ordem_id, usuario.id)
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
    execucao = obter_finalizar_execucao(session).executar(
        ordem_id, usuario.id, pelo_admin=usuario.papel is Papel.ADMIN
    )
    return ExecucaoResponse.model_validate(execucao)
