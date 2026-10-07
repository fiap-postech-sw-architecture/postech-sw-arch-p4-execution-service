"""Papeis do banco dos manifestos no PostgreSQL do StatefulSet (ADR-042).

Sobe a imagem e o ambiente do container ``postgres`` de ``k8s/base/banco.yaml``
(sem ``trust`` no loopback nem no socket), como o usuario 999 e com a raiz
somente leitura, com o ``k8s/base/papeis.sql`` como script de init e o servidor
gravando todo comando no log. Confere cada papel como no Kubernetes: o dono
migra e semeia (o Job), a API sobe com ``execucao_app`` e faz DML, mas DDL nao,
o exporter le estatisticas e nao le tabela, ninguem entra como ``postgres`` sem
senha, e nenhuma senha dos papeis chega ao log do servidor.
"""

from __future__ import annotations

import secrets
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg2
import pytest
import yaml
from alembic import command
from fastapi.testclient import TestClient
from psycopg2.errors import InsufficientPrivilege
from testcontainers.core.container import DockerContainer

from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)
from src.estoque.infraestrutura.seed import ITENS_DEMO, semear
from src.main import criar_app
from tests.integracao.conftest import BILLING_URL, config_alembic

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_BASE = Path(__file__).resolve().parents[2] / "k8s" / "base"
_PAPEIS = {
    "execucao": "POSTGRES_OWNER_PASSWORD",
    "execucao_app": "POSTGRES_APP_PASSWORD",
    "execucao_exporter": "POSTGRES_EXPORTER_PASSWORD",
}


def _container_do_banco() -> dict[str, Any]:
    for documento in yaml.safe_load_all((_BASE / "banco.yaml").read_text()):
        if documento["kind"] == "StatefulSet":
            containers = documento["spec"]["template"]["spec"]["containers"]
            [postgres] = [c for c in containers if c["name"] == "postgres"]
            return dict(postgres)
    msg = "banco.yaml sem o StatefulSet"
    raise AssertionError(msg)


@dataclass(frozen=True)
class BancoDosManifestos:
    container: DockerContainer
    sonda: list[str]
    senhas: dict[str, str]
    endereco: str

    def url(self, papel: str) -> str:
        return f"postgresql://{papel}:{self.senhas[_PAPEIS[papel]]}@{self.endereco}"

    def log(self) -> str:
        saida, erros = self.container.get_logs()
        return (saida + erros).decode()


def _esperar_o_dono(url: str) -> None:
    # O dono so existe depois do script de init, e o servidor temporario do init
    # nao ouve TCP: a primeira conexao dele e o banco pronto.
    prazo = time.monotonic() + 90
    while True:
        try:
            psycopg2.connect(url, connect_timeout=2).close()
        except psycopg2.OperationalError:
            if time.monotonic() > prazo:
                raise
            time.sleep(0.5)
        else:
            return


@pytest.fixture(scope="module")
def banco() -> Iterator[BancoDosManifestos]:
    postgres = _container_do_banco()
    senhas = {
        variavel["valueFrom"]["secretKeyRef"]["key"]: secrets.token_hex(24)
        for variavel in postgres["env"]
        if "valueFrom" in variavel
    }
    container = DockerContainer(postgres["image"]).with_exposed_ports(5432)
    for variavel in postgres["env"]:
        valor = variavel.get("value")
        if valor is None:
            valor = senhas[variavel["valueFrom"]["secretKeyRef"]["key"]]
        container.with_env(variavel["name"], valor)
    container.with_volume_mapping(
        str(_BASE / "papeis.sql"), "/docker-entrypoint-initdb.d/papeis.sql", "ro"
    )
    # Como no pod: usuario 999, raiz somente leitura e so o socket e o /tmp
    # gravaveis. Todo comando vai para o log, para provar que senha nao vai.
    container.with_kwargs(user="999:999", read_only=True)
    container.with_tmpfs_mount("/var/run/postgresql")
    container.with_tmpfs_mount("/tmp")  # noqa: S108 - o emptyDir do pod
    container.with_command(["postgres", "-c", "log_statement=all"])
    container.start()
    try:
        porta = container.get_exposed_port(5432)
        endereco = f"{container.get_container_host_ip()}:{porta}/execucao"
        sonda = postgres["readinessProbe"]["exec"]["command"]
        instancia = BancoDosManifestos(container, sonda, senhas, endereco)
        _esperar_o_dono(instancia.url("execucao"))
        # O Job de migracao: migracao e semente com o dono.
        command.upgrade(config_alembic(instancia.url("execucao")), "head")
        engine = criar_engine(instancia.url("execucao"))
        semear(criar_session_factory(engine))
        engine.dispose()
        yield instancia
    finally:
        container.stop()


