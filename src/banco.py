"""Operacoes do banco que o Kubernetes pede: ``python -m src.banco <comando>``.

``aguardar`` e o initContainer ``aguarda-migracao`` dos Deployments: espera o
Job de migracao da versao da imagem, e so espera. Sai com sucesso quando o
banco esta na ``head`` da imagem ou a frente dela (depois de um
``kubectl rollout undo``, a imagem anterior sobe sobre o banco que o Job novo
migrou, e a migracao so expande e contrai: o pod revertido nao pode ficar preso
no Init); espera com o banco atras ou fora do ar. Nunca cria tabela nem grava
``alembic_version``.

``restringir`` e o passo do Job depois do ``alembic upgrade head``: tira do
papel da aplicacao a escrita na ``alembic_version`` (ele a le, para o
``aguardar``, mas a revisao do banco nao e dele).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.compartilhado.infraestrutura.ambiente import url_de_conexao
from src.compartilhado.infraestrutura.database import criar_engine
from src.compartilhado.infraestrutura.logging import configurar_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import Engine

_log = structlog.get_logger(__name__)

_RAIZ: Final = Path(__file__).resolve().parents[1]
# O papel da aplicacao criado pelo k8s/base/papeis.sql.
_PAPEL_DA_APLICACAO: Final = "execucao_app"


def revisao_da_imagem() -> tuple[ScriptDirectory, str]:
    """As migracoes desta imagem e a ``head`` delas."""
    config = Config(str(_RAIZ / "alembic.ini"))
    config.set_main_option("script_location", str(_RAIZ / "migrations"))
    script = ScriptDirectory.from_config(config)
    return script, script.get_current_head() or ""


def aguardar(
    engine: Engine,
    *,
    intervalo: float = 2.0,
    esperar: Callable[[float], None] = time.sleep,
) -> None:
    """Espera o banco na ``head`` da imagem, ou a frente dela.

    Banco fora do ar, sem ``alembic_version`` e revisao conhecida mas atras da
    ``head`` sao espera, e cada espera loga o motivo; o prazo e o do rollout no
    Kubernetes. Erro que nao e do banco sai na hora.
    """
    script, head = revisao_da_imagem()
    conhecidas = {revisao.revision for revisao in script.walk_revisions()}
    while True:
        try:
            with engine.connect() as conexao:
                atual = MigrationContext.configure(conexao).get_current_revision()
        except SQLAlchemyError as exc:
            _log.warning(
                "migration_wait_database_unavailable", error=type(exc).__name__
            )
            esperar(intervalo)
            continue
        if atual == head:
            _log.info("migration_wait_done", database_revision=atual, reason="at_head")
            return
        if atual is not None and atual not in conhecidas:
            # Banco a frente da imagem: nao e espera.
            _log.info(
                "migration_wait_done",
                database_revision=atual,
                image_head=head,
                reason="ahead_of_image",
            )
            return
        _log.info(
            "migration_wait_database_behind",
            database_revision=atual,
            image_head=head,
            retry_in_seconds=intervalo,
        )
        esperar(intervalo)


def restringir_versao(engine: Engine) -> None:
    """Revoga do papel da aplicacao a escrita na ``alembic_version``.

    Roda como o dono do banco, que criou a tabela, depois de migrar. Sem isto, os
    privilegios padrao do ``papeis.sql`` (DML em toda tabela do dono) deixariam
    uma API comprometida apagar ou trocar a revisao do banco: o proximo
    ``alembic upgrade head`` falharia, ou o ``aguardar`` liberaria todo pod.

    Raises:
        RuntimeError: o papel ainda escreve na tabela (o REVOKE de quem nao e o
            dono dela nao revoga nada, so avisa).
    """
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "REVOKE INSERT, UPDATE, DELETE ON alembic_version "
                f"FROM {_PAPEL_DA_APLICACAO}"
            )
        )
        # Quem nao e dono da tabela nem tem grant option recebe so um aviso do
        # servidor, e o REVOKE nao faz nada: confere o efeito.
        ainda_escreve = conexao.execute(
            text("SELECT has_table_privilege(:papel, 'alembic_version', :escrita)"),
            {"papel": _PAPEL_DA_APLICACAO, "escrita": "INSERT, UPDATE, DELETE"},
        ).scalar_one()
    if ainda_escreve:
        msg = f"{_PAPEL_DA_APLICACAO} ainda escreve na alembic_version"
        raise RuntimeError(msg)
    _log.info("alembic_version_restricted", role=_PAPEL_DA_APLICACAO)


def main(args: Sequence[str] = ()) -> None:
    """``aguardar`` espera a migracao; ``restringir`` fecha a escrita na versao."""
    if list(args) not in (["aguardar"], ["restringir"]):
        sys.exit("uso: python -m src.banco (aguardar | restringir)")
    configurar_logging()
    # O alembic loga o contexto (Context impl, transactional DDL) a cada leitura
    # da revisao, e a espera le a cada 2 s.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    engine = criar_engine(url_de_conexao("DATABASE_URL"))
    try:
        if args[0] == "aguardar":
            aguardar(engine)
        else:
            restringir_versao(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
