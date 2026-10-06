from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

import src.mapeamentos  # noqa: F401 - registra as tabelas no metadata
from src.compartilhado.infraestrutura.ambiente import inteiro_opcional
from src.compartilhado.infraestrutura.database import metadata

config = context.config

# `configure_logger=False` (attributes) deixa os testes rodarem a migracao sem
# o fileConfig reconfigurar o logging global do processo pytest.
if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name)

target_metadata = metadata

# Chave do pg_advisory_lock das migracoes (qualquer bigint fixo do servico).
_TRAVA_DE_MIGRACAO = 4_034_003


def _url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
    if not url:
        msg = "Defina DATABASE_URL para rodar as migracoes"
        raise RuntimeError(msg)
    return str(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    chave = {"chave": _TRAVA_DE_MIGRACAO}
    with engine.connect() as connection:
        # Replicas com RUN_MIGRATIONS_ON_STARTUP=true serializam aqui: a segunda
        # espera a primeira e encontra o esquema em head. Trava de sessao; o
        # commit fecha so a transacao implicita, para o Alembic abrir a dele.
        connection.execute(text("SELECT pg_advisory_lock(:chave)"), chave)
        # So depois da trava: DDL que espera lock de tabela alem do limite
        # falha (e o deploy tenta de novo) em vez de enfileirar o trafego.
        connection.execute(
            text("SELECT set_config('lock_timeout', :limite, false)"),
            {"limite": f"{inteiro_opcional('DB_LOCK_TIMEOUT_MS', 5000)}ms"},
        )
        connection.commit()
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:chave)"), chave)
            connection.commit()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
