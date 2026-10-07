"""mensageria: outbox com envelope, destino e contexto W3C; mensagens_processadas

Revision ID: 002
Revises: 001
Create Date: 2026-10-06 18:00:00.000000

A outbox passa a guardar o envelope inteiro do contrato (RFC-004, secao 5.2),
o destino (exchange e routing key) e o ``traceparent``/``tracestate`` de quem
gravou. Linhas gravadas antes desta revisao nunca foram publicadas (nao havia
relay): ganham o envelope montado das colunas antigas, com ``causation_id``
nulo, e seguem para o relay. Diagnostico e execucao guardam o id e o contexto
de trace do comando que abriu o fluxo: causa e pai dos fatos que o mecanico gera
pela API.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | None = None
depends_on: str | None = None

_ENVELOPE_DAS_COLUNAS = """
UPDATE outbox SET
    exchange = 'pytstop.eventos',
    routing_key = 'evento.execucao.'
        || lower(regexp_replace(tipo, '([a-z])([A-Z])', '\\1_\\2', 'g')),
    envelope = jsonb_build_object(
        'id', mensagem_id,
        'tipo', tipo,
        'versao', 1,
        'origem', 'execution-service',
        'correlation_id', correlation_id,
        'causation_id', NULL,
        'ocorrido_em', to_jsonb(ocorrido_em),
        'dados', dados
    )
"""

_COLUNAS_DO_ENVELOPE = """
UPDATE outbox SET
    ocorrido_em = (envelope ->> 'ocorrido_em')::timestamptz,
    dados = envelope -> 'dados'
"""


def upgrade() -> None:
    op.add_column("outbox", sa.Column("exchange", sa.String(length=255)))
    op.add_column("outbox", sa.Column("routing_key", sa.String(length=255)))
    op.add_column("outbox", sa.Column("envelope", postgresql.JSONB()))
    op.add_column("outbox", sa.Column("traceparent", sa.Text(), nullable=True))
    op.add_column("outbox", sa.Column("tracestate", sa.Text(), nullable=True))
    # to_jsonb do timestamptz sai em ISO 8601 no fuso da sessao: UTC, como o
    # contrato pede (+00:00).
    op.execute("SET LOCAL TimeZone = 'UTC'")
    op.execute(_ENVELOPE_DAS_COLUNAS)
    for coluna in ("exchange", "routing_key", "envelope"):
        op.alter_column("outbox", coluna, nullable=False)
    op.drop_column("outbox", "ocorrido_em")
    op.drop_column("outbox", "dados")
    op.create_table(
        "mensagens_processadas",
        sa.Column("mensagem_id", sa.Uuid(), nullable=False),
        sa.Column(
            "processada_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("mensagem_id"),
    )
    op.create_index(
        "ix_mensagens_processadas_processada_em",
        "mensagens_processadas",
        ["processada_em"],
    )
    op.add_column("diagnosticos", sa.Column("solicitacao_id", sa.Uuid()))
    op.add_column("execucoes", sa.Column("agendamento_id", sa.Uuid()))
    for tabela in ("diagnosticos", "execucoes"):
        op.add_column(tabela, sa.Column("traceparent", sa.Text(), nullable=True))
        op.add_column(tabela, sa.Column("tracestate", sa.Text(), nullable=True))
    # AnonimizarVeiculo acha os retratos do veiculo sem varrer as tabelas.
    for tabela in ("diagnosticos", "execucoes"):
        op.create_index(
            f"ix_{tabela}_veiculo_id", tabela, [sa.text("(veiculo ->> 'veiculo_id')")]
        )


def downgrade() -> None:
    for tabela in ("execucoes", "diagnosticos"):
        op.drop_index(f"ix_{tabela}_veiculo_id", table_name=tabela)
        op.drop_column(tabela, "tracestate")
        op.drop_column(tabela, "traceparent")
    op.drop_column("execucoes", "agendamento_id")
    op.drop_column("diagnosticos", "solicitacao_id")
    op.drop_table("mensagens_processadas")
    op.add_column("outbox", sa.Column("ocorrido_em", sa.DateTime(timezone=True)))
    op.add_column("outbox", sa.Column("dados", postgresql.JSONB()))
    op.execute(_COLUNAS_DO_ENVELOPE)
    op.alter_column("outbox", "ocorrido_em", nullable=False)
    op.alter_column("outbox", "dados", nullable=False)
    for coluna in ("tracestate", "traceparent", "envelope", "routing_key", "exchange"):
        op.drop_column("outbox", coluna)