@contextmanager
def _cursor(banco: BancoDosManifestos, papel: str) -> Iterator[Any]:
    # closing, e nao o with da conexao: no psycopg2 2.9 ele abre uma transacao
    # mesmo em autocommit, e o primeiro erro esperado abortaria os seguintes.
    with closing(psycopg2.connect(banco.url(papel))) as conexao:
        conexao.autocommit = True
        with conexao.cursor() as cursor:
            yield cursor


def test_api_sobe_com_o_papel_da_aplicacao_e_le_e_altera_o_estoque(
    banco: BancoDosManifestos,
    jwks_url: str,
    emitir_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", banco.url("execucao_app"))
    monkeypatch.setenv("JWKS_URL", jwks_url)
    monkeypatch.setenv("BILLING_URL", BILLING_URL)
    admin = {"Authorization": f"Bearer {emitir_token('admin')}"}
    with TestClient(criar_app()) as api:
        assert api.get("/api/v1/saude/pronto").status_code == 200
        estoque = api.get("/api/v1/estoque", headers=admin)
        ajuste = api.patch(
            "/api/v1/estoque/PEC-VELA/quantidade",
            json={"quantidade_disponivel": 3},
            headers=admin,
        )
    assert estoque.json()["total"] == len(ITENS_DEMO)
    assert ajuste.status_code == 200, ajuste.text


def test_papel_da_aplicacao_faz_dml_e_le_a_versao_mas_nao_faz_ddl(
    banco: BancoDosManifestos,
) -> None:
    with _cursor(banco, "execucao_app") as cursor:
        # A tabela que o aguarda-migracao le para esperar o Job.
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone() == ("002",)
        # INSERT com a sequencia do id (privilegio padrao das sequencias).
        cursor.execute(
            "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
            "routing_key, envelope) VALUES (gen_random_uuid(), 'ReservaLiberada', "
            "gen_random_uuid(), 'pytstop.eventos', "
            "'evento.execucao.reserva_liberada', '{}') RETURNING id"
        )
        [linha] = cursor.fetchall()
        cursor.execute("DELETE FROM outbox WHERE id = %s", linha)
        for ddl in (
            "CREATE TABLE intrusa (id int)",
            "DROP TABLE itens_estoque",
            "TRUNCATE outbox",
            "COPY outbox TO PROGRAM 'true'",
        ):
            with pytest.raises(InsufficientPrivilege):
                cursor.execute(ddl)


def test_exporter_le_estatisticas_e_nao_le_tabela(banco: BancoDosManifestos) -> None:
    with _cursor(banco, "execucao_exporter") as cursor:
        # So superusuario e pg_monitor listam o WAL (o pg_stat_activity, todos).
        cursor.execute("SELECT count(*) FROM pg_ls_waldir()")
        [(arquivos,)] = cursor.fetchall()
        assert arquivos > 0
        with pytest.raises(InsufficientPrivilege):
            cursor.execute("SELECT * FROM itens_estoque")


@pytest.mark.parametrize(
    "conexao",
    [pytest.param(["-h", "127.0.0.1"], id="loopback"), pytest.param([], id="socket")],
)
def test_postgres_sem_senha_nao_entra_nem_de_dentro_do_pod(
    banco: BancoDosManifestos, conexao: list[str]
) -> None:
    # Sem trust: o exporter, no mesmo pod, nao entraria como superusuario.
    codigo, saida = banco.container.exec(
        ["psql", "-w", *conexao, "-U", "postgres", "-d", "execucao", "-c", "SELECT 1"]
    )
    assert codigo != 0
    assert b"password" in saida


def test_sonda_do_statefulset_responde_sem_senha(banco: BancoDosManifestos) -> None:
    codigo, saida = banco.container.exec(banco.sonda)
    assert codigo == 0, saida


def test_log_do_servidor_fica_sem_as_senhas_dos_papeis(
    banco: BancoDosManifestos,
) -> None:
    # O script de novo: os papeis ja existem, e o CREATE ROLE com a senha
    # falha, o caso em que o servidor gravaria a linha STATEMENT.
    codigo, _ = banco.container.exec(
        [
            "sh",
            "-c",
            'PGPASSWORD="$POSTGRES_PASSWORD" psql -w -U postgres -d execucao '
            "-v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/papeis.sql",
        ]
    )
    log = banco.log()
    assert codigo != 0
    assert 'role "execucao" already exists' in log
    # O servidor gravava todo comando (log_statement=all) ate o SET do script.
    assert "statement: SET log_statement = none;" in log
    for papel, chave in _PAPEIS.items():
        assert banco.senhas[chave] not in log, papel
