from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

import src.mapeamentos  # noqa: F401 - registra todas as tabelas no metadata
from src.compartilhado.infraestrutura.database import metadata
from tests.integracao.conftest import RAIZ, config_alembic

if TYPE_CHECKING:
    from sqlalchemy import Engine


def test_migracoes_batem_com_os_mapeamentos(engine: Engine) -> None:
    with engine.connect() as conexao:
        diferencas = compare_metadata(MigrationContext.configure(conexao), metadata)
    assert diferencas == []


_TABELAS = {"itens_estoque", "reservas", "diagnosticos", "execucoes", "outbox"}


def test_downgrade_e_upgrade_de_ponta_a_ponta(
    engine: Engine, database_url: str
) -> None:
    config = config_alembic(database_url)
    command.downgrade(config, "base")
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        # O banco e o da suite inteira: volta para head mesmo se o assert falhar.
        command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) >= _TABELAS


def test_replicas_migrando_juntas_se_serializam(
    engine: Engine, database_url: str
) -> None:
    # RUN_MIGRATIONS_ON_STARTUP com 3 containers num banco novo: sem o
    # pg_advisory_lock, dois saiam com UniqueViolation em alembic_version.
    config = config_alembic(database_url)
    command.downgrade(config, "base")
    try:
        replicas = [
            subprocess.Popen(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=RAIZ,
                env={**os.environ, "DATABASE_URL": database_url},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            for _ in range(3)
        ]
        saidas = [replica.communicate(timeout=60)[0] for replica in replicas]
        assert [replica.returncode for replica in replicas] == [0, 0, 0], saidas
    finally:
        command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) >= _TABELAS


def test_migracao_desiste_do_lock_de_tabela_em_vez_de_enfileirar(
    engine: Engine, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DDL esperando uma transacao longa seguraria todo o trafego da tabela
    # atras dela; com lock_timeout a migracao falha e o deploy tenta de novo.
    monkeypatch.setenv("DB_LOCK_TIMEOUT_MS", "200")
    with engine.connect() as leitura_longa:
        leitura_longa.execute(text("SELECT count(*) FROM outbox"))
        inicio = time.monotonic()
        with pytest.raises(OperationalError) as erro:
            command.downgrade(config_alembic(database_url), "base")
        assert time.monotonic() - inicio < 5
    assert getattr(erro.value.orig, "pgcode", None) == "55P03"
    assert set(inspect(engine).get_table_names()) >= _TABELAS  # DDL desfeito


@pytest.mark.parametrize(
    ("disponivel", "reservada"),
    [
        pytest.param(-1, 0, id="saldo-negativo"),
        pytest.param(1, 2, id="reserva-acima-do-fisico"),
        pytest.param(1, -1, id="reserva-negativa"),
    ],
)
def test_check_do_banco_impede_saldo_negativo_ou_reserva_acima_do_fisico(
    engine: Engine, disponivel: int, reservada: int
) -> None:
    insert = text(
        "INSERT INTO itens_estoque (id, sku, nome, quantidade_disponivel, "
        "quantidade_reservada, ativo) VALUES (gen_random_uuid(), 'PEC-X', "
        "'x', :disponivel, :reservada, true)"
    )
    with pytest.raises(IntegrityError), engine.begin() as conexao:
        conexao.execute(
            insert,
            {"disponivel": disponivel, "reservada": reservada},
        )


_INSERCOES = {
    "execucoes": (
        "INSERT INTO execucoes (ordem_id, status, prioridade, enfileirada_em) "
        "VALUES (gen_random_uuid(), :status, :prioridade, now())"
    ),
    "diagnosticos": (
        "INSERT INTO diagnosticos (ordem_id, status, itens, observacoes, "
        "solicitado_em) VALUES (gen_random_uuid(), :status, '[]', '', now())"
    ),
    "reservas": (
        "INSERT INTO reservas (id, ordem_id, status, itens, faltantes, criada_em) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), :status, '[]', '[]', now())"
    ),
}


@pytest.mark.parametrize(
    ("tabela", "valores", "restricao"),
    [
        pytest.param(
            "execucoes",
            {"status": "AGUARDANDO", "prioridade": "urgente"},
            "ck_execucoes_prioridade",
            id="prioridade",
        ),
        pytest.param(
            "execucoes",
            {"status": "PAUSADA", "prioridade": "normal"},
            "ck_execucoes_status",
            id="status-execucao",
        ),
        pytest.param(
            "diagnosticos",
            {"status": "PERDIDO"},
            "ck_diagnosticos_status",
            id="status-diagnostico",
        ),
        pytest.param(
            "reservas", {"status": "PARCIAL"}, "ck_reservas_status", id="status-reserva"
        ),
    ],
)
def test_check_do_banco_recusa_valor_fora_do_enum(
    engine: Engine, tabela: str, valores: dict[str, str], restricao: str
) -> None:
    # Defesa em profundidade: so os valores do enum do dominio (prioridade do
    # contrato AgendarExecucao: normal ou alta) entram na coluna.
    with pytest.raises(IntegrityError, match=restricao), engine.begin() as conexao:
        conexao.execute(text(_INSERCOES[tabela]), valores)
