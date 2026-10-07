"""Consumidor dos comandos da saga (ADR-036): origem, contrato, idempotencia e retry.

Cada mensagem da fila do servico abre um span CONSUMER filho da publicacao, tem
o envelope conferido pelo contrato e a origem pelo ``user_id`` (comandos so vem
do orquestrador; a copia de retry, com ``x-tentativa``, vem do proprio
consumidor) e vai ao handler do tipo, com ``mensagens_processadas`` gravada na
mesma transacao do efeito. Desfecho:

- processada, ignorada (o comando nao corresponde ao estado atual e o handler o
  descarta, regra dos participantes da saga) ou duplicada (``id`` ja
  processado): ack;
- erro transitorio (banco, rede, dependencia): copia em ``pytstop.retry`` com a
  routing key = fila, ``expiration`` crescente e ``x-tentativa`` + 1, confirmada
  pelo broker antes do ack da original;
- erro permanente (contrato, tipo, versao, origem ou dado recusado pelo
  dominio) ou cinco tentativas esgotadas: ``basic_reject`` sem requeue (DLQ).
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
from pika.exceptions import AMQPError
from prometheus_client import Counter
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

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

# ``x-tentativa`` 1 a 5: espera na ``.retry`` antes de voltar a fila (RFC-004).
ATRASOS_MS: Final = (1000, 5000, 15000, 60000, 300000)
FILA: Final = "execucao.comandos"
# Todo comando que o servico consome vem do orquestrador (usuario ``os``).
_PRODUTOR: Final = "os"
_PREFETCH: Final = 5
# Volta do laco: limite para notar o pedido de parada e tocar o heartbeat.
_TICK_S: Final = 0.5
_RETENCAO_DAS_PROCESSADAS: Final = timedelta(days=30)
_LIMPEZA_A_CADA_S: Final = 3600.0
_TIPO_DESCONHECIDO: Final = "desconhecido"
# Erro de dominio que passa com o tempo: dependencia fora ou a corrida que a
# releitura nao resolveu. O resto dos erros de dominio e estado, nao falha.
_DOMINIO_TRANSITORIO: Final = (
    DependenciaIndisponivelException,
    EntidadeDuplicadaException,
)

# Recebe o ``dados`` ja validado e roda o caso de uso na sessao e UoW da mensagem.
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
    if type(tentativa) is not int or tentativa < 0:
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
        *,
        atrasos_ms: tuple[int, ...] = ATRASOS_MS,
    ) -> None:
        self._session_factory = session_factory
        self._url = rabbitmq_url
        self._usuario = usuario_da_url(rabbitmq_url)
        self._handlers = handlers
        self._sinais = sinais
        self._atrasos_ms = atrasos_ms
        self._backoff = Backoff()
        self._conexao: BlockingConnection | None = None
        self._proxima_limpeza = time.monotonic()

    def executar(self, parar: threading.Event) -> None:
        """Laco principal: a mensagem em curso termina antes de ``parar`` valer."""
        try:
            while not parar.is_set():
                self._sinais.heartbeat()
                try:
                    if self._conexao is None:
                        self._conexao = self._assinar()
                    self._sinais.pronto()
                    self._conexao.process_data_events(time_limit=_TICK_S)
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

    def _assinar(self) -> BlockingConnection:
        conexao, canal = abrir_canal(
            self._url, filas=(FILA,), exchanges=(EXCHANGE_RETRY,)
        )
        canal.basic_qos(prefetch_count=_PREFETCH)
        canal.basic_consume(FILA, self._ao_receber)
        _log.info("consumer_subscribed", fila=FILA)
        return conexao

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
        """Envelope valido de um tipo com handler, vindo do produtor esperado.

        Raises:
            MensagemInvalidaError: erro permanente (vai para a DLQ).
        """
        # Na copia de retry o user_id e o do proprio consumidor, que a republicou;
        # a conferencia do produtor ja aconteceu na primeira entrega.
        if user_id != _PRODUTOR and not (tentativa and user_id == self._usuario):
            msg = f"user_id {user_id!r} nao e o produtor dos comandos"
            raise MensagemInvalidaError(msg)
        try:
            envelope: dict[str, Any] = json.loads(corpo)
        except (ValueError, RecursionError) as exc:
            msg = "corpo nao e JSON valido"
            raise MensagemInvalidaError(msg) from exc
        validar(envelope)
        if envelope["tipo"] not in self._handlers:
            msg = f"tipo sem handler neste servico: {envelope['tipo']}"
            raise MensagemInvalidaError(msg)
        return envelope

    def _executar(self, envelope: Mapping[str, Any], tentativa: int) -> Resultado:
        try:
            return self._rodar_handler(envelope)
        except MensagemJaProcessadaError:
            return Resultado.DUPLICADA
        except ValorInvalidoError:
            # So a classe: a mensagem pode ecoar o dado recebido.
            _log.warning("command_rejected_by_domain")
            return Resultado.DLQ
        except _DOMINIO_TRANSITORIO as exc:
            return self._falha_transitoria(exc, tentativa)
        except DomainException as exc:
            _log.info("command_ignored", codigo=exc.codigo)
            return Resultado.IGNORADA
        except Exception as exc:  # noqa: BLE001 - banco, rede ou defeito: retry ate a DLQ
            return self._falha_transitoria(exc, tentativa)

    def _rodar_handler(self, envelope: Mapping[str, Any]) -> Resultado:
        mensagem_id = UUID(envelope["id"])
        processada = select(mensagens_processadas_table.c.mensagem_id).where(
            mensagens_processadas_table.c.mensagem_id == mensagem_id
        )
        with self._session_factory() as sessao:
            if sessao.execute(processada).first() is not None:
                return Resultado.DUPLICADA
            uow = SQLAlchemyUnitOfWork(lambda: sessao, mensagem_de_origem=mensagem_id)
            self._handlers[envelope["tipo"]](envelope["dados"], sessao, uow)
            return Resultado.PROCESSADA if uow.comitou else Resultado.IGNORADA

    def _falha_transitoria(self, exc: Exception, tentativa: int) -> Resultado:
        esgotou = tentativa >= len(self._atrasos_ms)
        evento = "message_retries_exhausted" if esgotou else "message_failed_will_retry"
        if isinstance(exc, DBAPIError):
            # Sem a mensagem do driver: o DETAIL do Postgres traz a linha inteira.
            _log.warning(evento, **descrever_erro_de_banco(exc))
        else:
            _log.warning(evento, exc_info=exc)
        return Resultado.DLQ if esgotou else Resultado.RETRY

    def _nova_tentativa(
        self,
        canal: BlockingChannel,
        envelope: Mapping[str, Any],
        headers: Mapping[str, Any],
        corpo: bytes,
        tentativa: int,
    ) -> None:
        """Copia em ``pytstop.retry``; sem confirmacao, a original fica sem ack.

        Raises:
            AMQPError: copia sem rota, recusada ou broker fora; o laco reconecta
                e o broker reentrega a original.
        """
        contexto = {
            k: v for k, v in headers.items() if k in {"traceparent", "tracestate"}
        }
        canal.basic_publish(
            exchange=EXCHANGE_RETRY,
            routing_key=FILA,
            body=corpo,
            properties=propriedades(
                envelope,
                usuario=self._usuario,
                headers={**contexto, "x-tentativa": tentativa},
                expiration=str(self._atrasos_ms[tentativa - 1]),
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
