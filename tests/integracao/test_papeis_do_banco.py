"""Papeis do banco dos manifestos no PostgreSQL do StatefulSet (ADR-042).

Sobe a imagem e o ambiente do container ``postgres`` do StatefulSet que o
``kubectl kustomize`` renderiza (sem ``trust`` no loopback nem no socket), como o
usuario 999 e com a raiz somente leitura, com o ``k8s/base/papeis.sql`` como
script de init. Confere cada papel como no Kubernetes: o dono migra e semeia (o
Job), a API sobe com ``execucao_app`` e faz DML, mas DDL, atributo de papel,
escrita na ``alembic_version`` e privilegio alem do DML nao, o exporter le
estatisticas e nenhuma tabela, e ninguem entra como ``postgres`` sem senha.

Num servidor que grava o texto de todo comando (por comando, por duracao e por
amostragem), o script e o procedimento de troca de senha do README deixam as
senhas fora do log, e cada um dos cinco ``SET`` que as protegem, retirado, as
deixa vazar.
"""

from __future__ import annotations

import base64
import re
import secrets
import textwrap
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg2
import pytest
from alembic import command
from fastapi.testclient import TestClient
from psycopg2.errors import InsufficientPrivilege
from testcontainers.core.container import DockerContainer

from src import banco as modulo_banco
from src.banco import aguardar, restringir_versao, revisao_da_imagem
from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)
from src.estoque.infraestrutura.seed import ITENS_DEMO, semear
from src.main import criar_app
from tests.integracao.conftest import BILLING_URL, config_alembic
from tests.manifestos import objeto

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_RAIZ = Path(__file__).resolve().parents[2]
_PAPEIS = {
    "postgres": "POSTGRES_PASSWORD",
    "execucao": "POSTGRES_OWNER_PASSWORD",
    "execucao_app": "POSTGRES_APP_PASSWORD",
    "execucao_exporter": "POSTGRES_EXPORTER_PASSWORD",
}
_PAPEIS_DO_SERVICO = ("execucao", "execucao_app", "execucao_exporter")

# Os cinco parametros que gravam o texto do comando, e portanto a senha que o psql
# ja trocou na variavel: cada um desligado no inicio do script e do procedimento
# de troca de senha do README.
_SETS_DE_LOG = (
    "log_statement",
    "log_min_error_statement",
    "log_min_duration_statement",
    "log_min_duration_sample",
    "log_transaction_sample_rate",
)
# Servidor que grava tudo: o texto de todo comando, por duracao e por amostragem,
# e o comando que falha.
_SERVIDOR_QUE_GRAVA_TUDO = [
    "postgres",
    "-c",
    "log_statement=all",
    "-c",
    "log_min_error_statement=error",
    "-c",
    "log_min_duration_statement=0",
    "-c",
    "log_min_duration_sample=0",
    "-c",
    "log_statement_sample_rate=1",
    "-c",
    "log_transaction_sample_rate=1",
]


def _container_do_banco() -> dict[str, Any]:
    statefulset = objeto("kind", "StatefulSet", "execucao-postgres")
    containers = statefulset["spec"]["template"]["spec"]["containers"]
    [postgres] = [c for c in containers if c["name"] == "postgres"]
    return dict(postgres)


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


def _esperar_o_banco(url: str) -> None:
    # Com o script de init, o dono so existe depois dele, e o servidor temporario
    # do init nao ouve TCP: a primeira conexao e o banco pronto.
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


