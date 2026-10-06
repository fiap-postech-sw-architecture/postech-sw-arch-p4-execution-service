from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from src.compartilhado.dominio.exceptions import EntidadeDuplicadaException
from src.compartilhado.infraestrutura.database import (
    duplicata_vira_excecao_de_dominio,
)


class _DriverError(Exception):
    def __init__(self, pgcode: str) -> None:
        super().__init__(pgcode)
        self.pgcode = pgcode


def _violacao(pgcode: str) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, _DriverError(pgcode))


def test_violacao_de_unicidade_vira_entidade_duplicada() -> None:
    with (
        pytest.raises(EntidadeDuplicadaException, match="SKU PEC-X"),
        duplicata_vira_excecao_de_dominio("Ja existe item com SKU PEC-X"),
    ):
        raise _violacao("23505")


@pytest.mark.parametrize(
    "pgcode",
    [pytest.param("23514", id="check"), pytest.param("23502", id="not-null")],
)
def test_outras_violacoes_sobem_como_estao(pgcode: str) -> None:
    with pytest.raises(IntegrityError), duplicata_vira_excecao_de_dominio("x"):
        raise _violacao(pgcode)
