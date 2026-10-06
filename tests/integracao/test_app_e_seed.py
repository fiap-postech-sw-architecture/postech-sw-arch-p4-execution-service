from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from src.compartilhado.interfaces.autenticacao import CREDENCIAL_INVALIDA
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import ItemEstoqueSQLAlchemyRepository
from src.estoque.infraestrutura.seed import ITENS_DEMO, main, semear

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session, sessionmaker

    from tests.conftest import ServidorJwks


def _saldos(session_factory: sessionmaker[Session]) -> dict[str, int]:
    with session_factory() as session:
        repo = ItemEstoqueSQLAlchemyRepository(session)
        return {str(i.sku): i.quantidade_disponivel for i in repo.listar(0, 100)}


def test_seed_cria_os_skus_da_demo_e_e_idempotente(
    session_factory: sessionmaker[Session],
) -> None:
    assert semear(session_factory) == [sku for sku, _, _ in ITENS_DEMO]
    assert _saldos(session_factory) == {
        "PEC-AMORTECEDOR": 4,
        "PEC-DISCO-FREIO": 6,
        "PEC-FILTRO-OLEO": 20,
        "PEC-OLEO-5W30": 40,
        "PEC-PASTILHA-FREIO": 10,
        "PEC-VELA": 0,
    }
    assert semear(session_factory) == []
    assert len(_saldos(session_factory)) == 6


def test_seed_nao_mexe_em_saldo_existente(
    session_factory: sessionmaker[Session],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semear(session_factory)
    with session_factory() as session:
        repo = ItemEstoqueSQLAlchemyRepository(session)
        vela = repo.obter_por_sku(Sku("PEC-VELA"), com_lock=True)
        assert vela is not None
        vela.ajustar_quantidade(7)
        session.commit()

    monkeypatch.setenv("DATABASE_URL", database_url)
    main()
    assert _saldos(session_factory)["PEC-VELA"] == 7


def test_saude_metrics_e_swagger(api: TestClient) -> None:
    assert api.get("/api/v1/saude").json() == {"status": "ok"}
    assert "http_request_duration_seconds" in api.get("/metrics").text
    openapi = api.get("/openapi.json").json()
    assert openapi["info"]["title"] == "PytStop Execution Service"
    assert {
        "/api/v1/saude",
        "/api/v1/estoque",
        "/api/v1/estoque/{sku}",
        "/api/v1/estoque/{sku}/quantidade",
        "/api/v1/diagnosticos",
        "/api/v1/diagnosticos/{ordem_id}/inicio",
        "/api/v1/diagnosticos/{ordem_id}/conclusao",
        "/api/v1/fila",
        "/api/v1/execucoes/{ordem_id}/inicio",
        "/api/v1/execucoes/{ordem_id}/finalizacao",
    } <= set(openapi["paths"])
    assert api.get("/docs").status_code == 200


def test_token_do_os_service_validado_pelo_jwks(
    api: TestClient, emitir_token: Callable[..., str]
) -> None:
    valido = {"Authorization": f"Bearer {emitir_token('mecanico')}"}
    # Alem dos 10 s de leeway.
    vencido = {
        "Authorization": "Bearer "
        + emitir_token("mecanico", exp=datetime.now(UTC) - timedelta(seconds=20))
    }
    outro_emissor = {"Authorization": f"Bearer {emitir_token('admin', iss='intruso')}"}

    assert api.get("/api/v1/fila", headers=valido).status_code == 200
    respostas = [
        api.get("/api/v1/fila", headers=headers) for headers in (vencido, outro_emissor)
    ]
    assert [r.status_code for r in respostas] == [401, 401]
    assert respostas[0].json()["erro"]["mensagem"] == CREDENCIAL_INVALIDA
    assert respostas[1].json()["erro"]["mensagem"] == CREDENCIAL_INVALIDA


def test_jwks_fora_do_ar_e_503_com_retry_after_e_sem_token_e_401(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    servidor_jwks_proprio: ServidorJwks,
    emitir_token: Callable[..., str],
) -> None:
    from src.main import criar_app

    servidor_jwks_proprio.status = 503
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("JWKS_URL", servidor_jwks_proprio.url)
    monkeypatch.setenv("BILLING_URL", "http://billing.test")
    with TestClient(criar_app()) as api:
        com_token = api.get(
            "/api/v1/fila", headers={"Authorization": f"Bearer {emitir_token('admin')}"}
        )
        sem_token = api.get("/api/v1/fila")

    assert com_token.status_code == 503
    assert com_token.headers["Retry-After"] == "5"
    assert com_token.json()["erro"]["codigo"] == "SERVICO_INDISPONIVEL"
    assert sem_token.status_code == 401


@pytest.mark.parametrize("ausente", ["DATABASE_URL", "JWKS_URL", "BILLING_URL"])
def test_boot_falha_sem_configuracao_obrigatoria(
    monkeypatch: pytest.MonkeyPatch, database_url: str, jwks_url: str, ausente: str
) -> None:
    from src.main import criar_app

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("JWKS_URL", jwks_url)
    monkeypatch.setenv("BILLING_URL", "http://billing.test")
    monkeypatch.delenv(ausente)
    with pytest.raises(RuntimeError, match=ausente), TestClient(criar_app()):
        pass