@contextmanager
def _banco_dos_manifestos(
    comando: list[str], *, com_papeis: bool
) -> Iterator[BancoDosManifestos]:
    """O container ``postgres`` do StatefulSet, com ``comando`` no lugar do servidor."""
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
    if com_papeis:
        container.with_volume_mapping(
            str(_RAIZ / "k8s" / "base" / "papeis.sql"),
            "/docker-entrypoint-initdb.d/papeis.sql",
            "ro",
        )
    # Como no pod: usuario 999, raiz somente leitura e so o socket e o /tmp
    # gravaveis. O servidor grava o log que cada teste confere.
    container.with_kwargs(user="999:999", read_only=True)
    container.with_tmpfs_mount("/var/run/postgresql")
    container.with_tmpfs_mount("/tmp")  # noqa: S108 - o emptyDir do pod
    container.with_command(comando)
    container.start()
    try:
        porta = container.get_exposed_port(5432)
        endereco = f"{container.get_container_host_ip()}:{porta}/execucao"
        sonda = postgres["readinessProbe"]["exec"]["command"]
        instancia = BancoDosManifestos(container, sonda, senhas, endereco)
        _esperar_o_banco(instancia.url("execucao" if com_papeis else "postgres"))
        yield instancia
    finally:
        container.stop()


@pytest.fixture(scope="module")
def banco() -> Iterator[BancoDosManifestos]:
    with _banco_dos_manifestos(
        ["postgres", "-c", "log_statement=all"], com_papeis=True
    ) as instancia:
        # O Job de migracao, com o dono: migra, fecha a escrita da aplicacao na
        # alembic_version e semeia.
        command.upgrade(config_alembic(instancia.url("execucao")), "head")
        engine = criar_engine(instancia.url("execucao"))
        restringir_versao(engine)
        semear(criar_session_factory(engine))
        engine.dispose()
        yield instancia


@pytest.fixture(scope="module")
def servidor_que_grava_tudo() -> Iterator[BancoDosManifestos]:
    # Sem o script de init: cada teste o roda (inteiro ou sem um dos SET) num
    # banco sem os papeis.
    with _banco_dos_manifestos(_SERVIDOR_QUE_GRAVA_TUDO, com_papeis=False) as instancia:
        yield instancia


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
    _, head = revisao_da_imagem()
    with _cursor(banco, "execucao_app") as cursor:
        # A tabela que o aguarda-migracao le para esperar o Job.
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone() == (head,)
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
            "CREATE TEMP TABLE intrusa (id int)",
            "DROP TABLE itens_estoque",
            "TRUNCATE outbox",
            "COPY outbox TO PROGRAM 'true'",
        ):
            with pytest.raises(InsufficientPrivilege):
                cursor.execute(ddl)


def test_papeis_sem_atributo_que_amplie_o_acesso(banco: BancoDosManifestos) -> None:
    # So o login: nem superusuario, nem CREATEROLE (criaria papeis e daria
    # membership), CREATEDB, REPLICATION ou BYPASSRLS.
    with _cursor(banco, "postgres") as cursor:
        cursor.execute(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s) "
            "ORDER BY rolname",
            (list(_PAPEIS_DO_SERVICO),),
        )
        atributos = cursor.fetchall()
    assert atributos == [
        (papel, False, False, False, True, False, False)
        for papel in sorted(_PAPEIS_DO_SERVICO)
    ]


def test_so_o_exporter_pertence_a_um_papel_e_e_o_pg_monitor(
    banco: BancoDosManifestos,
) -> None:
    # A aplicacao nao herda pg_write_all_data, pg_read_all_data nem nenhum outro.
    with _cursor(banco, "postgres") as cursor:
        cursor.execute(
            "SELECT papel.rolname, membro.rolname FROM pg_auth_members m "
            "JOIN pg_roles papel ON papel.oid = m.roleid "
            "JOIN pg_roles membro ON membro.oid = m.member "
            "WHERE membro.rolname = ANY(%s)",
            (list(_PAPEIS_DO_SERVICO),),
        )
        assert cursor.fetchall() == [("pg_monitor", "execucao_exporter")]


