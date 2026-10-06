"""Tabela ``outbox`` (Transactional Outbox, padrao do p3).

Cada linha e uma mensagem do catalogo da saga ja no formato do envelope do
RFC-004 (secao 5.2): ``mensagem_id`` (``id``/``message_id``), ``tipo``,
``correlation_id`` (= ``ordem_id``), ``ocorrido_em`` e ``dados``. ``id``
bigserial da a ordem global de publicacao; as colunas de controle
(``status``, ``tentativas``, ``proxima_tentativa_em``, ``entregue_em``,
``ultimo_erro``) sao as do relay do p3, que o PR de mensageria porta para o
RabbitMQ. O relay acorda pelo ``NOTIFY`` em ``CANAL_NOTIFY``.
"""

from __future__ import annotations

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
from sqlalchemy.dialects.postgresql import JSONB

from src.compartilhado.infraestrutura.database import metadata

CANAL_NOTIFY = "outbox_novo"

outbox_table = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("mensagem_id", Uuid, nullable=False),
    Column("tipo", String(100), nullable=False),
    Column("correlation_id", Uuid, nullable=False),
    Column("ocorrido_em", DateTime(timezone=True), nullable=False),
    Column("dados", JSONB, nullable=False),
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
