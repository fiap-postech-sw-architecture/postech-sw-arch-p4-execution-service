"""``python -m src.banco aguardar``: o initContainer ``aguarda-migracao``.

Os tres casos do rollout, contra a revisao que o alembic grava no PostgreSQL:
banco na ``head`` da imagem (sai), banco a frente dela, como na imagem anterior
sobre o banco que o Job novo migrou depois de um ``rollout undo`` (sai: o pod
revertido nao pode ficar preso no Init), e banco atras (espera, dizendo por que).
A espera so le: nunca cria tabela nem grava a ``alembic_version``.
"""

from __future__ import annotations

import io
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from sqlalchemy import inspect, make_url, text
from sqlalchemy.exc import InvalidRequestError, OperationalError

from src import banco as modulo_banco
from src.banco import aguardar, revisao_da_imagem
from src.compartilhado.infraestrutura.database import criar_engine
from tests.integracao.conftest import config_alembic

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine


class _EsperaInterrompidaError(Exception):
    pass


def _eventos(log: io.StringIO) -> list[dict[str, Any]]:
    """Os eventos de ``src.banco`` (o alembic loga o contexto a cada leitura)."""
    linhas = (json.loads(linha) for linha in log.getvalue().splitlines())
    return [linha for linha in linhas if linha.get("logger") == "src.banco"]


def _revisao_anterior_a_head() -> str:
    script, head = revisao_da_imagem()
    revisao = script.get_revision(head)
    assert revisao is not None
    anterior = revisao.down_revision
    assert isinstance(anterior, str)
    return anterior


@contextmanager
def _versao_gravada(engine: Engine, versao: str) -> Iterator[None]:
    """O banco da suite com outra revisao gravada; volta para a ``head`` no fim."""
    _, head = revisao_da_imagem()
    with engine.begin() as conexao:
        conexao.execute(
            text("UPDATE alembic_version SET version_num = :v"), {"v": versao}
        )
    try:
        yield
    finally:
        with engine.begin() as conexao:
            conexao.execute(
                text("UPDATE alembic_version SET version_num = :v"), {"v": head}
            )


def _nunca_espera(_segundos: float) -> None:
    pytest.fail("o banco ja esta pronto: nao ha o que esperar")


def test_banco_na_head_da_imagem_nao_espera(
    engine: Engine, log_capturado: io.StringIO
) -> None:
    _, head = revisao_da_imagem()

    aguardar(engine, esperar=_nunca_espera)

    [evento] = _eventos(log_capturado)
    assert evento["event"] == "migration_wait_done"
    assert (evento["database_revision"], evento["reason"]) == (head, "at_head")


def test_banco_a_frente_da_imagem_nao_espera(
    engine: Engine, log_capturado: io.StringIO
) -> None:
    # Imagem N-1 sobre o banco N: o alembic da imagem antiga nao conhece a
    # revisao gravada (o laco de shell comparando `alembic current` com `heads`
    # nunca sairia aqui).
    with _versao_gravada(engine, "999"):
        aguardar(engine, esperar=_nunca_espera)

    [evento] = _eventos(log_capturado)
    assert (evento["database_revision"], evento["reason"]) == ("999", "ahead_of_image")


def test_banco_atras_da_imagem_espera_dizendo_por_que_ate_o_job_terminar(
    engine: Engine, log_capturado: io.StringIO
) -> None:
    _, head = revisao_da_imagem()
    atras = _revisao_anterior_a_head()
    esperas: list[float] = []

    def esperar(segundos: float) -> None:
        esperas.append(segundos)
        # O Job de migracao termina durante a espera.
        with engine.begin() as conexao:
            conexao.execute(
                text("UPDATE alembic_version SET version_num = :v"), {"v": head}
            )

    with _versao_gravada(engine, atras):
        aguardar(engine, esperar=esperar)

    assert esperas == [2.0]
    espera, pronto = _eventos(log_capturado)
    assert espera["event"] == "migration_wait_database_behind"
    assert (espera["database_revision"], espera["image_head"]) == (atras, head)
    assert (pronto["event"], pronto["reason"]) == ("migration_wait_done", "at_head")


@pytest.fixture
def banco_vazio(engine: Engine, database_url: str) -> Iterator[str]:
    """URL de um banco novo, sem tabela nenhuma (o Job ainda nao rodou)."""
    nome = "banco_vazio_da_espera"
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conexao:
        conexao.execute(text(f"DROP DATABASE IF EXISTS {nome} WITH (FORCE)"))
        conexao.execute(text(f"CREATE DATABASE {nome}"))
    try:
        yield (
            make_url(database_url)
            .set(database=nome)
            .render_as_string(hide_password=False)
        )
    finally:
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conexao:
            conexao.execute(text(f"DROP DATABASE {nome} WITH (FORCE)"))


