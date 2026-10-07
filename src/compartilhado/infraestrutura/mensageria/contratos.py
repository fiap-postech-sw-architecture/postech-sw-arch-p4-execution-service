"""Contratos de mensageria copiados do platform (``contratos/``, SHA em ``ORIGEM``).

Envelope e ``dados`` de cada tipo sao conferidos pelo JSON Schema 2020-12 do
contrato: na gravacao da outbox (falha e defeito deste servico) e no consumo
(falha e erro permanente: a mensagem vai para a DLQ). Leitor tolerante: os
schemas nao fecham ``additionalProperties``, entao campo novo nao invalida; ja
``versao`` desconhecida invalida (RFC-004, secao 5.5).
"""

from __future__ import annotations

import json
import re
from datetime import UTC
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import best_match

from src.compartilhado.aplicacao.outbox import dados_do_evento

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from src.compartilhado.aplicacao.integration_event import IntegrationEvent

CONTRATOS: Final = Path(__file__).resolve().parents[4] / "contratos"
ORIGEM: Final = "execution-service"
VERSAO: Final = 1
EXCHANGE_EVENTOS: Final = "pytstop.eventos"
_ENVELOPE: Final = "envelope"


class MensagemInvalidaError(Exception):
    """Mensagem fora do contrato: envelope, ``dados``, tipo ou versao.

    A mensagem diz onde e qual regra falhou, nunca o valor recebido (placa e
    texto livre viajam no ``dados``).
    """


@cache
def _validadores() -> dict[str, Draft202012Validator]:
    # Indexados pelo nome do arquivo: o tipo recebido so escolhe uma chave do
    # dicionario, nunca monta um caminho de arquivo.
    return {
        caminho.name.removesuffix(".schema.json"): Draft202012Validator(
            json.loads(caminho.read_text(encoding="utf-8")),
            format_checker=FormatChecker(),
        )
        for caminho in (CONTRATOS / "schemas").glob("*.schema.json")
    }


def tipos_com_contrato() -> frozenset[str]:
    """Tipos de mensagem com schema copiado (sem o do envelope)."""
    return frozenset(_validadores()) - {_ENVELOPE}


def _conferir(validador: Draft202012Validator, instancia: object, onde: str) -> None:
    erro = best_match(validador.iter_errors(instancia))
    if erro is not None:
        msg = f"{onde} fora do contrato em {erro.json_path} ({erro.validator})"
        raise MensagemInvalidaError(msg)


def validar(envelope: Mapping[str, Any]) -> None:
    """Confere o envelope, a ``versao`` e o ``dados`` do tipo.

    Raises:
        MensagemInvalidaError: envelope ou ``dados`` fora do schema, ``versao``
            desconhecida ou tipo sem contrato.
    """
    validadores = _validadores()
    _conferir(validadores[_ENVELOPE], envelope, "envelope")
    if envelope["versao"] != VERSAO:
        msg = f"versao {envelope['versao']} desconhecida (conhecida: {VERSAO})"
        raise MensagemInvalidaError(msg)
    tipo = envelope["tipo"]
    if tipo not in tipos_com_contrato():
        msg = f"tipo sem contrato: {tipo}"
        raise MensagemInvalidaError(msg)
    _conferir(validadores[tipo], envelope["dados"], "dados")


@cache
def _produtores() -> dict[str, str]:
    asyncapi = yaml.safe_load((CONTRATOS / "asyncapi.yaml").read_text(encoding="utf-8"))
    return {
        operacao["messages"][0]["$ref"].rsplit("/", 1)[-1]: operacao["bindings"][
            "amqp"
        ]["userId"]
        for operacao in asyncapi["operations"].values()
        if operacao["action"] == "send"
    }


def produtor(tipo: str) -> str | None:
    """Usuario do RabbitMQ que publica o tipo: o ``userId`` da operacao no AsyncAPI."""
    return _produtores().get(tipo)


def routing_key(tipo: str) -> str:
    """``evento.execucao.<tipo em snake_case>`` (convencao do AsyncAPI do platform)."""
    return "evento.execucao." + re.sub(r"(?<!^)(?=[A-Z])", "_", tipo).lower()


def envelope_do_evento(
    evento: IntegrationEvent, causation_id: UUID | None
) -> dict[str, Any]:
    """Envelope do evento, ja conferido contra o contrato.

    ``causation_id`` e o ``id`` do comando que causou o evento, ou ``None`` quando
    a causa e uma requisicao HTTP (acao do mecanico ou do admin).

    Raises:
        MensagemInvalidaError: o evento nao cumpre o contrato (defeito do servico).
    """
    envelope = {
        "id": str(evento.id),
        "tipo": evento.tipo,
        "versao": VERSAO,
        "origem": ORIGEM,
        "correlation_id": str(evento.ordem_id),
        "causation_id": None if causation_id is None else str(causation_id),
        "ocorrido_em": evento.ocorrido_em.astimezone(UTC).isoformat(),
        "dados": dados_do_evento(evento),
    }
    validar(envelope)
    return envelope
