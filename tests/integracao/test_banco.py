"""Engine do servico contra o Postgres: limites de tempo e erro sem parametros."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from src.compartilhado.infraestrutura.database import criar_engine

if TYPE_CHECKING:
    from sqlalchemy import Engine

_INSERIR_ITEM = text(
    "INSERT INTO itens_estoque (id, sku, nome, quantidade_disponivel, "
    "quantidade_reservada, ativo) VALUES (:id, :sku, :nome, :disponivel, 0, true)"
)


def _mostrar(engine: Engine, parametro: str) -> str:
    with engine.connect() as conexao:
        return str(conexao.execute(text(f"SHOW {parametro}")).scalar_one())


@pytest.mark.parametrize(
    ("parametro", "valor"),
    [
        pytest.param("lock_timeout", "5s", id="lock"),
        pytest.param("statement_timeout", "15s", id="statement"),
        pytest.param("idle_in_transaction_session_timeout", "30s", id="ocioso"),
    ],
)
def test_engine_aplica_os_limites_de_tempo_no_servidor(
    engine: Engine, parametro: str, valor: str
) -> None:
    assert _mostrar(engine, parametro) == valor


def test_limites_e_pool_vem_do_ambiente(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_LOCK_TIMEOUT_MS", "250")
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    ajustada = criar_engine(database_url)
    try:
        assert _mostrar(ajustada, "lock_timeout") == "250ms"
        assert ajustada.pool.size() == 2
    finally:
        ajustada.dispose()


def test_lock_alem_do_limite_vira_erro_em_vez_de_esperar_para_sempre(
    engine: Engine, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id = uuid4()
    with engine.begin() as conexao:
        conexao.execute(
            _INSERIR_ITEM,
            {"id": item_id, "sku": "PEC-LOCK", "nome": "x", "disponivel": 1},
        )
    monkeypatch.setenv("DB_LOCK_TIMEOUT_MS", "200")
    impaciente = criar_engine(database_url)
    travar = text("SELECT id FROM itens_estoque WHERE id = :id FOR UPDATE")
    try:
        with engine.connect() as dono, impaciente.connect() as outro:
            dono.execute(travar, {"id": item_id})
            inicio = time.monotonic()
            with pytest.raises(OperationalError) as erro:
                outro.execute(travar, {"id": item_id})
            assert time.monotonic() - inicio < 2
            assert getattr(erro.value.orig, "pgcode", None) == "55P03"
    finally:
        impaciente.dispose()


def test_erro_de_banco_nao_carrega_os_parametros(engine: Engine) -> None:
    with pytest.raises(IntegrityError) as erro, engine.begin() as conexao:
        conexao.execute(
            _INSERIR_ITEM,
            {
                "id": uuid4(),
                "sku": "PEC-X",
                "nome": "parametro-secreto",
                "disponivel": -1,
            },
        )
    assert "hide_parameters" in str(erro.value)
    assert "parametro-secreto" not in str(erro.value).split("DETAIL")[0]
