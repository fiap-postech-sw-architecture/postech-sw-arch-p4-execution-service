from __future__ import annotations

from typing import Any

from sqlalchemy import Column, DateTime, Enum, Index, Table, Text, Uuid

from src.compartilhado.infraestrutura.database import mapper_registry, metadata
from src.compartilhado.infraestrutura.tipos_sqlalchemy import (
    JsonDeDominio,
    check_de_enum,
    retrato_do_veiculo,
)
from src.diagnostico.dominio.diagnostico import (
    Diagnostico,
    ItemDiagnostico,
    StatusDiagnostico,
    TipoItem,
)


def _itens_para_json(itens: tuple[ItemDiagnostico, ...]) -> list[dict[str, Any]]:
    return [
        {"tipo": item.tipo.value, "codigo": item.codigo, "quantidade": item.quantidade}
        for item in itens
    ]


def _itens_de_json(dados: list[dict[str, Any]]) -> tuple[ItemDiagnostico, ...]:
    return tuple(
        ItemDiagnostico(
            tipo=TipoItem(item["tipo"]),
            codigo=item["codigo"],
            quantidade=item["quantidade"],
        )
        for item in dados
    )


diagnosticos_table = Table(
    "diagnosticos",
    metadata,
    Column("ordem_id", Uuid, primary_key=True),
    Column(
        "status", Enum(StatusDiagnostico, native_enum=False, length=20), nullable=False
    ),
    # Anulaveis so na lapide (descarte que chegou antes da solicitacao).
    Column("veiculo", retrato_do_veiculo(), nullable=True),
    Column("descricao_problema", Text, nullable=True),
    Column("mecanico_id", Uuid, nullable=True),
    Column("itens", JsonDeDominio(_itens_para_json, _itens_de_json), nullable=False),
    Column("observacoes", Text, nullable=False),
    Column("solicitado_em", DateTime(timezone=True), nullable=False),
    Column("iniciado_em", DateTime(timezone=True), nullable=True),
    Column("concluido_em", DateTime(timezone=True), nullable=True),
    Column("descartado_em", DateTime(timezone=True), nullable=True),
    # Id do SolicitarDiagnostico; nulo na lapide.
    Column("solicitacao_id", Uuid, nullable=True),
    check_de_enum("status", StatusDiagnostico, "ck_diagnosticos_status"),
)

# AnonimizarVeiculo acha os retratos do veiculo sem varrer a tabela.
Index(
    "ix_diagnosticos_veiculo_id",
    diagnosticos_table.c.veiculo["veiculo_id"].astext,
)
# Fila do mecanico: GET /diagnosticos?status=AGUARDANDO por ordem de chegada.
Index(
    "ix_diagnosticos_status_solicitado_em",
    diagnosticos_table.c.status,
    diagnosticos_table.c.solicitado_em,
)

mapper_registry.map_imperatively(
    Diagnostico,
    diagnosticos_table,
    properties={
        "id": diagnosticos_table.c.ordem_id,
        "_status": diagnosticos_table.c.status,
        "_veiculo": diagnosticos_table.c.veiculo,
        "_descricao_problema": diagnosticos_table.c.descricao_problema,
        "_mecanico_id": diagnosticos_table.c.mecanico_id,
        "_itens": diagnosticos_table.c.itens,
        "_observacoes": diagnosticos_table.c.observacoes,
        "_solicitado_em": diagnosticos_table.c.solicitado_em,
        "_iniciado_em": diagnosticos_table.c.iniciado_em,
        "_concluido_em": diagnosticos_table.c.concluido_em,
        "_descartado_em": diagnosticos_table.c.descartado_em,
        "_solicitacao_id": diagnosticos_table.c.solicitacao_id,
    },
)
