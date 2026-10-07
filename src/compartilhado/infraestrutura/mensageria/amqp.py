"""Conexao com o RabbitMQ (pika) pelo usuario do servico, com publisher confirms.

A topologia (exchanges, filas, bindings e policies) vem do ``definitions.json``
do platform; o usuario do servico nao tem permissao de configure e so confere,
por declaracao passiva, o que le ou escreve. Recurso alheio responderia 403 e
fecharia o canal (ADR-036).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Final

import pika
from pika.exceptions import AMQPError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection

EXCHANGE_RETRY: Final = "pytstop.retry"
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
        OSError: nome do broker sem resolucao no DNS (``socket.gaierror``, que
            o pika nao embrulha); tambem broker fora, com backoff.
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
