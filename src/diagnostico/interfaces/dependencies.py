"""Composicao dos casos de uso do diagnostico na sessao do request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.diagnostico.aplicacao.use_cases import (
    ConcluirDiagnostico,
    IniciarDiagnostico,
    ListarDiagnosticos,
)
from src.diagnostico.infraestrutura.adapters import CatalogoDePecasSQLAlchemy
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
from src.diagnostico.infraestrutura.validador_billing import ValidadorDeItensBilling

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from starlette.requests import Request

    from src.compartilhado.interfaces.autenticacao import UsuarioAutenticado


def obter_listar_diagnosticos(session: Session) -> ListarDiagnosticos:
    return ListarDiagnosticos(DiagnosticoSQLAlchemyRepository(session))


def obter_iniciar_diagnostico(session: Session) -> IniciarDiagnostico:
    return IniciarDiagnostico(
        DiagnosticoSQLAlchemyRepository(session), SQLAlchemyUnitOfWork(lambda: session)
    )


def obter_concluir_diagnostico(
    session: Session, request: Request, usuario: UsuarioAutenticado
) -> ConcluirDiagnostico:
    # Cliente HTTP e circuit breaker sao do processo (lifespan); o token e o do
    # mecanico deste request, repassado ao Billing.
    validador = ValidadorDeItensBilling(
        cliente=request.app.state.billing_client,
        breaker=request.app.state.billing_breaker,
        authorization=usuario.authorization,
    )
    return ConcluirDiagnostico(
        repo=DiagnosticoSQLAlchemyRepository(session),
        catalogo=CatalogoDePecasSQLAlchemy(session),
        validador=validador,
        uow=SQLAlchemyUnitOfWork(lambda: session),
    )