@pytest.mark.parametrize(
    ("papel", "banco_alvo", "privilegio", "permitido"),
    [
        pytest.param("execucao", "execucao", "CONNECT", True, id="dono-conecta"),
        pytest.param("execucao", "execucao", "CREATE", True, id="dono-cria"),
        pytest.param("execucao", "postgres", "CONNECT", False, id="dono-fora-do-pg"),
        pytest.param("execucao_app", "execucao", "CONNECT", True, id="app-conecta"),
        pytest.param("execucao_app", "execucao", "TEMPORARY", False, id="app-sem-temp"),
        pytest.param("execucao_app", "execucao", "CREATE", False, id="app-sem-create"),
        pytest.param("execucao_app", "postgres", "CONNECT", False, id="app-fora-do-pg"),
        pytest.param(
            "execucao_exporter", "execucao", "CONNECT", True, id="exp-conecta"
        ),
        pytest.param(
            "execucao_exporter", "execucao", "TEMPORARY", False, id="exp-sem-temp"
        ),
        pytest.param(
            "execucao_exporter", "postgres", "CONNECT", False, id="exp-fora-do-pg"
        ),
    ],
)
def test_so_o_dono_tem_mais_que_o_connect_no_banco(
    banco: BancoDosManifestos,
    papel: str,
    banco_alvo: str,
    privilegio: str,
    permitido: bool,
) -> None:
    # O PUBLIC tem CONNECT e TEMPORARY em todo banco novo, e o GRANT CONNECT do
    # papeis.sql nao diria nada se o script nao os tirasse.
    with _cursor(banco, "postgres") as cursor:
        cursor.execute(
            "SELECT has_database_privilege(%s, %s, %s)", (papel, banco_alvo, privilegio)
        )
        assert cursor.fetchone() == (permitido,)


_PRIVILEGIOS_DE_TABELA = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


def _privilegios(cursor: Any, papel: str) -> dict[str, set[str]]:
    """Os privilegios de ``papel`` em cada tabela do schema ``public``."""
    cursor.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
    )
    tabelas = [linha[0] for linha in cursor.fetchall()]
    privilegios: dict[str, set[str]] = {}
    for tabela in tabelas:
        cursor.execute(
            "SELECT p FROM unnest(%s::text[]) AS p "
            "WHERE has_table_privilege(%s, format('public.%%I', %s::text), p)",
            (list(_PRIVILEGIOS_DE_TABELA), papel, tabela),
        )
        privilegios[tabela] = {linha[0] for linha in cursor.fetchall()}
    return privilegios


def test_papel_da_aplicacao_so_tem_dml_nas_tabelas_do_dono(
    banco: BancoDosManifestos,
) -> None:
    with _cursor(banco, "postgres") as cursor:
        privilegios = _privilegios(cursor, "execucao_app")
        cursor.execute(
            "SELECT has_schema_privilege('execucao_app', 'public', 'CREATE'), "
            "has_database_privilege('execucao_app', 'execucao', 'CREATE')"
        )
        sem_create = cursor.fetchone()
    assert "outbox" in privilegios
    # Nunca TRUNCATE, REFERENCES nem TRIGGER, em tabela nenhuma, e a
    # alembic_version so se le: a revisao do banco nao e da aplicacao.
    assert privilegios == {
        tabela: {"SELECT"}
        if tabela == "alembic_version"
        else {"SELECT", "INSERT", "UPDATE", "DELETE"}
        for tabela in privilegios
    }
    assert sem_create == (False, False)


@pytest.mark.parametrize(
    "escrita",
    [
        pytest.param("UPDATE alembic_version SET version_num = '999'", id="update"),
        pytest.param("DELETE FROM alembic_version", id="delete"),
        pytest.param("INSERT INTO alembic_version VALUES ('999')", id="insert"),
    ],
)
def test_papel_da_aplicacao_nao_escreve_a_revisao_do_banco(
    banco: BancoDosManifestos, escrita: str
) -> None:
    with _cursor(banco, "execucao_app") as cursor, pytest.raises(InsufficientPrivilege):
        cursor.execute(escrita)


def test_aguardar_le_a_revisao_com_o_papel_da_aplicacao(
    banco: BancoDosManifestos,
) -> None:
    # O initContainer roda com a DATABASE_URL do execucao_app.
    engine = criar_engine(banco.url("execucao_app"))
    try:
        aguardar(
            engine, esperar=lambda _segundos: pytest.fail("o banco ja esta na head")
        )
    finally:
        engine.dispose()


