from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import registry, sessionmaker

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

metadata = MetaData()
# Registry unico dos mapeamentos imperativos: cada ``mapping.py`` de contexto
# mapeia o proprio agregado no import do modulo.
mapper_registry = registry(metadata=metadata)


def criar_engine(url: str) -> Engine:
    # pool_pre_ping descarta conexao morta apos restart do banco; pool_recycle
    # evita conexao presa em pod de vida longa. Pool default (5 + 10).
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800)


def criar_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: os casos de uso devolvem o agregado depois do
    # commit (com a sessao ja fechada pela UoW); expirar os atributos dispararia
    # refresh em sessao fechada (DetachedInstanceError).
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
