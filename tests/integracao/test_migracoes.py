from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import src.mapeamentos  # noqa: F401 - registra todas as tabelas no metadata
from src.compartilhado.infraestrutura.database import metadata
from tests.integracao.conftest import config_alembic

if TYPE_CHECKING:
    from sqlalchemy import Engine


def test_migracoes_batem_com_os_mapeamentos(engine: Engine) -> None:
    with engine.connect() as conexao:
        diferencas = compare_metadata(MigrationContext.configure(conexao), metadata)
    assert diferencas == []


def test_downgrade_e_upgrade_de_ponta_a_ponta(
    engine: Engine, database_url: str
) -> None:
    config = config_alembic(database_url)
    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    command.upgrade(config, "head")
    assert {"itens_estoque", "reservas", "diagnosticos", "execucoes", "outbox"} <= set(
        inspect(engine).get_table_names()
    )


@pytest.mark.parametrize(
    ("disponivel", "reservada"),
    [(-1, 0), (1, 2), (1, -1)],
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
