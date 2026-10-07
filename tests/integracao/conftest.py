from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import respx
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text

from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)
from src.compartilhado.infraestrutura.mensageria.consumidor import Consumidor
from src.compartilhado.infraestrutura.mensageria.processo import (
    Backoff,
    SinaisDoProcesso,
)
from src.compartilhado.infraestrutura.mensageria.relay import Relay
from src.consumidor import HANDLERS
from src.diagnostico.infraestrutura.validador_billing import ValidadorDeItensBilling
from tests.integracao.broker import Broker, subir_broker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from fastapi.testclient import TestClient
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

RAIZ = Path(__file__).resolve().parents[2]
BILLING_URL = "http://billing.test"
_TABELAS = (
    "itens_estoque, reservas, diagnosticos, execucoes, outbox, mensagens_processadas"
)


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


def _banco_externo() -> str | None:
    """TEST_DATABASE_URL, recusada se o banco nao for de teste (`*_test`).

    A suite trunca as tabelas a cada teste e derruba o esquema no fim: apontar
    para um banco de verdade por engano apagaria os dados.
    """
    externa = os.environ.get("TEST_DATABASE_URL")
    if externa and not (make_url(externa).database or "").endswith("_test"):
        pytest.exit("TEST_DATABASE_URL precisa apontar para um banco *_test", 2)
    return externa


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Postgres 16 efemero (testcontainers) ou TEST_DATABASE_URL explicita."""
    externa = _banco_externo()
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
    if _banco_externo():
        # O container efemero some sozinho; o banco externo volta ao vazio.
        command.downgrade(config_alembic(database_url), "base")


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
                    "SELECT mensagem_id, tipo, correlation_id, envelope, "
                    "envelope -> 'dados' AS dados, exchange, routing_key, "
                    "traceparent, tracestate, status, tentativas, ultimo_erro "
                    "FROM outbox ORDER BY id"
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
    # Sem o sono do jitter entre retries do Billing (o intervalo e testado no
    # adapter): o teste de API so confere as tentativas.
    monkeypatch.setattr(
        "src.diagnostico.interfaces.dependencies.ValidadorDeItensBilling",
        functools.partial(ValidadorDeItensBilling, dormir=lambda _segundos: None),
    )
    with TestClient(criar_app()) as cliente:
        yield cliente


@pytest.fixture
def autenticar(emitir_token: Callable[..., str]) -> Callable[..., dict[str, str]]:
    def _headers(papel: str, sub: Any = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {emitir_token(papel, sub)}"}

    return _headers


@pytest.fixture(scope="session")
def _broker_da_sessao() -> Iterator[Broker]:
    container, broker = subir_broker()
    try:
        yield broker
    finally:
        container.stop()


@pytest.fixture
def broker(_broker_da_sessao: Broker) -> Iterator[Broker]:
    """RabbitMQ com a topologia do platform; as filas do servico saem vazias."""
    yield _broker_da_sessao
    _broker_da_sessao.esvaziar()


@pytest.fixture
def sinais(tmp_path: Path) -> Callable[[str], SinaisDoProcesso]:
    def _sinais(nome: str) -> SinaisDoProcesso:
        return SinaisDoProcesso(
            tmp_path / f"{nome}-heartbeat", tmp_path / f"{nome}-pronto"
        )

    return _sinais


@pytest.fixture
def consumidor(
    broker: Broker,
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
) -> Callable[..., Consumidor]:
    def _criar(handlers: Mapping[str, Any] = HANDLERS) -> Consumidor:
        return Consumidor(
            engine,
            broker.url("execucao"),
            handlers,
            sinais("consumidor"),
            Backoff(0.1, 0.5),
        )

    return _criar


@pytest.fixture
def relay(
    broker: Broker, engine: Engine, sinais: Callable[[str], SinaisDoProcesso]
) -> Callable[..., Relay]:
    def _criar(poll_s: float = 0.1) -> Relay:
        return Relay(
            engine,
            broker.url("execucao"),
            sinais("relay"),
            poll_s=poll_s,
            backoff=Backoff(0.1, 0.5),
        )

    return _criar
