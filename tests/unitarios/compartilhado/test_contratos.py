"""Contratos copiados do platform: exemplos validos, erros sem dado e envelope."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

import src.diagnostico.aplicacao.events
import src.estoque.aplicacao.events
import src.execucao.aplicacao.events  # noqa: F401 - registra as subclasses
from src.compartilhado.aplicacao.integration_event import IntegrationEvent
from src.compartilhado.infraestrutura.mensageria.contratos import (
    CONTRATOS,
    MensagemInvalidaError,
    envelope_do_evento,
    routing_key,
    tipos_com_contrato,
    validar,
)
from src.consumidor import HANDLERS
from src.estoque.aplicacao.events import ReservaLiberadaEvent

if TYPE_CHECKING:
    from pathlib import Path

EXEMPLOS = sorted((CONTRATOS / "exemplos").glob("*.json"))


def _exemplo(tipo: str) -> dict[str, Any]:
    conteudo: dict[str, Any] = json.loads(
        (CONTRATOS / "exemplos" / f"{tipo}.json").read_text()
    )
    return conteudo


def _eventos_do_servico() -> dict[str, type[IntegrationEvent]]:
    return {
        classe.__name__.removesuffix("Event"): classe
        for classe in IntegrationEvent.__subclasses__()
        if classe.__module__.startswith("src.")
    }


@pytest.mark.parametrize("caminho", EXEMPLOS, ids=lambda p: p.stem)
def test_exemplo_do_platform_cumpre_o_contrato(caminho: Path) -> None:
    validar(json.loads(caminho.read_text()))


def test_todo_tipo_produzido_ou_consumido_tem_schema_e_exemplo() -> None:
    produzidos = set(_eventos_do_servico())
    consumidos = set(HANDLERS)
    exemplos = {caminho.stem for caminho in EXEMPLOS}
    assert produzidos | consumidos == tipos_com_contrato() == exemplos


def test_comandos_consumidos_sao_do_orquestrador_e_eventos_sao_deste_servico() -> None:
    for tipo in HANDLERS:
        assert _exemplo(tipo)["origem"] == "os-service", tipo
    for tipo in _eventos_do_servico():
        assert _exemplo(tipo)["origem"] == "execution-service", tipo


def test_campos_dos_eventos_sao_os_do_schema() -> None:
    for tipo, classe in _eventos_do_servico().items():
        schema = json.loads((CONTRATOS / "schemas" / f"{tipo}.schema.json").read_text())
        campos = {f.name for f in fields(classe)} - {"id", "ocorrido_em"}
        assert campos == set(schema["required"]) == set(schema["properties"]), tipo


def test_dado_fora_do_contrato_diz_onde_sem_ecoar_o_valor() -> None:
    comando = _exemplo("SolicitarDiagnostico")
    comando["dados"]["veiculo"]["placa"] = "ABC-1234"

    with pytest.raises(MensagemInvalidaError) as erro:
        validar(comando)

    assert str(erro.value) == "dados fora do contrato em $.veiculo.placa (pattern)"


@pytest.mark.parametrize(
    ("campo", "valor", "mensagem"),
    [
        pytest.param("versao", 2, "versao 2 desconhecida", id="versao"),
        pytest.param("tipo", "GerarOrcamento", "tipo sem contrato", id="tipo"),
        pytest.param("ocorrido_em", "ontem", "envelope fora do contrato", id="data"),
    ],
)
def test_envelope_fora_do_contrato(campo: str, valor: object, mensagem: str) -> None:
    comando = {**_exemplo("LiberarReserva"), campo: valor}
    with pytest.raises(MensagemInvalidaError, match=mensagem):
        validar(comando)


def test_campo_desconhecido_e_tolerado() -> None:
    comando = _exemplo("LiberarReserva")
    comando["dados"]["campo_novo"] = "x"
    validar({**comando, "campo_novo": 1})


def test_routing_key_e_o_tipo_em_snake_case() -> None:
    assert routing_key("ReservaDePecasFalhou") == (
        "evento.execucao.reserva_de_pecas_falhou"
    )


def test_envelope_do_evento_sai_em_utc_com_a_causa() -> None:
    brasilia = timezone(timedelta(hours=-3))
    evento = ReservaLiberadaEvent(
        ordem_id=uuid4(), ocorrido_em=datetime(2026, 10, 6, 9, 0, tzinfo=brasilia)
    )
    causa = uuid4()

    envelope = envelope_do_evento(evento, causa)

    assert envelope["ocorrido_em"] == "2026-10-06T12:00:00+00:00"
    assert envelope["causation_id"] == str(causa)
    assert envelope["correlation_id"] == str(evento.ordem_id)
