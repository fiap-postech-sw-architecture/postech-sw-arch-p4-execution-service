from __future__ import annotations

from typing import Annotated
from uuid import UUID  # noqa: TC003 - path param resolvido em runtime

from fastapi import APIRouter, Depends

# Runtime imports: o FastAPI resolve as annotations das rotas em runtime.
from sqlalchemy.orm import Session
from starlette.requests import Request  # noqa: TC002

from src.compartilhado.aplicacao.responsavel import registrar_auditoria
from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    exigir_papel,
)
from src.compartilhado.interfaces.dependencies import obter_session
from src.compartilhado.interfaces.schemas import Limite, Offset, Pagina, respostas
from src.diagnostico.dominio.diagnostico import ItemDiagnostico, StatusDiagnostico
from src.diagnostico.interfaces.dependencies import (
    obter_concluir_diagnostico,
    obter_iniciar_diagnostico,
    obter_listar_diagnosticos,
)
from src.diagnostico.interfaces.schemas import ConclusaoRequest, DiagnosticoResponse

router = APIRouter(
    prefix="/api/v1/diagnosticos", tags=["Diagnosticos"], responses=respostas(401, 403)
)

SessionDep = Annotated[Session, Depends(obter_session)]
Mecanico = Annotated[UsuarioAutenticado, Depends(exigir_papel(Papel.MECANICO))]


@router.get(
    "",
    summary="Fila de diagnosticos, com filtro opcional de status",
    dependencies=[Depends(exigir_papel(Papel.MECANICO))],
)
def listar_diagnosticos(
    session: SessionDep,
    status: StatusDiagnostico | None = None,
    offset: Offset = 0,
    limit: Limite = 20,
) -> Pagina[DiagnosticoResponse]:
    """Ordem de chegada (mais antigo primeiro); ``?status=AGUARDANDO`` e a fila."""
    diagnosticos, total = obter_listar_diagnosticos(session).executar(
        status, offset=offset, limit=limit
    )
    return Pagina(
        items=[DiagnosticoResponse.model_validate(d) for d in diagnosticos],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/{ordem_id}/inicio",
    summary="Mecanico assume o diagnostico (emite DiagnosticoIniciado)",
    responses=respostas(404, 409),
)
def iniciar_diagnostico(
    ordem_id: UUID, usuario: Mecanico, session: SessionDep
) -> DiagnosticoResponse:
    """O mecanico autenticado vira o responsavel; repetir por ele e idempotente.

    O admin tambem pode assumir (vira o responsavel), com log de auditoria.
    """
    diagnostico = obter_iniciar_diagnostico(session).executar(ordem_id, usuario.id)
    if usuario.papel is Papel.ADMIN:
        registrar_auditoria(
            "iniciar_diagnostico", ator_id=usuario.id, alvo=str(ordem_id)
        )
    return DiagnosticoResponse.model_validate(diagnostico)


@router.post(
    "/{ordem_id}/conclusao",
    summary="Registra servicos e pecas (emite DiagnosticoConcluido)",
    responses=respostas(404, 409, 422, 502, 503),
)
def concluir_diagnostico(
    ordem_id: UUID,
    body: ConclusaoRequest,
    request: Request,
    usuario: Mecanico,
    session: SessionDep,
) -> DiagnosticoResponse:
    """Valida pecas no estoque local e codigos no Billing antes de concluir.

    422 lista os codigos recusados; 503 quando o Billing nao responde (ou o
    circuito esta aberto); 502 quando ele recusa o pedido (4xx). So o mecanico
    que iniciou conclui (o admin conclui em nome dele). Repetir devolve o
    diagnostico ja concluido, sem alterar itens.
    """
    itens = [
        ItemDiagnostico(tipo=item.tipo, codigo=item.codigo, quantidade=item.quantidade)
        for item in body.itens
    ]
    diagnostico = obter_concluir_diagnostico(session, request, usuario).executar(
        ordem_id,
        usuario.id,
        itens,
        body.observacoes,
        pelo_admin=usuario.papel is Papel.ADMIN,
    )
    return DiagnosticoResponse.model_validate(diagnostico)
