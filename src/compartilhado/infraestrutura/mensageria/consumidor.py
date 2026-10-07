"""Consumidor dos comandos da saga (ADR-036): origem, contrato, idempotencia e retry.

Cada mensagem da fila do servico abre um span CONSUMER filho da publicacao (os
logs do handler saem dentro dele), tem o envelope conferido pelo contrato e a
origem pelo ``user_id`` (o produtor do tipo no AsyncAPI; a copia de retry, com
``x-tentativa``, vem do proprio consumidor) e vai ao handler do tipo, com
``mensagens_processadas`` gravada na mesma transacao do efeito. Desfecho:

- processada, ignorada (o comando nao corresponde ao estado atual e o handler o
  descarta, regra dos participantes da saga) ou duplicada (``id`` ja
  processado): ack;
- erro transitorio (banco, rede, dependencia fora): copia em ``pytstop.retry``,
  sem ``expiration``, com ``x-tentativa`` + 1 e a routing key da fila de retry do
  nivel da nova tentativa (``<fila>.retry.1s`` ate ``.300s``, cujo TTL devolve a
  copia a fila), confirmada pelo broker antes do ack da original;
- erro permanente (contrato, tipo, versao, origem, dado recusado pelo dominio
  ou erro nao classificado) ou falha depois da quinta copia: ``basic_reject``
  sem requeue (DLQ).
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import structlog
from opentelemetry.trace import SpanKind, StatusCode
from pika.exceptions import AMQPError, ConsumerCancelled
from prometheus_client import Counter
from sqlalchemy import delete, func, select
from sqlalchemy.exc import (
    DBAPIError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelException,
    DomainException,
    EntidadeDuplicadaException,
    ValorInvalidoError,
)
from src.compartilhado.infraestrutura.database import descrever_erro_de_banco
from src.compartilhado.infraestrutura.mensageria.amqp import (
    EXCHANGE_RETRY,
    abrir_canal,
    fechar,
    propriedades,
    usuario_da_url,
)
from src.compartilhado.infraestrutura.mensageria.contratos import (
    MensagemInvalidaError,
    produtor,
    validar,
)
from src.compartilhado.infraestrutura.mensageria.processo import Backoff
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_de,
    tracer,
)
from src.compartilhado.infraestrutura.outbox_mapping import (
    mensagens_processadas_table,
)
from src.compartilhado.infraestrutura.unit_of_work import (
    MensagemJaProcessadaError,
    SQLAlchemyUnitOfWork,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Mapping

    import pika
    from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection
    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.compartilhado.infraestrutura.mensageria.processo import SinaisDoProcesso

_log = structlog.get_logger(__name__)

FILA: Final = "execucao.comandos"
# Fila de retry de cada tentativa (x-tentativa 1 a 5); o atraso e o TTL dela.
NIVEIS_DE_RETRY: Final = tuple(
    f"{FILA}.retry.{atraso}" for atraso in ("1s", "5s", "15s", "60s", "300s")
)
# Uma mensagem em voo por vez: a venenosa (header que o pika nao decodifica
# derruba a conexao a cada entrega) volta sozinha ao broker e sai pela DLQ no
# delivery-limit da fila, sem arrastar as validas de um lote pre-buscado.
_PREFETCH: Final = 1
# Volta do laco: limite para notar o pedido de parada e tocar o heartbeat.
_TICK_S: Final = 0.5
_RETENCAO_DAS_PROCESSADAS: Final = timedelta(days=30)
_LIMPEZA_A_CADA_S: Final = 3600.0
_TIPO_DESCONHECIDO: Final = "desconhecido"
# Erro que passa com o tempo: banco ou rede fora, dependencia fora ou a corrida
# que a releitura nao resolveu. Erro de dominio fora daqui e estado, nao falha;
# o resto (defeito, dado que o banco recusa) vai direto para a DLQ.
_TRANSITORIOS: Final = (
    OperationalError,
    InterfaceError,
    PoolTimeoutError,
    OSError,
    DependenciaIndisponivelException,
    EntidadeDuplicadaException,
)

# Recebe o envelope ja validado e roda o caso de uso na sessao e UoW da mensagem.
type HandlerDeComando = Callable[[Mapping[str, Any], Session, UnitOfWork], None]


class Resultado(StrEnum):
    PROCESSADA = "processada"
    DUPLICADA = "duplicada"
    IGNORADA = "ignorada"
    RETRY = "retry"
    DLQ = "dlq"


_CONSUMIDAS = Counter(
    "pytstop_mensagens_consumidas_total",
    "Mensagens consumidas pelo servico, por tipo e desfecho.",
    ["tipo", "resultado"],
)


def _tentativa(headers: Mapping[str, Any]) -> int:
    """``x-tentativa`` da copia de retry (0 na primeira entrega)."""
    tentativa = headers.get("x-tentativa", 0)
    if type(tentativa) is not int or not 0 <= tentativa <= len(NIVEIS_DE_RETRY):
        msg = "header x-tentativa invalido"
        raise MensagemInvalidaError(msg)
    return tentativa


class Consumidor:
    """Consome a fila de comandos do servico ate ``parar`` ser ligado."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        rabbitmq_url: str,
        handlers: Mapping[str, HandlerDeComando],
        sinais: SinaisDoProcesso,
        backoff: Backoff | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._url = rabbitmq_url
        self._usuario = usuario_da_url(rabbitmq_url)
        self._handlers = handlers
        self._sinais = sinais
        self._backoff = backoff or Backoff()
        self._conexao: BlockingConnection | None = None
        self._cancelado = False
        self._proxima_limpeza = time.monotonic()

    def executar(self, parar: threading.Event) -> None:
        """Laco principal: a mensagem em curso termina antes de ``parar`` valer.

        Broker fora, copia de retry recusada (o canal fecha) ou assinatura
        cancelada pelo broker: loga, fecha e assina de novo com backoff.
        """
        try:
            while not parar.is_set():
                self._sinais.heartbeat()
                try:
                    self._consumir()
                    self._backoff.reiniciar()
                except AMQPError as exc:
                    self._sinais.indisponivel()
                    _log.warning(
                        "consumer_broker_unavailable", error=type(exc).__name__
                    )
                    fechar(self._conexao)
                    self._conexao = None
                    self._backoff.esperar(parar)
                self._limpar_processadas_antigas()
        finally:
            fechar(self._conexao)
            self._conexao = None
            self._sinais.indisponivel()

    def _consumir(self) -> None:
        if self._conexao is None:
            self._conexao = self._assinar()
        self._sinais.pronto()
        self._conexao.process_data_events(time_limit=_TICK_S)
        if self._cancelado:
            # O pika so avisa pelo callback; sem isto o processo seguiria vivo
            # e pronto sem receber nada.
            msg = "assinatura cancelada pelo broker"
            raise ConsumerCancelled(msg)

    def _assinar(self) -> BlockingConnection:
        conexao, canal = abrir_canal(
            self._url, filas=(FILA,), exchanges=(EXCHANGE_RETRY,)
        )
        self._cancelado = False
        canal.basic_qos(prefetch_count=_PREFETCH)
        canal.add_on_cancel_callback(self._ao_ser_cancelado)
        canal.basic_consume(FILA, self._ao_receber)
        _log.info("consumer_subscribed", fila=FILA)
        return conexao

    def _ao_ser_cancelado(self, _frame: object) -> None:
        # Fila apagada ou failover: o broker cancela o consumidor (Basic.Cancel).
        self._cancelado = True

    def _ao_receber(
        self,
        canal: BlockingChannel,
        entrega: pika.spec.Basic.Deliver,
        props: pika.BasicProperties,
        corpo: bytes,
    ) -> None:
        headers = props.headers or {}
        tipo = props.type if props.type in self._handlers else _TIPO_DESCONHECIDO
        atributos = {
            "messaging.system": "rabbitmq",
            "messaging.destination.name": FILA,
            "messaging.message.id": props.message_id,
            "messaging.message.conversation_id": props.correlation_id,
        }
        with tracer.start_as_current_span(
            f"process {tipo}",
            context=contexto_de(headers),
            kind=SpanKind.CONSUMER,
            attributes={k: v for k, v in atributos.items() if v is not None},
        ) as span:
            resultado, envelope, tentativa = self._processar(props, corpo)
            span.set_attribute("pytstop.resultado", resultado.value)
            if resultado in {Resultado.RETRY, Resultado.DLQ}:
                span.set_status(StatusCode.ERROR, resultado.value)
            if resultado is Resultado.RETRY and envelope is not None:
                self._nova_tentativa(canal, envelope, headers, corpo, tentativa + 1)
            if resultado is Resultado.DLQ:
                canal.basic_reject(entrega.delivery_tag, requeue=False)
            else:
                canal.basic_ack(entrega.delivery_tag)
        _CONSUMIDAS.labels(tipo, resultado.value).inc()

    def _processar(
        self, props: pika.BasicProperties, corpo: bytes
    ) -> tuple[Resultado, dict[str, Any] | None, int]:
        try:
            tentativa = _tentativa(props.headers or {})
            envelope = self._abrir(props.user_id, corpo, tentativa)
        except MensagemInvalidaError as exc:
            _log.warning(
                "message_rejected",
                motivo_rejeicao=str(exc),
                message_id=props.message_id,
            )
            return Resultado.DLQ, None, 0
        with structlog.contextvars.bound_contextvars(
            correlation_id=envelope["correlation_id"],
            message_id=envelope["id"],
            tipo=envelope["tipo"],
        ):
            resultado = self._executar(envelope, tentativa)
            _log.info(
                "message_consumed", resultado=resultado.value, tentativa=tentativa
            )
        return resultado, envelope, tentativa

    def _abrir(
        self, user_id: str | None, corpo: bytes, tentativa: int
    ) -> dict[str, Any]:
        """Envelope valido de um tipo com handler, vindo do produtor do tipo.

        Raises:
            MensagemInvalidaError: erro permanente (vai para a DLQ).
        """
        try:
            envelope: dict[str, Any] = json.loads(corpo)
        except (ValueError, RecursionError) as exc:
            msg = "corpo nao e JSON valido"
            raise MensagemInvalidaError(msg) from exc
        validar(envelope)
        tipo = envelope["tipo"]
        if tipo not in self._handlers:
            msg = f"tipo sem handler neste servico: {tipo}"
            raise MensagemInvalidaError(msg)
        # Na copia de retry o user_id e o do proprio consumidor, que a republicou;
        # a conferencia do produtor ja aconteceu na primeira entrega.
        if user_id != produtor(tipo) and not (tentativa and user_id == self._usuario):
            msg = f"user_id {user_id!r} nao e o produtor de {tipo}"
            raise MensagemInvalidaError(msg)
        return envelope

    def _executar(self, envelope: Mapping[str, Any], tentativa: int) -> Resultado:
        try:
            return self._rodar_handler(envelope)
        except MensagemJaProcessadaError:
            return Resultado.DUPLICADA
        except _TRANSITORIOS as exc:
            return self._falha_transitoria(exc, tentativa)
        except DomainException as exc:
            _log.info("command_ignored", codigo=exc.codigo)
            return Resultado.IGNORADA
        except ValorInvalidoError:
            # So a classe: a mensagem pode ecoar o dado recebido.
            _log.warning("command_rejected_by_domain")
            return Resultado.DLQ
        except Exception as exc:  # noqa: BLE001 - nao classificado vai para a DLQ
            _log.error("command_failed", **_descricao_do_erro(exc))
            return Resultado.DLQ

    def _rodar_handler(self, envelope: Mapping[str, Any]) -> Resultado:
        mensagem_id = UUID(envelope["id"])
        processada = select(mensagens_processadas_table.c.mensagem_id).where(
            mensagens_processadas_table.c.mensagem_id == mensagem_id
        )
        with self._session_factory() as sessao:
            if sessao.execute(processada).first() is not None:
                return Resultado.DUPLICADA
            uow = SQLAlchemyUnitOfWork(lambda: sessao, mensagem_de_origem=mensagem_id)
            self._handlers[envelope["tipo"]](envelope, sessao, uow)
            return Resultado.PROCESSADA if uow.comitou else Resultado.IGNORADA

    @staticmethod
    def _falha_transitoria(exc: Exception, tentativa: int) -> Resultado:
        esgotou = tentativa >= len(NIVEIS_DE_RETRY)
        evento = "message_retries_exhausted" if esgotou else "message_failed_will_retry"
        _log.warning(evento, **_descricao_do_erro(exc))
        return Resultado.DLQ if esgotou else Resultado.RETRY

    def _nova_tentativa(
        self,
        canal: BlockingChannel,
        envelope: Mapping[str, Any],
        headers: Mapping[str, Any],
        corpo: bytes,
        tentativa: int,
    ) -> None:
        """Copia na fila de retry do nivel; sem confirmacao, a original fica sem ack.

        Raises:
            AMQPError: copia sem rota, recusada (o canal fecha) ou broker fora; o
                laco reconecta com backoff e o broker reentrega a original.
        """
        contexto = {
            k: v for k, v in headers.items() if k in {"traceparent", "tracestate"}
        }
        canal.basic_publish(
            exchange=EXCHANGE_RETRY,
            routing_key=NIVEIS_DE_RETRY[tentativa - 1],
            body=corpo,
            properties=propriedades(
                envelope,
                usuario=self._usuario,
                headers={**contexto, "x-tentativa": tentativa},
            ),
            mandatory=True,
        )

    def _limpar_processadas_antigas(self) -> None:
        agora = time.monotonic()
        if agora < self._proxima_limpeza:
            return
        self._proxima_limpeza = agora + _LIMPEZA_A_CADA_S
        # Relogio do banco, o mesmo do default de processada_em.
        limite = func.now() - _RETENCAO_DAS_PROCESSADAS
        try:
            with self._session_factory() as sessao:
                apagadas = (
                    sessao.connection()
                    .execute(
                        delete(mensagens_processadas_table).where(
                            mensagens_processadas_table.c.processada_em < limite
                        )
                    )
                    .rowcount
                )
                sessao.commit()
        except SQLAlchemyError as exc:
            _log.warning("processed_messages_cleanup_failed", error=type(exc).__name__)
            return
        _log.info("processed_messages_cleanup", apagadas=apagadas)


def _descricao_do_erro(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DBAPIError):
        # Sem a mensagem do driver: o DETAIL do Postgres traz a linha inteira.
        return dict(descrever_erro_de_banco(exc))
    return {"exc_info": exc}
