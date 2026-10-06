from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import structlog
from structlog.testing import capture_logs

import src.compartilhado.aplicacao.responsavel as modulo
from src.compartilhado.aplicacao.responsavel import (
    registrar_auditoria,
    responsavel_efetivo,
)

MECANICO, ADMIN = uuid4(), uuid4()


@pytest.fixture(autouse=True)
def _logger_fresco(monkeypatch: pytest.MonkeyPatch) -> None:
    # capture_logs nao intercepta logger ja cacheado (cache_logger_on_first_use).
    monkeypatch.setattr(modulo, "_log", structlog.get_logger("test_responsavel"))


@pytest.mark.parametrize(
    ("atual", "usuario", "pelo_admin", "esperado"),
    [
        pytest.param(MECANICO, MECANICO, False, MECANICO, id="o-proprio-mecanico"),
        pytest.param(MECANICO, ADMIN, True, MECANICO, id="admin-em-nome-dele"),
        pytest.param(None, ADMIN, True, ADMIN, id="admin-sem-responsavel"),
        pytest.param(MECANICO, ADMIN, False, ADMIN, id="outro-sem-ser-admin"),
    ],
)
def test_responsavel_efetivo(
    atual: UUID | None, usuario: UUID, pelo_admin: bool, esperado: UUID
) -> None:
    assert responsavel_efetivo(atual, usuario, pelo_admin=pelo_admin) == esperado


def test_auditoria_registra_quem_o_que_e_onde() -> None:
    with capture_logs() as logs:
        registrar_auditoria(
            "finalizar_execucao", ator_id=ADMIN, alvo="ordem-1", mecanico_id="m"
        )
    assert logs == [
        {
            "event": "audit",
            "acao": "finalizar_execucao",
            "ator_id": str(ADMIN),
            "alvo": "ordem-1",
            "mecanico_id": "m",
            "log_level": "info",
        }
    ]
