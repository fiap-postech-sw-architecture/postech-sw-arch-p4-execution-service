"""Conexao com o RabbitMQ (pika) pelo usuario do servico, com publisher confirms.

A topologia (exchanges, filas, bindings e policies) vem do ``definitions.json``
do platform; o usuario do servico nao tem permissao de configure e so confere,
por declaracao passiva, o que le ou escreve. Recurso alheio responderia 403 e
fecharia o canal (ADR-036).
"""

from __future__ import annotations

import contextlib
import socket
from typing import TYPE_CHECKING, Any, Final

import pika
from pika.adapters.utils.connection_workflow import AMQPConnectorStackTimeout
from pika.exceptions import AMQPError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection

EXCHANGE_RETRY: Final = "pytstop.retry"
# Broker fora, que o laco do processo trata com backoff e fora da prontidao: o
# AMQPError e os dois erros que o pika deixa sair crus da abertura e que tambem
# sao broker fora, o nome sem resolucao no DNS (socket.gaierror: o Service
# headless do RabbitMQ some do DNS sem pod pronto, no boot ou depois de uma
# queda) e o prazo da pilha vencido (AMQPConnectorStackTimeout: o broker aceitou
# o TCP e nao respondeu o AMQP). Nao e todo OSError: descritores esgotados e
# falha de TLS tambem saem crus da abertura, e reconectar em laco esconderia o
# defeito; o processo cai e o Kubernetes o reinicia. Depois de aberta a conexao,
# o pika ja embrulha o erro de socket em AMQPError.
BROKER_FORA: Final = (AMQPError, socket.gaierror, AMQPConnectorStackTimeout)
# Broker com alarme de memoria ou disco segura os publishers; depois disto a
# conexao cai e o processo reconecta, em vez de ficar parado sem sinal.
_BLOQUEIO_MAXIMO_S: Final = 30.0
# Heartbeat explicito (o padrao do broker, 60 s, sem depender dele): o handler
# do consumidor roda na thread da conexao e nenhum heartbeat sai enquanto ele
# roda. O tempo maximo dele fica abaixo disto pelos tetos do banco na
# transacao da mensagem (consumidor.py), e o broker so derruba a conexao depois
# de dois heartbeats sem resposta.
_HEARTBEAT_S: Final = 60


def usuario_da_url(url: str) -> str:
    """Usuario do RabbitMQ na URL: vai na propriedade ``user_id`` de toda publicacao."""
    usuario: str = pika.URLParameters(url).credentials.username
    return usuario


def abrir_canal(
    url: str, *, exchanges: Sequence[str] = (), filas: Sequence[str] = ()
) -> tuple[BlockingConnection, BlockingChannel]:
    """Conecta, confere a topologia e devolve o canal em modo de confirmacao.

    Raises:
        AMQPError: broker fora, credencial recusada ou recurso ausente (404) ou
            sem permissao (403); quem chama tenta de novo com backoff.
        socket.gaierror: nome do broker sem resolucao no DNS, que o pika nao
            embrulha; tambem broker fora (``BROKER_FORA``).
        AMQPConnectorStackTimeout: o broker aceitou o TCP e nao respondeu o
            AMQP no prazo da pilha do pika; tambem broker fora.
        OSError: descritores esgotados ou falha de TLS, crus do pika; nao e
            broker fora e derruba o processo.
    """
    parametros = pika.URLParameters(url)
    parametros.heartbeat = _HEARTBEAT_S
    parametros.blocked_connection_timeout = _BLOQUEIO_MAXIMO_S
    conexao = pika.BlockingConnection(parametros)
    try:
        canal = conexao.channel()
        for exchange in exchanges:
            canal.exchange_declare(exchange, passive=True)
        for fila in filas:
            canal.queue_declare(fila, passive=True)
        canal.confirm_delivery()
    except AMQPError:
        fechar(conexao)
        raise
    return conexao, canal


def fechar(conexao: BlockingConnection | None) -> None:
    """Fecha a conexao se ainda estiver aberta; conexao morta nao e erro aqui."""
    with contextlib.suppress(AMQPError, OSError):
        if conexao is not None and conexao.is_open:
            conexao.close()


def propriedades(
    envelope: Mapping[str, Any], *, usuario: str, headers: Mapping[str, Any]
) -> pika.BasicProperties:
    """Propriedades AMQP do envelope (RFC-004, secao 5.2), persistente."""
    return pika.BasicProperties(
        message_id=envelope["id"],
        correlation_id=envelope["correlation_id"],
        type=envelope["tipo"],
        user_id=usuario,
        content_type="application/json",
        delivery_mode=pika.DeliveryMode.Persistent,
        headers=dict(headers),
    )
