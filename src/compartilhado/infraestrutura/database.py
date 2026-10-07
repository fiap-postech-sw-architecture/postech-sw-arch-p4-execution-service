from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from sqlalchemy import MetaData, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import registry, sessionmaker

from src.compartilhado.dominio.exceptions import EntidadeDuplicadaException
from src.compartilhado.infraestrutura.ambiente import inteiro_opcional

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session

metadata = MetaData()
# Registry unico dos mapeamentos imperativos: cada ``mapping.py`` de contexto
# mapeia o proprio agregado no import do modulo.
mapper_registry = registry(metadata=metadata)

_VIOLACAO_DE_UNICIDADE: Final = "23505"  # SQLSTATE unique_violation

# Limites do servidor, em ms (variavel de ambiente, padrao). O lock pessimista
# da reserva continua bloqueante, mas nao espera para sempre.
_TIMEOUTS_DO_SERVIDOR: Final = (
    ("lock_timeout", "DB_LOCK_TIMEOUT_MS", 5000),
    ("statement_timeout", "DB_STATEMENT_TIMEOUT_MS", 15000),
    ("idle_in_transaction_session_timeout", "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 30000),
)


# Banco que some sem fechar o socket (failover, NAT): sem isto um comando
# esperaria o timeout de TCP do sistema (minutos), e o handler do consumidor
# seguraria a conexao AMQP sem heartbeat. Dado sem confirmacao por 10 s, ou
# conexao ociosa sem resposta em cerca de 1 min, derruba a conexao.
_SOCKET_SEM_RESPOSTA: Final = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 10_000,
}


def criar_engine(url: str) -> Engine:
    """Engine do servico, com pool e limites de tempo ajustaveis por env.

    ``hide_parameters``: erro de banco nunca leva os valores do statement
    (texto livre, placa) para mensagem, log ou traceback. Servidor: lock 5 s,
    statement 15 s e transacao ociosa 30 s (``DB_*_MS``). Cliente: conexao em
    3 s, socket sem resposta derrubado em 10 s (keepalive e
    ``tcp_user_timeout``) e espera por conexao do pool em 5 s; pool de 5 + 10
    (``DB_POOL_*``, ``DB_MAX_OVERFLOW``, ``DB_CONNECT_TIMEOUT_S``).
    ``pool_pre_ping`` descarta
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
        connect_args={**argumentos_de_conexao(), "options": opcoes},
    )


def argumentos_de_conexao() -> dict[str, int]:
    """Conexao em ate ``DB_CONNECT_TIMEOUT_S`` (3 s) e socket sem resposta derrubado.

    Os mesmos para o pool e para a conexao dedicada do ``LISTEN`` do relay.
    """
    return {
        "connect_timeout": inteiro_opcional("DB_CONNECT_TIMEOUT_S", 3),
        **_SOCKET_SEM_RESPOSTA,
    }


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
        "error": type(exc).__name__,
        "pgcode": getattr(exc.orig, "pgcode", None),
        "constraint": getattr(diagnostico, "constraint_name", None),
    }


def violacao_de_unicidade(exc: IntegrityError) -> bool:
    """O ``IntegrityError`` veio de uma UNIQUE (ou chave primaria) do banco."""
    return getattr(exc.orig, "pgcode", None) == _VIOLACAO_DE_UNICIDADE


@contextmanager
def duplicata_vira_excecao_de_dominio(mensagem: str) -> Iterator[None]:
    """``IntegrityError`` de UNIQUE no bloco vira ``EntidadeDuplicadaException``.

    Fecha a corrida do verifica-depois-insere entre duas transacoes: na API a
    perdedora responde 409; num comando da saga o caso de uso rele a linha da
    vencedora e segue a regra de repeticao. Outras violacoes (CHECK, NOT NULL)
    sobem como estao.
    """
    try:
        yield
    except IntegrityError as exc:
        if not violacao_de_unicidade(exc):
            raise
        raise EntidadeDuplicadaException(mensagem) from exc
