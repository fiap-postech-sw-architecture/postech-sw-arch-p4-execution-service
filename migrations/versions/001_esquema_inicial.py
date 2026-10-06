"""esquema inicial: estoque, reservas, diagnosticos, execucoes e outbox

Revision ID: 001
Revises:
Create Date: 2026-10-06 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def _timestamp(nome: str, *, nullable: bool = True) -> sa.Column[object]:
    return sa.Column(nome, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "itens_estoque",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("nome", sa.String(length=255), nullable=False),
        sa.Column("quantidade_disponivel", sa.Integer(), nullable=False),
        sa.Column("quantidade_reservada", sa.Integer(), nullable=False),
        sa.Column("ativo", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku", name="uq_itens_estoque_sku"),
        sa.CheckConstraint(
            "quantidade_disponivel >= 0",
            name="ck_itens_estoque_disponivel_nao_negativa",
        ),
        sa.CheckConstraint(
            "quantidade_reservada >= 0 "
            "AND quantidade_reservada <= quantidade_disponivel",
            name="ck_itens_estoque_reservada_dentro_do_disponivel",
        ),
    )
    op.create_table(
        "reservas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ordem_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("itens", postgresql.JSONB(), nullable=False),
        sa.Column("faltantes", postgresql.JSONB(), nullable=False),
        _timestamp("criada_em", nullable=False),
        _timestamp("encerrada_em"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ordem_id", name="uq_reservas_ordem_id"),
    )
    op.create_table(
        "diagnosticos",
        sa.Column("ordem_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("veiculo", postgresql.JSONB(), nullable=False),
        sa.Column("descricao_problema", sa.Text(), nullable=False),
        sa.Column("mecanico_id", sa.Uuid(), nullable=True),
        sa.Column("itens", postgresql.JSONB(), nullable=False),
        sa.Column("observacoes", sa.Text(), nullable=False),
        _timestamp("solicitado_em", nullable=False),
        _timestamp("iniciado_em"),
        _timestamp("concluido_em"),
        _timestamp("descartado_em"),
        sa.PrimaryKeyConstraint("ordem_id"),
    )
    op.create_index(
        "ix_diagnosticos_status_solicitado_em",
        "diagnosticos",
        ["status", "solicitado_em"],
    )
    op.create_table(
        "execucoes",
        sa.Column("ordem_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("prioridade", sa.Integer(), nullable=False),
        _timestamp("enfileirada_em", nullable=False),
        sa.Column("mecanico_id", sa.Uuid(), nullable=True),
        _timestamp("iniciada_em"),
        _timestamp("finalizada_em"),
        _timestamp("cancelada_em"),
        sa.PrimaryKeyConstraint("ordem_id"),
    )
    op.create_index("ix_execucoes_status", "execucoes", ["status"])
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("mensagem_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=100), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        _timestamp("ocorrido_em", nullable=False),
        sa.Column("dados", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pendente"
        ),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "proxima_tentativa_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        _timestamp("entregue_em"),
        sa.Column("ultimo_erro", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mensagem_id", name="uq_outbox_mensagem_id"),
    )
    op.create_index("ix_outbox_claim", "outbox", ["status", "proxima_tentativa_em"])
    op.create_index(
        "ix_outbox_correlacao", "outbox", ["correlation_id", "id", "status"]
    )


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("execucoes")
    op.drop_table("diagnosticos")
    op.drop_table("reservas")
    op.drop_table("itens_estoque")
