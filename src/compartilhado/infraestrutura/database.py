from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import registry, sessionmaker

from src.compartilhado.infraestrutura.ambiente import inteiro_opcional

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session

metadata = MetaData()
# Registry unico dos mapeamentos imperativos: cada ``mapping.py`` de contexto
# mapeia o proprio agregado no import do modulo.
mapper_registry = registry(metadata=metadata)

# Limites do servidor, em ms (variavel de ambiente, padrao). O lock pessimista
# da reserva continua bloqueante, mas nao espera para sempre.
_TIMEOUTS_DO_SERVIDOR: Final = (
    ("lock_timeout", "DB_LOCK_TIMEOUT_MS", 5000),
    ("statement_timeout", "DB_STATEMENT_TIMEOUT_MS", 15000),
    ("idle_in_transaction_session_timeout", "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 30000),
)


def criar_engine(url: str) -> Engine:
    """Engine do servico, com pool e limites de tempo ajustaveis por env.

    ``hide_parameters``: erro de banco nunca leva os valores do statement
    (texto livre, placa) para mensagem, log ou traceback. Servidor: lock 5 s,
    statement 15 s e transacao ociosa 30 s (``DB_*_MS``). Cliente: conexao em
    3 s e espera por conexao do pool em 5 s; pool de 5 + 10 (``DB_POOL_*``,
    ``DB_MAX_OVERFLOW``, ``DB_CONNECT_TIMEOUT_S``). ``pool_pre_ping`` descarta
    conexao morta apos restart do banco; ``pool_recycle`` evita conexao presa
    em pod de vida longa.
    """
    opcoes = " ".join(
        f"-c {parametro}={inteiro_opcional(variavel, padrao)}"
        for parametro, variavel, padrao in _TIMEOUTS_DO_SERVIDOR
    )
    return create_engine(
        url,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=inteiro_opcional("DB_POOL_SIZE", 5),
        max_overflow=inteiro_opcional("DB_MAX_OVERFLOW", 10),
        pool_timeout=inteiro_opcional("DB_POOL_TIMEOUT_S", 5),
        connect_args={
            "connect_timeout": inteiro_opcional("DB_CONNECT_TIMEOUT_S", 3),
            "options": opcoes,
        },
    )


def criar_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: os casos de uso devolvem o agregado depois do
    # commit (com a sessao ja fechada pela UoW); expirar os atributos dispararia
    # refresh em sessao fechada (DetachedInstanceError).
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def descrever_erro_de_banco(exc: DBAPIError) -> dict[str, str | None]:
    """O que identifica o erro sem dado do request: classe, SQLSTATE e constraint.

    A mensagem do driver fica de fora: o ``DETAIL`` do Postgres traz a linha
    inteira (texto livre, placa) numa violacao de constraint.
    """
    diagnostico = getattr(exc.orig, "diag", None)
    return {
        "erro": type(exc).__name__,
        "pgcode": getattr(exc.orig, "pgcode", None),
        "constraint": getattr(diagnostico, "constraint_name", None),
    }
