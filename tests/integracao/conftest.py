from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import respx
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi.testclient import TestClient
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

RAIZ = Path(__file__).resolve().parents[2]
BILLING_URL = "http://billing.test"
_TABELAS = "itens_estoque, reservas, diagnosticos, execucoes, outbox"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integracao" in item.path.parts:
            item.add_marker(pytest.mark.integracao)


def config_alembic(url: str) -> Config:
    config = Config(str(RAIZ / "alembic.ini"))
    config.set_main_option("script_location", str(RAIZ / "migrations"))
    config.attributes["configure_logger"] = False
    config.attributes["database_url"] = url
    return config


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Postgres 16 efemero (testcontainers) ou TEST_DATABASE_URL explicita."""
    externa = os.environ.get("TEST_DATABASE_URL")
    if externa:
        yield externa
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver="psycopg2") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    # O esquema dos testes e o das migracoes (nao metadata.create_all): um erro
    # na migracao quebra a suite inteira, nao so o deploy.
    command.upgrade(config_alembic(database_url), "head")
    eng = criar_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _limpar_tabelas(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conexao:
        conexao.execute(text(f"TRUNCATE {_TABELAS} RESTART IDENTITY"))


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return criar_session_factory(engine)


@pytest.fixture
def outbox(engine: Engine) -> Callable[[], list[dict[str, Any]]]:
    """Linhas gravadas na outbox, em ordem de gravacao."""

    def _ler() -> list[dict[str, Any]]:
        with engine.connect() as conexao:
            linhas = conexao.execute(
                text(
                    "SELECT mensagem_id, tipo, correlation_id, ocorrido_em, dados, "
                    "status FROM outbox ORDER BY id"
                )
            )
            return [dict(linha._mapping) for linha in linhas]

    return _ler


@pytest.fixture
def billing() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BILLING_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def api(
    database_url: str,
    engine: Engine,
    jwks_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """App real (lifespan inclusive) contra o Postgres de teste e o JWKS local."""
    from fastapi.testclient import TestClient

    from src.main import criar_app

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("JWKS_URL", jwks_url)
    monkeypatch.setenv("BILLING_URL", BILLING_URL)
    with TestClient(criar_app()) as cliente:
        yield cliente


@pytest.fixture
def autenticar(emitir_token: Callable[..., str]) -> Callable[..., dict[str, str]]:
    def _headers(papel: str, sub: Any = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {emitir_token(papel, sub)}"}

    return _headers
