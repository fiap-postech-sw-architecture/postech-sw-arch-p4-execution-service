from __future__ import annotations

from sqlalchemy import Column, DateTime, Enum, Index, Table, Uuid

from src.compartilhado.infraestrutura.database import mapper_registry, metadata
from src.compartilhado.infraestrutura.tipos_sqlalchemy import (
    check_de_enum,
    retrato_do_veiculo,
)
from src.execucao.dominio.execucao import Execucao, Prioridade, StatusExecucao

execucoes_table = Table(
    "execucoes",
    metadata,
    Column("ordem_id", Uuid, primary_key=True),
    Column(
        "status", Enum(StatusExecucao, native_enum=False, length=20), nullable=False
    ),
    # Grava o valor do contrato ("normal"/"alta"), nao o nome do membro.
    Column(
        "prioridade",
        Enum(
            Prioridade,
            native_enum=False,
            length=10,
            values_callable=lambda prioridades: [p.value for p in prioridades],
        ),
        nullable=False,
    ),
    Column("enfileirada_em", DateTime(timezone=True), nullable=False),
    # Copia do retrato do diagnostico; nula na lapide e sem diagnostico.
    Column("veiculo", retrato_do_veiculo(), nullable=True),
    Column("mecanico_id", Uuid, nullable=True),
    Column("iniciada_em", DateTime(timezone=True), nullable=True),
    Column("finalizada_em", DateTime(timezone=True), nullable=True),
    Column("cancelada_em", DateTime(timezone=True), nullable=True),
    # Id do AgendarExecucao; nulo na lapide.
    Column("agendamento_id", Uuid, nullable=True),
    check_de_enum("status", StatusExecucao, "ck_execucoes_status"),
    check_de_enum("prioridade", Prioridade, "ck_execucoes_prioridade"),
)

# AnonimizarVeiculo acha as copias do retrato do veiculo sem varrer a tabela.
Index("ix_execucoes_veiculo_id", execucoes_table.c.veiculo["veiculo_id"].astext)
# GET /fila e a posicao filtram por status (so AGUARDANDO esta na fila).
Index("ix_execucoes_status", execucoes_table.c.status)

mapper_registry.map_imperatively(
    Execucao,
    execucoes_table,
    properties={
        "id": execucoes_table.c.ordem_id,
        "_status": execucoes_table.c.status,
        "_prioridade": execucoes_table.c.prioridade,
        "_enfileirada_em": execucoes_table.c.enfileirada_em,
        "_veiculo": execucoes_table.c.veiculo,
        "_mecanico_id": execucoes_table.c.mecanico_id,
        "_iniciada_em": execucoes_table.c.iniciada_em,
        "_finalizada_em": execucoes_table.c.finalizada_em,
        "_cancelada_em": execucoes_table.c.cancelada_em,
        "_agendamento_id": execucoes_table.c.agendamento_id,
    },
)