def test_banco_sem_a_tabela_de_versao_espera_e_nao_cria_nada(
    banco_vazio: str, log_capturado: io.StringIO
) -> None:
    engine_vazio = criar_engine(banco_vazio)
    esperas: list[float] = []

    def esperar(segundos: float) -> None:
        esperas.append(segundos)
        # So le: a espera nao deixou tabela nem a alembic_version no banco.
        assert inspect(engine_vazio).get_table_names() == []
        if len(esperas) == 2:
            # O Job de migracao roda no fim da segunda espera.
            command.upgrade(config_alembic(banco_vazio), "head")

    try:
        aguardar(engine_vazio, esperar=esperar)
    finally:
        engine_vazio.dispose()

    assert esperas == [2.0, 2.0]
    eventos = _eventos(log_capturado)
    assert [e["event"] for e in eventos] == [
        "migration_wait_database_behind",
        "migration_wait_database_behind",
        "migration_wait_done",
    ]
    assert eventos[0]["database_revision"] is None


def test_banco_com_a_tabela_de_versao_vazia_espera(
    engine: Engine, log_capturado: io.StringIO
) -> None:
    _, head = revisao_da_imagem()
    esperas: list[float] = []

    def esperar(segundos: float) -> None:
        esperas.append(segundos)
        # O Job grava a revisao durante a espera.
        with engine.begin() as conexao:
            conexao.execute(
                text("INSERT INTO alembic_version VALUES (:v)"), {"v": head}
            )

    with engine.begin() as conexao:
        conexao.execute(text("DELETE FROM alembic_version"))
    try:
        aguardar(engine, esperar=esperar)
    finally:
        with engine.begin() as conexao:
            conexao.execute(text("DELETE FROM alembic_version"))
            conexao.execute(
                text("INSERT INTO alembic_version VALUES (:v)"), {"v": head}
            )

    assert esperas == [2.0]
    espera, pronto = _eventos(log_capturado)
    assert espera["event"] == "migration_wait_database_behind"
    assert espera["database_revision"] is None
    assert pronto["event"] == "migration_wait_done"


def test_erro_que_nao_e_do_driver_sai_em_vez_de_esperar(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def conectar() -> Any:
        raise InvalidRequestError("configuracao errada")

    monkeypatch.setattr(engine, "connect", conectar)

    with pytest.raises(InvalidRequestError):
        aguardar(engine, esperar=_nunca_espera)


def test_imagem_sem_nenhuma_migracao_recusa_em_vez_de_liberar_o_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    (tmp_path / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
    monkeypatch.setattr(modulo_banco, "_RAIZ", tmp_path)

    with pytest.raises(RuntimeError, match="nenhuma migracao"):
        aguardar(engine, esperar=_nunca_espera)


def test_banco_fora_do_ar_e_espera_e_o_log_traz_o_tipo_do_erro(
    log_capturado: io.StringIO,
) -> None:
    inalcancavel = criar_engine("postgresql://x:y@127.0.0.1:1/nada")  # gitleaks:allow

    def esperar(_segundos: float) -> None:
        raise _EsperaInterrompidaError

    try:
        with pytest.raises(_EsperaInterrompidaError):
            aguardar(inalcancavel, esperar=esperar)
    finally:
        inalcancavel.dispose()

    [evento] = _eventos(log_capturado)
    assert evento["event"] == "migration_wait_database_unavailable"
    assert evento["error"] == "OperationalError"


def test_banco_que_volta_do_ar_encerra_a_espera(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, log_capturado: io.StringIO
) -> None:
    conectar = engine.connect
    falhas: list[int] = []

    def conectar_com_uma_falha() -> Any:
        if not falhas:
            falhas.append(1)
            raise OperationalError("SELECT 1", {}, Exception("banco reiniciando"))
        return conectar()

    monkeypatch.setattr(engine, "connect", conectar_com_uma_falha)
    esperas: list[float] = []

    aguardar(engine, esperar=esperas.append)

    assert esperas == [2.0]
    indisponivel, pronto = _eventos(log_capturado)
    assert indisponivel["event"] == "migration_wait_database_unavailable"
    assert pronto["event"] == "migration_wait_done"


def test_comando_aguardar_le_a_database_url_e_nao_aceita_outro_argumento(
    database_url: str, monkeypatch: pytest.MonkeyPatch, log_capturado: io.StringIO
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    # O comando refaria o pipeline de log; aqui vale o do log_capturado.
    monkeypatch.setattr(modulo_banco, "configurar_logging", lambda: None)
    # O comando cala o INFO do alembic (a leitura da revisao o repete a cada
    # espera); o nivel antigo volta no fim do teste.
    alembic = logging.getLogger("alembic")
    monkeypatch.setattr(alembic, "level", alembic.level)

    modulo_banco.main(["aguardar"])

    assert [e["event"] for e in _eventos(log_capturado)] == ["migration_wait_done"]
    assert "Context impl" not in log_capturado.getvalue()
    for argumentos in ([], ["aguarda"], ["aguardar", "restringir"]):
        with pytest.raises(SystemExit, match="uso"):
            modulo_banco.main(argumentos)
