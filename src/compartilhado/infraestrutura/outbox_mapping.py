"""Tabelas ``outbox`` (Transactional Outbox, padrao do p3) e ``mensagens_processadas``.

Cada linha da outbox e uma mensagem do catalogo da saga pronta para o relay: o
``envelope`` (RFC-004, secao 5.2) ja conferido contra o contrato, o destino
(``exchange`` e ``routing_key``) e o contexto W3C (``traceparent`` e
``tracestate``) de quem gravou, para o relay publicar como filho dele.
``mensagem_id``, ``tipo`` e ``correlation_id`` repetem o envelope para indice,
metrica e ordem por saga; ``id`` bigserial da a ordem global de publicacao. As
colunas de controle (``status``, ``tentativas``, ``proxima_tentativa_em``,
``entregue_em``, ``ultimo_erro``) sao as do relay do p3, que acorda pelo
``NOTIFY`` em ``CANAL_NOTIFY``.

``mensagens_processadas`` guarda o ``id`` de cada comando consumido, gravado pelo
consumidor na mesma transacao do efeito: a reentrega do mesmo ``id`` recebe ack
sem efeito.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from src.compartilhado.infraestrutura.database import metadata

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection

CANAL_NOTIFY = "outbox_novo"

outbox_table = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("mensagem_id", Uuid, nullable=False),
    Column("tipo", String(100), nullable=False),
    Column("correlation_id", Uuid, nullable=False),
    Column("status", String(20), nullable=False, server_default="pendente"),
    Column("tentativas", Integer, nullable=False, server_default="0"),
    Column(
        "proxima_tentativa_em",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "criado_em", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("entregue_em", DateTime(timezone=True), nullable=True),
    Column("ultimo_erro", Text, nullable=True),
    Column("exchange", String(255), nullable=False),
    Column("routing_key", String(255), nullable=False),
    Column("envelope", JSONB, nullable=False),
    Column("traceparent", Text, nullable=True),
    Column("tracestate", Text, nullable=True),
    UniqueConstraint("mensagem_id", name="uq_outbox_mensagem_id"),
)

# Claim do relay: pendentes cuja proxima tentativa ja venceu.
Index("ix_outbox_claim", outbox_table.c.status, outbox_table.c.proxima_tentativa_em)
# Ordem por saga: o relay so publica uma mensagem quando as anteriores da mesma
# ordem (correlation_id) ja sairam.
Index(
    "ix_outbox_correlacao",
    outbox_table.c.correlation_id,
    outbox_table.c.id,
    outbox_table.c.status,
)

mensagens_processadas_table = Table(
    "mensagens_processadas",
    metadata,
    Column("mensagem_id", Uuid, primary_key=True),
    Column(
        "processada_em",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

# Retencao: o consumidor apaga as linhas com mais de 30 dias.
Index(
    "ix_mensagens_processadas_processada_em",
    mensagens_processadas_table.c.processada_em,
)


def registrar_processada(conexao: Connection, mensagem_id: UUID) -> bool:
    """Grava o ``id`` do comando como processado; False se ja estava (reentrega).

    ``ON CONFLICT DO NOTHING``: uma entrega simultanea do mesmo comando espera
    a transacao desta na chave primaria e recebe False, sem estourar a PK.
    """
    inserida = conexao.execute(
        insert(mensagens_processadas_table)
        .values(mensagem_id=mensagem_id)
        .on_conflict_do_nothing(index_elements=["mensagem_id"])
        .returning(mensagens_processadas_table.c.mensagem_id)
    ).first()
    return inserida is not None
