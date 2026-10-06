"""Contrato do catalogo da saga (RFC-004, secao 5.3): tipos e campos de ``dados``.

Renomear uma classe de evento ou um campo muda a mensagem publicada; este teste
quebra antes de o OS Service quebrar.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from uuid import uuid4

import src.diagnostico.aplicacao.events
import src.estoque.aplicacao.events
import src.execucao.aplicacao.events  # noqa: F401 - registra as subclasses
from src.compartilhado.aplicacao.integration_event import IntegrationEvent
from src.diagnostico.aplicacao.events import DiagnosticoConcluidoEvent

CATALOGO = {
    "DiagnosticoIniciado": {"ordem_id", "mecanico_id", "iniciado_em"},
    "DiagnosticoConcluido": {"ordem_id", "itens", "observacoes", "concluido_em"},
    "DiagnosticoDescartado": {"ordem_id"},
    "PecasReservadas": {"ordem_id", "reserva_id"},
    "ReservaDePecasFalhou": {"ordem_id", "faltantes"},
    "ReservaLiberada": {"ordem_id"},
    "ExecucaoAgendada": {"ordem_id", "posicao_na_fila"},
    "ExecucaoCancelada": {"ordem_id"},
    "ExecucaoIniciada": {"ordem_id", "mecanico_id", "iniciada_em"},
    "ExecucaoFinalizada": {"ordem_id", "finalizada_em", "pecas_consumidas"},
}


def _campos_de_dados(classe: type[IntegrationEvent]) -> set[str]:
    return {f.name for f in fields(classe)} - {"id", "ocorrido_em"}


def test_servico_emite_exatamente_os_eventos_do_catalogo() -> None:
    classes = IntegrationEvent.__subclasses__()
    tipos = {
        c.__name__.removesuffix("Event"): c
        for c in classes
        if c.__module__.startswith("src.")
    }
    assert set(tipos) == set(CATALOGO)
    for tipo, campos in CATALOGO.items():
        assert _campos_de_dados(tipos[tipo]) == campos, tipo


def test_listas_aninhadas_seguem_o_catalogo() -> None:
    from src.diagnostico.aplicacao.events import ItemDTO
    from src.estoque.aplicacao.events import FaltanteDTO
    from src.execucao.aplicacao.events import PecaConsumidaDTO

    assert [f.name for f in fields(ItemDTO)] == ["tipo", "codigo", "quantidade"]
    assert [f.name for f in fields(FaltanteDTO)] == [
        "sku",
        "solicitado",
        "disponivel",
    ]
    assert [f.name for f in fields(PecaConsumidaDTO)] == ["sku", "quantidade"]


def test_repr_do_evento_nao_leva_as_observacoes() -> None:
    evento = DiagnosticoConcluidoEvent(
        ordem_id=uuid4(),
        itens=(),
        observacoes="ligar para Joao",
        concluido_em=datetime.now(UTC),
    )
    assert "Joao" not in repr(evento)
