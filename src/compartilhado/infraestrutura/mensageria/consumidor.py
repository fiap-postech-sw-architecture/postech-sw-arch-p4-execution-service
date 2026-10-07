"""Consumidor dos comandos da saga (ADR-036): origem, contrato, idempotencia e retry.

Cada mensagem da fila do servico abre um span CONSUMER filho da publicacao (os
logs do handler saem dentro dele), tem o envelope conferido pelo contrato e a
origem pelo ``user_id`` (o produtor do tipo no AsyncAPI; a copia de retry, com
``x-tentativa``, vem do proprio consumidor) e vai ao handler do tipo, preso a
transacao da mensagem: o consumidor grava o ``id`` em ``mensagens_processadas``,
roda o handler e comita uma vez so (efeito, respostas na outbox e o ``id``
juntos ou nada). Desfecho:

- processada, ignorada (o comando nao corresponde ao estado atual e o handler o
  descarta, regra dos participantes da saga) ou duplicada (``id`` ja
  processado): ack;
- erro transitorio (banco, rede, dependencia fora): copia em ``pytstop.retry``,
  sem ``expiration``, com ``x-tentativa`` + 1 e a routing key da fila de retry do
  nivel da nova tentativa (``<fila>.retry.1s`` ate ``.300s``, cujo TTL devolve a
  copia a fila), confirmada pelo broker antes do ack da original; copia sem rota
  ou com nack leva a original para a DLQ;
- erro permanente (contrato, tipo, versao, origem, dado recusado pelo dominio
  ou erro nao classificado) ou falha depois da quinta copia: ``basic_reject``
  sem requeue (DLQ).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import structlog
from opentelemetry.trace import SpanKind, StatusCode
from pika.exceptions import AMQPError, ConsumerCancelled, NackError, UnroutableError
from prometheus_client import Counter
from sqlalchemy import delete, func, select, text
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
    RespostaInvalidaDaDependenciaException,
    ValorInvalidoError,
)
from src.compartilhado.infraestrutura.database import (
    criar_session_factory,
    descrever_erro_de_banco,
)
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
from src.compartilhado.infraestrutura.mensageria.processo import (
    LOTE_DA_LIMPEZA,
    Backoff,
    Periodico,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_de,
    tracer,
)
from src.compartilhado.infraestrutura.outbox_mapping import (
    mensagens_processadas_table,
    registrar_processada,
)
from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator, Mapping

    import pika
    from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWorkDoComando
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
_TIPO_DESCONHECIDO: Final = "desconhecido"
# Tetos do banco na transacao da mensagem, abaixo dos da API: o handler roda na
# thread da conexao AMQP, sem heartbeat, e o pior caso (pool 5 s, conexao 3 s e
# ate 8 comandos de 5 s, cada espera de lock dentro do comando) fica abaixo do
# heartbeat de 60 s. Comando que passa do teto vira erro transitorio (retry).
_TETOS_DO_BANCO: Final = (
    "SET LOCAL statement_timeout = '5s'",
    "SET LOCAL lock_timeout = '3s'",
)
# O envelope da saga tem poucos KB; acima disto e entrada hostil (JSON aninhado
# que estoura a recursao do parser ou da validacao), direto para a DLQ.
_CORPO_MAXIMO: Final = 64 * 1024
_TAMANHO_DO_UUID: Final = 36
# Erro que passa com o tempo, e vira copia de retry: banco fora, conexao
# fechada, pool cheio ou timeout (lock e statement timeout, deadlock e falha de
# serializacao chegam como OperationalError), rede, dependencia fora e a corrida
# que a releitura nao resolveu (a entrega seguinte cai na regra de repeticao e
# republica o desfecho). Erro do broker no ack, no reject ou na copia sai do
# callback: o laco reconecta e a original volta.
_TRANSITORIOS: Final = (
    OperationalError,
    InterfaceError,
    PoolTimeoutError,
    OSError,
    DependenciaIndisponivelException,
    EntidadeDuplicadaException,
)
# Erro que repetir nao muda, direto para a DLQ: dado recusado pelo dominio ou
# pela dependencia. O resto da ``DomainException`` e estado (comando ignorado) e
# qualquer erro nao classificado tambem vai para a DLQ.
_PERMANENTES: Final = (ValorInvalidoError, RespostaInvalidaDaDependenciaException)

# Recebe o envelope ja validado e roda o caso de uso na sessao e na unidade de
# trabalho da mensagem; quem comita e o consumidor.
type HandlerDeComando = Callable[
    [Mapping[str, Any], Session, UnitOfWorkDoComando], None
]


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
        engine: Engine,
        rabbitmq_url: str,
        handlers: Mapping[str, HandlerDeComando],
        sinais: SinaisDoProcesso,
        backoff: Backoff | None = None,
    ) -> None:
        self._engine = engine
        self._sessoes = criar_session_factory(engine)
        self._url = rabbitmq_url
        self._usuario = usuario_da_url(rabbitmq_url)
        self._handlers = handlers
        self._sinais = sinais
        self._backoff = backoff or Backoff()
        self._conexao: BlockingConnection | None = None
        self._cancelado = False
        self._limpeza = Periodico()

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
        try:
            self._cancelado = False
            canal.basic_qos(prefetch_count=_PREFETCH)
            canal.add_on_cancel_callback(self._ao_ser_cancelado)
            canal.basic_consume(FILA, self._ao_receber)
        except BaseException:
            # A conexao ainda nao e do laco: sem fechar aqui, ela vazaria.
            fechar(conexao)
            raise
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
        # Propriedades ainda nao conferidas: no span so o que tem forma de UUID.
        atributos = {
            "messaging.system": "rabbitmq",
            "messaging.destination.name": FILA,
            "messaging.message.id": _uuid_ou_none(props.message_id),
            "messaging.message.conversation_id": _uuid_ou_none(props.correlation_id),
        }
        with tracer.start_as_current_span(
            f"process {tipo}",
            context=contexto_de(headers),
            kind=SpanKind.CONSUMER,
            attributes={k: v for k, v in atributos.items() if v is not None},
        ) as span:
            resultado, envelope, tentativa = self._processar(props, corpo)
            if resultado is Resultado.RETRY and envelope is not None:
                resultado = self._nova_tentativa(
                    canal, envelope, headers, corpo, tentativa + 1
                )
            span.set_attribute("pytstop.resultado", resultado.value)
            if resultado in {Resultado.RETRY, Resultado.DLQ}:
                span.set_status(StatusCode.ERROR, resultado.value)
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
                message_id=_uuid_ou_none(props.message_id),
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
        """Envelope valido de um tipo com handler, vindo de quem pode publica-lo.

        Corpo acima do teto, ilegivel (JSON invalido ou aninhado demais) ou fora
        do contrato e erro permanente: nenhuma excecao do parse sai daqui.

        Raises:
            MensagemInvalidaError: erro permanente (vai para a DLQ).
        """
        if len(corpo) > _CORPO_MAXIMO:
            msg = f"corpo acima de {_CORPO_MAXIMO} bytes"
            raise MensagemInvalidaError(msg)
        try:
            envelope: dict[str, Any] = json.loads(corpo)
            validar(envelope)
        except MensagemInvalidaError:
            raise
        except Exception as exc:  # corpo ilegivel (inclusive RecursionError): DLQ
            msg = f"corpo ilegivel ({type(exc).__name__})"
            raise MensagemInvalidaError(msg) from exc
        tipo = envelope["tipo"]
        if tipo not in self._handlers:
            msg = f"tipo sem handler neste servico: {tipo}"
            raise MensagemInvalidaError(msg)
        # Primeira entrega: o produtor do tipo no AsyncAPI. Copia de retry
        # (x-tentativa de 1 a 5): o proprio consumidor, que a republicou.
        esperado = self._usuario if tentativa else produtor(tipo)
        if user_id != esperado:
            msg = f"user_id {user_id!r} nao publica {tipo} com x-tentativa {tentativa}"
            raise MensagemInvalidaError(msg)
        return envelope

    def _executar(self, envelope: Mapping[str, Any], tentativa: int) -> Resultado:
        try:
            return self._rodar_handler(envelope)
        except _TRANSITORIOS as exc:
            return self._falha_transitoria(exc, tentativa)
        except _PERMANENTES as exc:
            # So a classe: a mensagem pode ecoar o dado recebido.
            _log.warning("command_rejected_by_domain", error=type(exc).__name__)
            return Resultado.DLQ
        except Exception as exc:  # noqa: BLE001 - nao classificado vai para a DLQ
            _log.error("command_failed", **_descricao_do_erro(exc))
            return Resultado.DLQ

    def _rodar_handler(self, envelope: Mapping[str, Any]) -> Resultado:
        with self._transacao(UUID(envelope["id"])) as aberta:
            if aberta is None:
                return Resultado.DUPLICADA
            sessao, transacao = aberta
            try:
                self._handlers[envelope["tipo"]](envelope, sessao, transacao)
            except (*_TRANSITORIOS, *_PERMANENTES):
                raise
            except DomainException as exc:
                # Fora do estado: nada do handler fica, mas o id fica gravado
                # (a reentrega e duplicada, nao roda de novo).
                sessao.rollback()
                _log.info("command_ignored", codigo=exc.codigo)
                return Resultado.IGNORADA
        return Resultado.IGNORADA if transacao.descartado else Resultado.PROCESSADA

    @contextmanager
    def _transacao(
        self, comando_id: UUID
    ) -> Iterator[tuple[Session, TransacaoDaMensagem] | None]:
        """Transacao da mensagem, comitada pelo consumidor; ``None`` se ja processada.

        O ``id`` entra em ``mensagens_processadas`` antes do handler: outra
        entrega do mesmo comando espera esta transacao e recebe ``None``. A
        sessao do handler entra por savepoint, entao o commit dela nao comita a
        mensagem: saida normal do bloco comita efeito, outbox e ``id`` juntos, e
        excecao desfaz tudo.
        """
        with self._engine.connect() as conexao, conexao.begin():
            for teto in _TETOS_DO_BANCO:
                conexao.execute(text(teto))
            if not registrar_processada(conexao, comando_id):
                yield None
                return
            with self._sessoes(
                bind=conexao, join_transaction_mode="create_savepoint"
            ) as sessao:
                yield sessao, TransacaoDaMensagem(sessao, comando_id)
                sessao.commit()

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
    ) -> Resultado:
        """Copia na fila de retry do nivel, confirmada antes do ack da original.

        Copia devolvida (sem rota) ou recusada com nack: a original vai para a
        DLQ (``Resultado.DLQ``), nunca recebe ack sem copia.

        Raises:
            AMQPError: canal fechado pelo broker (permissao ou fila de retry
                ausente) ou conexao perdida; o laco reconecta com backoff e o
                broker reentrega a original.
        """
        fila = NIVEIS_DE_RETRY[tentativa - 1]
        contexto = {
            k: v for k, v in headers.items() if k in {"traceparent", "tracestate"}
        }
        try:
            canal.basic_publish(
                exchange=EXCHANGE_RETRY,
                routing_key=fila,
                body=corpo,
                properties=propriedades(
                    envelope,
                    usuario=self._usuario,
                    headers={**contexto, "x-tentativa": tentativa},
                ),
                mandatory=True,
            )
        except (UnroutableError, NackError) as exc:
            # Sem como reagendar: a DLQ guarda a original para o redrive.
            _log.error("retry_copy_refused", fila=fila, error=type(exc).__name__)
            return Resultado.DLQ
        return Resultado.RETRY

    def _limpar_processadas_antigas(self) -> None:
        if not self._limpeza.devida():
            return
        tabela = mensagens_processadas_table
        # Relogio do banco, o mesmo do default de processada_em.
        antigas = (
            select(tabela.c.mensagem_id)
            .where(tabela.c.processada_em < func.now() - _RETENCAO_DAS_PROCESSADAS)
            .limit(LOTE_DA_LIMPEZA)
        )
        apagadas = 0
        try:
            while True:
                with self._engine.begin() as conexao:
                    deste_lote: int = conexao.execute(
                        delete(tabela).where(tabela.c.mensagem_id.in_(antigas))
                    ).rowcount
                apagadas += deste_lote
                if deste_lote < LOTE_DA_LIMPEZA:
                    break
        except SQLAlchemyError as exc:
            _log.warning("processed_messages_cleanup_failed", error=type(exc).__name__)
            return
        _log.info("processed_messages_cleanup", apagadas=apagadas)


def _uuid_ou_none(valor: object) -> str | None:
    """O valor como UUID canonico, ou None: o que vem da propriedade AMQP."""
    if not isinstance(valor, str) or len(valor) > _TAMANHO_DO_UUID:
        return None
    try:
        return str(UUID(valor))
    except ValueError:
        return None


def _descricao_do_erro(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DBAPIError):
        # Sem a mensagem do driver: o DETAIL do Postgres traz a linha inteira.
        return dict(descrever_erro_de_banco(exc))
    return {"exc_info": exc}
