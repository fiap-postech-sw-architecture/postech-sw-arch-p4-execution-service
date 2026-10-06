from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

import pytest

from src.compartilhado.aplicacao.integration_event import IntegrationEvent
from src.compartilhado.aplicacao.outbox import dados_do_evento


class _Tipo(StrEnum):
    PECA = "peca"


@dataclass(frozen=True, slots=True)
class _Linha:
    tipo: _Tipo
    quantidade: int


@dataclass(frozen=True, kw_only=True)
class AlgoAconteceuEvent(IntegrationEvent):
    responsavel: UUID
    quando: datetime
    linhas: tuple[_Linha, ...]
    nota: str | None = None


@dataclass(frozen=True, kw_only=True)
class _ComFloatEvent(IntegrationEvent):
    valor: float


def test_tipo_e_o_nome_da_classe_sem_o_sufixo_event() -> None:
    evento = AlgoAconteceuEvent(
        ordem_id=uuid4(), responsavel=uuid4(), quando=datetime.now(UTC), linhas=()
    )
    assert evento.tipo == "AlgoAconteceu"


def test_envelope_tem_id_unico_e_instante_em_utc() -> None:
    a = AlgoAconteceuEvent(
        ordem_id=uuid4(), responsavel=uuid4(), quando=datetime.now(UTC), linhas=()
    )
    b = AlgoAconteceuEvent(
        ordem_id=a.ordem_id, responsavel=a.responsavel, quando=a.quando, linhas=()
    )
    assert a.id != b.id
    assert a.ocorrido_em.tzinfo is UTC


def test_dados_em_tipos_json_sem_metadados_do_envelope() -> None:
    ordem_id, responsavel = uuid4(), uuid4()
    quando = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    evento = AlgoAconteceuEvent(
        ordem_id=ordem_id,
        responsavel=responsavel,
        quando=quando,
        linhas=(_Linha(tipo=_Tipo.PECA, quantidade=2),),
    )

    assert dados_do_evento(evento) == {
        "ordem_id": str(ordem_id),
        "responsavel": str(responsavel),
        "quando": "2026-10-06T12:00:00+00:00",
        "linhas": [{"tipo": "peca", "quantidade": 2}],
        "nota": None,
    }


def test_float_e_recusado_no_commit_e_nao_no_relay() -> None:
    # Valor monetario viaja como string decimal (RFC-004, secao 5.2), nunca float.
    evento = _ComFloatEvent(ordem_id=uuid4(), valor=1.5)
    with pytest.raises(TypeError, match="Tipo nao suportado"):
        dados_do_evento(evento)
