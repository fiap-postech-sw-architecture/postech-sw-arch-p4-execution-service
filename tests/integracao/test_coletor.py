"""Gauges por consulta da API contra o PostgreSQL das migracoes.

A fixture e conhecida: cada caso grava as linhas com a idade exata que quer e
confere o valor que o coletor devolve na raspagem.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import text

from src.coletor import JANELA_DE_NEGOCIO, ColetorDaApi
from src.compartilhado.infraestrutura.database import criar_engine_de_metricas
from src.main import criar_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection, Engine

DIA = timedelta(days=1)
IDADE = "pytstop_outbox_mais_antiga_segundos"
DURACAO = "pytstop_execucao_duracao_media_segundos"
FALTA = "pytstop_pecas_em_falta"


def _coletar(engine: Engine) -> dict[str, dict[tuple[str, ...], float]]:
    """O que o coletor devolve: familia -> valores dos rotulos -> valor."""
    return {
        familia.name: {
            tuple(amostra.labels.values()): amostra.value for amostra in familia.samples
        }
        for familia in ColetorDaApi(engine.connect).collect()
    }


def _item(tipo: str, codigo: str) -> dict[str, Any]:
    return {"tipo": tipo, "codigo": codigo, "quantidade": 1}


def _execucao(
    conexao: Connection,
    itens: list[dict[str, Any]],
    *,
    finalizada_ha: timedelta | None,
    duracao: timedelta = timedelta(minutes=10),
    status: str = "FINALIZADA",
) -> None:
    """Diagnostico e execucao da mesma ordem; ``finalizada_ha`` nulo = em execucao."""
    ordem_id = uuid4()
    agora = datetime.now(UTC)
    finalizada = None if finalizada_ha is None else agora - finalizada_ha
    iniciada = (finalizada or agora - timedelta(hours=1)) - (
        duracao if finalizada else timedelta(0)
    )
    conexao.execute(
        text(
            "INSERT INTO diagnosticos (ordem_id, status, itens, observacoes, "
            "solicitado_em) VALUES (:id, 'CONCLUIDO', CAST(:itens AS jsonb), '', now())"
        ),
        {"id": ordem_id, "itens": json.dumps(itens)},
    )
    conexao.execute(
        text(
            "INSERT INTO execucoes (ordem_id, status, prioridade, enfileirada_em, "
            "mecanico_id, iniciada_em, finalizada_em) VALUES (:id, :status, "
            "'normal', now(), gen_random_uuid(), :iniciada, :finalizada)"
        ),
        {
            "id": ordem_id,
            "status": status,
            "iniciada": iniciada,
            "finalizada": finalizada,
        },
    )


def _reserva(
    conexao: Connection, status: str, faltantes: list[str], criada_ha: timedelta
) -> None:
    conexao.execute(
        text(
            "INSERT INTO reservas (id, ordem_id, status, itens, faltantes, "
            "criada_em) VALUES (gen_random_uuid(), :ordem, :status, '[]', "
            "CAST(:faltantes AS jsonb), :criada)"
        ),
        {
            "ordem": uuid4(),
            "status": status,
            "faltantes": json.dumps(
                [{"sku": sku, "solicitado": 2, "disponivel": 0} for sku in faltantes]
            ),
            "criada": datetime.now(UTC) - criada_ha,
        },
    )


def _linha_da_outbox(conexao: Connection, status: str, criada_ha: timedelta) -> None:
    conexao.execute(
        text(
            "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
            "routing_key, envelope, status, criado_em) VALUES (gen_random_uuid(), "
            "'ReservaLiberada', gen_random_uuid(), 'pytstop.eventos', "
            "'evento.execucao.reserva_liberada', '{}', :status, :criado)"
        ),
        {"status": status, "criado": datetime.now(UTC) - criada_ha},
    )


def test_janela_de_negocio_e_de_sete_dias() -> None:
    assert JANELA_DE_NEGOCIO == 7 * DIA


def test_duracao_media_por_servico_conta_so_as_finalizadas_da_janela(
    engine: Engine,
) -> None:
    with engine.begin() as conexao:
        # SRV-A: 100 s e 300 s. SRV-B: 300 s e 600 s (a peca nao entra).
        _execucao(
            conexao,
            [_item("servico", "SRV-A")],
            finalizada_ha=DIA,
            duracao=timedelta(seconds=100),
        )
        _execucao(
            conexao,
            [_item("servico", "SRV-A"), _item("servico", "SRV-B"), _item("peca", "P")],
            finalizada_ha=JANELA_DE_NEGOCIO - timedelta(hours=1),
            duracao=timedelta(seconds=300),
        )
        # O mesmo servico em dois itens conta uma vez para a execucao.
        _execucao(
            conexao,
            [_item("servico", "SRV-B"), _item("servico", "SRV-B")],
            finalizada_ha=DIA,
            duracao=timedelta(seconds=600),
        )
        # Fora da janela, em execucao e cancelada nao contam.
        _execucao(
            conexao,
            [_item("servico", "SRV-A")],
            finalizada_ha=JANELA_DE_NEGOCIO + timedelta(hours=1),
            duracao=timedelta(seconds=5000),
        )
        _execucao(
            conexao,
            [_item("servico", "SRV-A")],
            finalizada_ha=None,
            status="EM_EXECUCAO",
        )
        _execucao(
            conexao,
            [_item("servico", "SRV-A")],
            finalizada_ha=None,
            status="CANCELADA",
        )

    medias = _coletar(engine)[DURACAO]

    assert medias == {
        ("SRV-A",): pytest.approx(200.0),
        ("SRV-B",): pytest.approx(450.0),
    }


def test_pecas_em_falta_conta_as_reservas_recusadas_da_janela_por_sku(
    engine: Engine,
) -> None:
    with engine.begin() as conexao:
        _reserva(conexao, "RECUSADA", ["PEC-A", "PEC-B"], DIA)
        _reserva(conexao, "RECUSADA", ["PEC-A"], 2 * DIA)
        _reserva(conexao, "RECUSADA", ["PEC-C"], JANELA_DE_NEGOCIO - timedelta(hours=1))
        # Fora da janela e as que nao foram recusadas nao contam.
        _reserva(conexao, "RECUSADA", ["PEC-A", "PEC-D"], JANELA_DE_NEGOCIO + DIA)
        _reserva(conexao, "ATIVA", [], DIA)
        _reserva(conexao, "LIBERADA", [], DIA)

    faltas = _coletar(engine)[FALTA]

    assert faltas == {("PEC-A",): 2.0, ("PEC-B",): 1.0, ("PEC-C",): 1.0}


def test_sem_dado_na_janela_as_familias_saem_sem_serie(engine: Engine) -> None:
    coletado = _coletar(engine)

    assert coletado[DURACAO] == {}
    assert coletado[FALTA] == {}


def test_idade_da_outbox_e_a_da_linha_pendente_mais_antiga(engine: Engine) -> None:
    with engine.begin() as conexao:
        _linha_da_outbox(conexao, "pendente", timedelta(seconds=120))
        _linha_da_outbox(conexao, "pendente", timedelta(seconds=10))
        # Entregue e dead, mesmo mais velhas, nao contam.
        _linha_da_outbox(conexao, "entregue", 3 * DIA)
        _linha_da_outbox(conexao, "dead", 5 * DIA)

    (idade,) = _coletar(engine)[IDADE].values()

    # O relogio do banco (container) anda a parte do do teste: margem de 1 s.
    assert 119 <= idade < 130


def test_idade_da_outbox_sem_pendente_e_zero(engine: Engine) -> None:
    with engine.begin() as conexao:
        _linha_da_outbox(conexao, "entregue", 3 * DIA)

    assert _coletar(engine)[IDADE] == {(): 0.0}


@pytest.mark.parametrize(
    ("tabela", "ausente"),
    [
        pytest.param("outbox", IDADE, id="outbox"),
        pytest.param("execucoes", DURACAO, id="execucoes"),
        pytest.param("reservas", FALTA, id="reservas"),
    ],
)
def test_tabela_travada_omite_so_o_gauge_dela_dentro_do_prazo(
    engine: Engine, tabela: str, ausente: str
) -> None:
    with engine.connect() as trava, trava.begin():
        trava.execute(text(f"LOCK TABLE {tabela} IN ACCESS EXCLUSIVE MODE"))
        inicio = time.monotonic()
        coletado = _coletar(engine)
        gasto = time.monotonic() - inicio

    # O lock_timeout de 1 s da consulta, e nao os 5 s do engine nem o statement
    # timeout de 15 s.
    assert gasto < 4
    assert set(coletado) == {IDADE, DURACAO, FALTA} - {ausente}


def test_banco_fora_deixa_os_gauges_ausentes_e_a_raspagem_de_pe() -> None:
    morto = criar_engine_de_metricas(
        "postgresql://x:y@127.0.0.1:1/nada"
    )  # gitleaks:allow
    coletor = ColetorDaApi(morto.connect)

    assert list(coletor.collect()) == []
    assert {f.name for f in coletor.describe()} == {IDADE, DURACAO, FALTA}


@pytest.fixture
def ambiente_da_api(
    database_url: str, jwks_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("JWKS_URL", jwks_url)
    monkeypatch.setenv("BILLING_URL", "http://billing.test")


@pytest.fixture
def api_com_uma_linha_pendente(
    ambiente_da_api: None, engine: Engine
) -> Iterator[TestClient]:
    with engine.begin() as conexao:
        _linha_da_outbox(conexao, "pendente", timedelta(seconds=60))
    with TestClient(criar_app()) as cliente:
        yield cliente


def test_metrics_da_api_traz_os_gauges_por_consulta(
    api_com_uma_linha_pendente: TestClient,
) -> None:
    resposta = api_com_uma_linha_pendente.get("/metrics")

    assert resposta.status_code == 200
    for familia in (IDADE, DURACAO, FALTA):
        assert f"# TYPE {familia} gauge" in resposta.text
    idade = REGISTRY.get_sample_value(IDADE)
    assert idade is not None
    assert 59 <= idade < 70


def test_coletor_sai_do_registro_com_o_app_e_o_proximo_app_registra_de_novo(
    ambiente_da_api: None,
) -> None:
    for _ in range(2):
        with TestClient(criar_app()):
            assert REGISTRY.get_sample_value(IDADE) == 0.0
        assert REGISTRY.get_sample_value(IDADE) is None
