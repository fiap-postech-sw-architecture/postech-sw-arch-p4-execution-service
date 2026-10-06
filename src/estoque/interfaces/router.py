from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

# Runtime import: o FastAPI resolve `Annotated[Session, Depends(...)]` em runtime.
from sqlalchemy.orm import Session

from src.compartilhado.aplicacao.responsavel import registrar_auditoria
from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    exigir_papel,
)
from src.compartilhado.interfaces.dependencies import obter_session
from src.compartilhado.interfaces.schemas import Limite, Offset, Pagina, respostas
from src.estoque.dominio.sku import PADRAO_SKU, TAMANHO_MAXIMO_SKU, Sku
from src.estoque.interfaces.dependencies import (
    obter_ajustar_quantidade,
    obter_atualizar_item,
    obter_consultar_item,
    obter_criar_item,
    obter_desativar_item,
    obter_listar_itens,
)
from src.estoque.interfaces.schemas import (
    AjustarQuantidadeRequest,
    AtualizarItemEstoqueRequest,
    CriarItemEstoqueRequest,
    ItemEstoqueResponse,
)

router = APIRouter(
    prefix="/api/v1/estoque", tags=["Estoque"], responses=respostas(401, 403)
)

SessionDep = Annotated[Session, Depends(obter_session)]
SkuPath = Annotated[
    str,
    Path(pattern=PADRAO_SKU, max_length=TAMANHO_MAXIMO_SKU, examples=["PEC-VELA"]),
]
# Escrita so do admin, com log de auditoria (quem, o que, qual SKU).
Admin = Annotated[UsuarioAutenticado, Depends(exigir_papel())]
_LEITURA = [Depends(exigir_papel(Papel.MECANICO, Papel.ATENDENTE))]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Cadastra uma peca no estoque (admin)",
    responses=respostas(409),
)
def criar_item(
    body: CriarItemEstoqueRequest, admin: Admin, session: SessionDep
) -> ItemEstoqueResponse:
    """Cadastra o SKU com o saldo inicial; o mesmo SKU precisa existir no Billing."""
    item = obter_criar_item(session).executar(
        sku=Sku(body.sku),
        nome=body.nome,
        quantidade_disponivel=body.quantidade_disponivel,
    )
    registrar_auditoria("cadastrar_peca", ator_id=admin.id, alvo=body.sku)
    return ItemEstoqueResponse.model_validate(item)


@router.get(
    "", summary="Lista o estoque paginado (ordem de SKU)", dependencies=_LEITURA
)
def listar_itens(
    session: SessionDep, offset: Offset = 0, limit: Limite = 20
) -> Pagina[ItemEstoqueResponse]:
    itens, total = obter_listar_itens(session).executar(offset=offset, limit=limit)
    return Pagina(
        items=[ItemEstoqueResponse.model_validate(item) for item in itens],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{sku}",
    summary="Consulta uma peca pelo SKU",
    dependencies=_LEITURA,
    responses=respostas(404),
)
def obter_item(sku: SkuPath, session: SessionDep) -> ItemEstoqueResponse:
    item = obter_consultar_item(session).executar(Sku(sku))
    return ItemEstoqueResponse.model_validate(item)


@router.put(
    "/{sku}",
    summary="Atualiza nome e situacao (ativo) de uma peca (admin)",
    responses=respostas(404, 409),
)
def atualizar_item(
    sku: SkuPath, body: AtualizarItemEstoqueRequest, admin: Admin, session: SessionDep
) -> ItemEstoqueResponse:
    """Nao mexe em quantidade; desativar exige a peca sem unidades reservadas."""
    item = obter_atualizar_item(session).executar(
        Sku(sku), nome=body.nome, ativo=body.ativo
    )
    registrar_auditoria("atualizar_peca", ator_id=admin.id, alvo=sku)
    return ItemEstoqueResponse.model_validate(item)


@router.patch(
    "/{sku}/quantidade",
    summary="Ajusta o saldo fisico de uma peca (admin)",
    responses=respostas(404, 409),
)
def ajustar_quantidade(
    sku: SkuPath, body: AjustarQuantidadeRequest, admin: Admin, session: SessionDep
) -> ItemEstoqueResponse:
    """Define a quantidade disponivel absoluta (inventario); 409 abaixo do reservado."""
    item = obter_ajustar_quantidade(session).executar(
        Sku(sku), body.quantidade_disponivel
    )
    registrar_auditoria(
        "ajustar_saldo",
        ator_id=admin.id,
        alvo=sku,
        quantidade_disponivel=str(body.quantidade_disponivel),
    )
    return ItemEstoqueResponse.model_validate(item)


@router.delete(
    "/{sku}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desativa uma peca (soft delete, admin)",
    responses=respostas(404, 409),
)
def desativar_item(sku: SkuPath, admin: Admin, session: SessionDep) -> None:
    """A peca sai de novos diagnosticos e reservas; o historico fica."""
    obter_desativar_item(session).executar(Sku(sku))
    registrar_auditoria("desativar_peca", ator_id=admin.id, alvo=sku)
