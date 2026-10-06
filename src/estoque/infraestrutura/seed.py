"""Seed idempotente do estoque de demonstracao.

Uso: ``python -m src.estoque.infraestrutura.seed`` (le ``DATABASE_URL``).

Cria so os SKUs que ainda nao existem e nunca altera saldo de item ja
cadastrado (reexecutar e no-op). Os codigos sao os mesmos da tabela de precos
do Billing; ``PEC-VELA`` nasce com saldo zero de proposito: e o cenario de falta
de peca (``ReservaDePecasFalhou``) da demo da saga.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import structlog

from src.compartilhado.dominio.exceptions import EntidadeDuplicadaException
from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.estoque.aplicacao.use_cases import CriarItemEstoque
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import ItemEstoqueSQLAlchemyRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

_log = structlog.get_logger(__name__)

ITENS_DEMO: Final = (
    ("PEC-OLEO-5W30", "Oleo 5W30 (litro)", 40),
    ("PEC-FILTRO-OLEO", "Filtro de oleo", 20),
    ("PEC-PASTILHA-FREIO", "Jogo de pastilhas de freio", 10),
    ("PEC-DISCO-FREIO", "Disco de freio", 6),
    ("PEC-AMORTECEDOR", "Amortecedor dianteiro", 4),
    ("PEC-VELA", "Vela de ignicao", 0),
)


def _criar(session: Session, sku: str, nome: str, quantidade: int) -> bool:
    criar = CriarItemEstoque(
        ItemEstoqueSQLAlchemyRepository(session), SQLAlchemyUnitOfWork(lambda: session)
    )
    try:
        criar.executar(sku=Sku(sku), nome=nome, quantidade_disponivel=quantidade)
    except EntidadeDuplicadaException:
        return False
    return True


def semear(session_factory: Callable[[], Session]) -> list[str]:
    """Cadastra os SKUs de demonstracao ausentes; devolve os que foram criados."""
    criados: list[str] = []
    for sku, nome, quantidade in ITENS_DEMO:
        with session_factory() as session:
            if _criar(session, sku, nome, quantidade):
                criados.append(sku)
    return criados


def main() -> None:
    configurar_logging()
    engine = criar_engine(os.environ["DATABASE_URL"])
    try:
        criados = semear(criar_session_factory(engine))
    finally:
        engine.dispose()
    _log.info(
        "stock_seed_done", criados=criados, ja_existentes=len(ITENS_DEMO) - len(criados)
    )


if __name__ == "__main__":
    main()