def test_comando_restringir_e_idempotente(
    banco: BancoDosManifestos, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setattr(modulo_banco, "configurar_logging", lambda: None)
    monkeypatch.setenv("DATABASE_URL", banco.url("execucao"))

    modulo_banco.main(["restringir"])
    modulo_banco.main(["restringir"])

    with pytest.raises(SystemExit, match="uso"):
        modulo_banco.main(["outro"])


def test_restringir_reprova_quando_o_papel_ainda_escreve_na_versao(
    banco: BancoDosManifestos, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setattr(modulo_banco, "configurar_logging", lambda: None)
    with _cursor(banco, "execucao") as cursor:
        cursor.execute("GRANT INSERT ON alembic_version TO execucao_app")
    try:
        # O REVOKE de quem tem privilegio na tabela mas nao e o dono dela, nem
        # tem grant option, so avisa e nao revoga nada.
        monkeypatch.setenv("DATABASE_URL", banco.url("execucao_app"))
        with pytest.raises(RuntimeError, match="ainda escreve"):
            modulo_banco.main(["restringir"])
    finally:
        monkeypatch.setenv("DATABASE_URL", banco.url("execucao"))
        modulo_banco.main(["restringir"])


def test_exporter_nao_tem_privilegio_em_tabela_nenhuma(
    banco: BancoDosManifestos,
) -> None:
    with _cursor(banco, "postgres") as cursor:
        privilegios = _privilegios(cursor, "execucao_exporter")
    assert "outbox" in privilegios
    assert privilegios == {tabela: set() for tabela in privilegios}


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


# --- as senhas fora do log do servidor ----------------------------------------

_LIMPAR_PAPEIS = """
DO $$
DECLARE papel text;
BEGIN
  FOREACH papel IN ARRAY ARRAY['execucao', 'execucao_app', 'execucao_exporter'] LOOP
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = papel) THEN
      EXECUTE format('REASSIGN OWNED BY %I TO postgres', papel);
      EXECUTE format('DROP OWNED BY %I', papel);
      EXECUTE format('DROP ROLE %I', papel);
    END IF;
  END LOOP;
END $$;
"""


def _psql(
    servidor: BancoDosManifestos, sql: str, ambiente: dict[str, str] | None = None
) -> int:
    """Roda ``sql`` com o psql do container, como o Job e a troca de senha: o
    superusuario com a senha do ambiente, e as senhas dos papeis nas variaveis."""
    variaveis = " ".join(f"{nome}={valor}" for nome, valor in (ambiente or {}).items())
    codificado = base64.b64encode(sql.encode()).decode()
    codigo, _ = servidor.container.exec(
        [
            "sh",
            "-c",
            f"echo {codificado} | base64 -d > /tmp/caso.sql && "
            f'PGPASSWORD="$POSTGRES_PASSWORD" {variaveis} psql -w -U postgres '
            "-d execucao -v ON_ERROR_STOP=1 -f /tmp/caso.sql",
        ]
    )
    return int(codigo)


def _log_ate_aqui(servidor: BancoDosManifestos) -> str:
    """O log do servidor, depois de ele gravar tudo o que o teste mandou: um
    comando marcador, gravado depois dos outros, mostra que o log alcancou o fim."""
    marca = f"fim-{secrets.token_hex(8)}"
    _psql(servidor, f"SELECT '{marca}';")
    prazo = time.monotonic() + 10
    while marca not in (log := servidor.log()):
        assert time.monotonic() < prazo, "o servidor nao gravou o log a tempo"
        time.sleep(0.1)
    return log


def _sem_o_set(sql: str, parametro: str | None) -> str:
    if parametro is None:
        return sql
    sem, trocas = re.subn(rf"^SET {parametro} = .*\n", "", sql, flags=re.MULTILINE)
    assert trocas == 1, f"o SET de {parametro} nao esta uma vez so no SQL"
    return sem


def _sets_de_log(sql: str) -> list[str]:
    return re.findall(r"^SET (log_\w+) = .*$", sql, flags=re.MULTILINE)


_COM_TODOS_OS_SET = [pytest.param(None, id="completo")]
_SEM_UM_SET = [
    pytest.param(parametro, id=f"sem-{parametro}") for parametro in _SETS_DE_LOG
]


def _sql_da_troca_de_senha() -> str:
    """O SQL do passo 2 da troca de senha do README, o que vai ao ``psql``."""
    readme = (_RAIZ / "README.md").read_text()
    # O bloco esta num item de lista: recuado no README, a margem sai ao colar.
    [sql] = re.findall(r"<<'SQL'\n(.*?)\n *SQL\n", readme, flags=re.DOTALL)
    return textwrap.dedent(sql) + "\n"


def test_script_e_readme_desligam_os_mesmos_cinco_parametros_de_log() -> None:
    papeis = (_RAIZ / "k8s" / "base" / "papeis.sql").read_text()

    assert _sets_de_log(papeis) == list(_SETS_DE_LOG)
    assert _sets_de_log(_sql_da_troca_de_senha()) == list(_SETS_DE_LOG)


@pytest.mark.parametrize("sem", [*_COM_TODOS_OS_SET, *_SEM_UM_SET])
def test_papeis_sql_deixa_as_senhas_fora_do_log_so_com_os_cinco_set(
    servidor_que_grava_tudo: BancoDosManifestos, sem: str | None
) -> None:
    servidor = servidor_que_grava_tudo
    senhas = {
        "POSTGRES_OWNER_PASSWORD": secrets.token_hex(24),
        "POSTGRES_APP_PASSWORD": secrets.token_hex(24),
        "POSTGRES_EXPORTER_PASSWORD": secrets.token_hex(24),
    }
    script = (_RAIZ / "k8s" / "base" / "papeis.sql").read_text()
    # O exporter ja existe: o ultimo CREATE ROLE falha, o caso em que o servidor
    # grava a linha STATEMENT com a senha.
    _psql(servidor, _LIMPAR_PAPEIS + "CREATE ROLE execucao_exporter LOGIN;")

    codigo = _psql(servidor, _sem_o_set(script, sem), senhas)
    log = _log_ate_aqui(servidor)

    assert codigo != 0
    assert 'role "execucao_exporter" already exists' in log
    vazaram = [chave for chave, senha in senhas.items() if senha in log]
    if sem is None:
        assert vazaram == []
    else:
        assert vazaram, f"sem o SET de {sem} nenhuma senha chegou ao log"


@pytest.mark.parametrize("sem", [*_COM_TODOS_OS_SET, *_SEM_UM_SET])
def test_troca_de_senha_do_readme_deixa_a_senha_fora_do_log_so_com_os_cinco_set(
    servidor_que_grava_tudo: BancoDosManifestos, sem: str | None
) -> None:
    servidor = servidor_que_grava_tudo
    sql = _sem_o_set(_sql_da_troca_de_senha(), sem)
    _psql(servidor, _LIMPAR_PAPEIS + "CREATE ROLE execucao_app LOGIN;")
    # Dois casos: o papel existe (o comando passa) e nao existe (o comando falha,
    # e o servidor grava a linha STATEMENT).
    certo = {"SENHA_NOVA": secrets.token_hex(24)}
    errado = {"SENHA_NOVA": secrets.token_hex(24)}

    assert _psql(servidor, sql, certo) == 0
    assert (
        _psql(servidor, sql.replace("execucao_app", "papel_que_nao_existe"), errado)
        != 0
    )
    log = _log_ate_aqui(servidor)

    assert 'role "papel_que_nao_existe" does not exist' in log
    vazaram = [senha for senha in (certo, errado) if senha["SENHA_NOVA"] in log]
    if sem is None:
        assert vazaram == []
    else:
        assert vazaram, f"sem o SET de {sem} a senha nova chegou ao log"
