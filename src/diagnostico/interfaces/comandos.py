"""Comandos da saga para o diagnostico, entregues pelo consumidor (RFC-004 5.3).

O envelope chega validado pelo contrato; o caso de uso roda na sessao e na
unidade de trabalho da mensagem, e o consumidor comita efeito, resposta e
idempotencia juntos.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.compartilhado.dominio.veiculo import Veiculo
from src.diagnostico.aplicacao.use_cases import (
    DescartarDiagnostico,
    RegistrarSolicitacaoDeDiagnostico,
)
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWorkDoComando


def solicitar_diagnostico(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
) -> None:
    dados = envelope["dados"]
    retrato = dados["veiculo"]
    RegistrarSolicitacaoDeDiagnostico(
        DiagnosticoSQLAlchemyRepository(sessao), uow
    ).executar(
        UUID(dados["ordem_id"]),
        Veiculo(
            veiculo_id=UUID(dados["veiculo_id"]),
            placa=retrato["placa"],
            marca=retrato["marca"],
            modelo=retrato["modelo"],
            ano=retrato["ano"],
        ),
        dados["descricao_problema"],
        solicitacao_id=UUID(envelope["id"]),
    )


def descartar_diagnostico(
    envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
) -> None:
    dados = envelope["dados"]
    DescartarDiagnostico(DiagnosticoSQLAlchemyRepository(sessao), uow).executar(
        UUID(dados["ordem_id"])
    )
