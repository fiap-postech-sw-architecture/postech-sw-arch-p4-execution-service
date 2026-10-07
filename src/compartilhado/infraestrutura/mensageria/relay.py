"""Relay da outbox para o RabbitMQ (ADR-036), evolucao do relay do p3.

Acorda pelo ``NOTIFY`` da outbox ou pelo poll de seguranca e drena a outbox em
lotes de transacoes curtas, sem transacao aberta durante o publish
(``outbox.py``): claim com lease, renovacao do lease antes de publicar e o
desfecho gravado so se a linha ainda for desta replica (fencing). Publica com
publisher confirms e ``mandatory``, como filho do contexto OTel gravado na
linha, e so entao marca ``entregue``.

Mensagem sem rota (devolvida pelo ``mandatory``), recusada (nack) ou que fecha o
canal conta tentativa, com os atrasos do relay do p3 ate ``dead``; envelope fora
do contrato vira ``dead`` direto, e qualquer outra falha no caminho da linha
conta tentativa sem derrubar o relay. Queda do broker nao conta: sem conexao nao
ha claim, e as linhas em maos voltam a valer ja. Com a conexao bloqueada pelo
broker (alarme de memoria ou disco) o relay para de reivindicar ate o
desbloqueio, e o timeout do bloqueio derruba a conexao como uma queda. Uma vez
por hora apaga, em lotes, as entregues ha mais de 7 dias.
"""

from __future__ import annotations

import contextlib
import json
import select
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from opentelemetry.trace import SpanKind, StatusCode
from pika.exceptions import (
    AMQPError,
    ChannelClosedByBroker,
    NackError,
    UnroutableError,
)
from prometheus_client import Counter, Gauge
from sqlalchemy.exc import SQLAlchemyError

from src.compartilhado.infraestrutura.mensageria.amqp import (
    abrir_canal,
    fechar,
    propriedades,
    usuario_da_url,
)
from src.compartilhado.infraestrutura.mensageria.contratos import (
    EXCHANGE_EVENTOS,
    MensagemInvalidaError,
    validar,
)
from src.compartilhado.infraestrutura.mensageria.outbox import LinhaDaOutbox, Outbox
from src.compartilhado.infraestrutura.mensageria.processo import Backoff, Periodico
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    contexto_de,
    tracer,
)
from src.compartilhado.infraestrutura.outbox_mapping import CANAL_NOTIFY

if TYPE_CHECKING:
    import threading
    from collections.abc import Sequence

    from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection
    from sqlalchemy import Engine

    from src.compartilhado.infraestrutura.mensageria.processo import SinaisDoProcesso

_log = structlog.get_logger(__name__)

# Lote pequeno: um crash repete no maximo as linhas em voo.
_LOTE: Final = 10
# Lease maior que o pior caso de um publish (30 s bloqueado pelo broker): outra
# replica nao reivindica a linha enquanto esta publica.
_LEASE: Final = timedelta(seconds=60)
# Conexao dedicada do LISTEN: um socket morto em silencio (failover, NAT) e
# detectado em cerca de 1 min, em vez de deixar o relay surdo ao NOTIFY.
_KEEPALIVES: Final = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "connect_timeout": 3,
}

_PUBLICADAS = Counter(
    "pytstop_mensagens_publicadas_total",
    "Mensagens da outbox publicadas com confirmacao do broker.",
    ["tipo"],
)
_PENDENTES = Gauge("outbox_pendentes", "Mensagens da outbox esperando publicacao.")
_DEAD = Gauge("outbox_dead", "Mensagens da outbox que esgotaram as tentativas.")


class BrokerIndisponivelError(Exception):
    """A conexao com o broker caiu ou ficou bloqueada (nao e falha da linha)."""


class _Broker:
    """Conexao e canal com confirms do relay.

    Reabre o canal que o broker fechou e acompanha o ``Connection.Blocked``
    (alarme de memoria ou disco): bloqueada, o relay nao reivindica linhas.
    """

    def __init__(self, url: str) -> None:
        self._usuario = usuario_da_url(url)
        self._conexao: BlockingConnection
        self._canal: BlockingChannel
        self._conexao, self._canal = abrir_canal(url, exchanges=(EXCHANGE_EVENTOS,))
        self.bloqueada = False
        self._conexao.add_on_connection_blocked_callback(self._bloquear)
        self._conexao.add_on_connection_unblocked_callback(self._desbloquear)

    def publicar(self, linha: LinhaDaOutbox) -> str | None:
        """Publica com ``mandatory`` e espera o confirm; devolve a falha da mensagem.

        ``None`` com a mensagem confirmada; texto fixo (classe e codigo do
        broker) se ela voltou sem rota, levou nack ou fechou o canal: a excecao
        do pika carrega a mensagem devolvida, com o texto livre do envelope.

        Raises:
            BrokerIndisponivelError: conexao perdida, bloqueada alem do limite
                ou canal que nao reabre (nao conta tentativa).
        """
        corpo = json.dumps(linha.envelope).encode()
        props = propriedades(
            linha.envelope, usuario=self._usuario, headers=contexto_atual()
        )
        try:
            self._canal.basic_publish(
                exchange=linha.exchange,
                routing_key=linha.routing_key,
                body=corpo,
                properties=props,
                mandatory=True,
            )
        except (UnroutableError, NackError, ChannelClosedByBroker) as falha:
            # Sem rota e nack mantem o canal; a recusa (403, 404, 406) o fecha.
            self._reabrir_canal()
            codigo = getattr(falha, "reply_code", None)
            return type(falha).__name__ + (f" ({codigo})" if codigo else "")
        except (AMQPError, OSError) as exc:
            raise BrokerIndisponivelError from exc
        return None

    def _reabrir_canal(self) -> None:
        if self._canal.is_open:
            return
        try:
            self._canal = self._conexao.channel()
            self._canal.confirm_delivery()
        except (AMQPError, OSError) as exc:
            raise BrokerIndisponivelError from exc

    def manter_viva(self) -> None:
        """Heartbeat do AMQP e eventos do broker (bloqueio, queda) no laco ocioso.

        Raises:
            BrokerIndisponivelError: o broker caiu ou o bloqueio passou do limite.
        """
        try:
            self._conexao.process_data_events(time_limit=0)
        except (AMQPError, OSError, ValueError) as exc:
            # ValueError: o pika usado depois de fechar a conexao por conta
            # propria (timeout do bloqueio).
            raise BrokerIndisponivelError from exc

    def fechar(self) -> None:
        fechar(self._conexao)

    def _bloquear(self, _conexao: object, _metodo: object) -> None:
        self.bloqueada = True
        _log.warning("relay_broker_blocked")

    def _desbloquear(self, _conexao: object, _metodo: object) -> None:
        self.bloqueada = False
        _log.info("relay_broker_unblocked")


class Relay:
    """Publica a outbox no RabbitMQ; ``executar`` roda ate ``parar`` ser ligado."""

    def __init__(
        self,
        engine: Engine,
        rabbitmq_url: str,
        sinais: SinaisDoProcesso,
        *,
        poll_s: float = 5.0,
        backoff: Backoff | None = None,
    ) -> None:
        self._engine = engine
        self._outbox = Outbox(engine)
        self._url = rabbitmq_url
        self._sinais = sinais
        self._poll_s = poll_s
        self._backoff = backoff or Backoff()
        # Erros do driver na conexao do LISTEN (psycopg2), sem importa-lo aqui.
        self._erro_do_driver: type[Exception] = engine.dialect.loaded_dbapi.Error
        self._broker: _Broker | None = None
        # Conexao crua do psycopg2 (sem tipos), dedicada ao LISTEN.
        self._ouvinte: Any = None
        self._limpeza = Periodico()
        _PENDENTES.set_function(lambda: self._outbox.contar("pendente"))
        _DEAD.set_function(lambda: self._outbox.contar("dead"))

    def executar(self, parar: threading.Event) -> None:
        """Laco principal: conecta, drena, limpa e espera o NOTIFY ou o poll."""
        try:
            while not parar.is_set():
                self._sinais.heartbeat()
                try:
                    self._ciclo(parar)
                except BrokerIndisponivelError as exc:
                    self._fora_do_ar("broker", exc)
                    self._backoff.esperar(parar)
                except (SQLAlchemyError, OSError, self._erro_do_driver) as exc:
                    self._fora_do_ar("database", exc)
                    self._backoff.esperar(parar)
                else:
                    self._backoff.reiniciar()
        finally:
            self._fechar_broker()
            self._fechar_ouvinte()
            self._sinais.indisponivel()

    def _ciclo(self, parar: threading.Event) -> None:
        broker = self._conectar()
        if self._ouvinte is None:
            self._ouvinte = self._ouvir()
        if broker.bloqueada:
            # Alarme no broker: nada de reivindicar ate o Connection.Unblocked,
            # que o manter_viva recebe enquanto o laco espera.
            self._sinais.indisponivel()
        else:
            self._sinais.pronto()
            self._drenar(broker, parar)
            if self._limpeza.devida():
                self._limpar_entregues_antigas()
        prontos, _, _ = select.select([self._ouvinte], [], [], self._poll_s)
        if prontos:
            self._ouvinte.poll()
            self._ouvinte.notifies.clear()
        broker.manter_viva()

    def _conectar(self) -> _Broker:
        if self._broker is None:
            try:
                self._broker = _Broker(self._url)
            except (AMQPError, OSError) as exc:  # fora, credencial ou topologia
                raise BrokerIndisponivelError from exc
            _log.info("relay_broker_connected")
        return self._broker

    def _ouvir(self) -> Any:  # noqa: ANN401 - conexao crua do driver (psycopg2)
        """Conexao dedicada (fora do pool) em autocommit com ``LISTEN`` na outbox."""
        argumentos, parametros = self._engine.dialect.create_connect_args(
            self._engine.url
        )
        conexao = self._engine.dialect.loaded_dbapi.connect(
            *argumentos, **{**parametros, **_KEEPALIVES}
        )
        try:
            conexao.autocommit = True
            with conexao.cursor() as cursor:
                cursor.execute(f"LISTEN {CANAL_NOTIFY}")
        except BaseException:
            conexao.close()
            raise
        return conexao

    def _fora_do_ar(self, dependencia: str, exc: Exception) -> None:
        self._sinais.indisponivel()
        _log.warning(
            "relay_dependency_unavailable",
            dependencia=dependencia,
            error=type(exc.__cause__ or exc).__name__,
        )
        # As duas conexoes recomecam do zero na volta seguinte.
        self._fechar_broker()
        self._fechar_ouvinte()

    def _fechar_broker(self) -> None:
        if self._broker is not None:
            self._broker.fechar()
            self._broker = None

    def _fechar_ouvinte(self) -> None:
        if self._ouvinte is not None:
            with contextlib.suppress(self._erro_do_driver):
                self._ouvinte.close()
            self._ouvinte = None

    def _drenar(self, broker: _Broker, parar: threading.Event) -> None:
        """Reivindica e entrega lotes ate nao sobrar linha elegivel."""
        while not parar.is_set():
            self._sinais.heartbeat()
            linhas = self._outbox.reivindicar(_LOTE, _LEASE)
            if not linhas:
                return
            for indice, linha in enumerate(linhas):
                if broker.bloqueada:
                    self._liberar(linhas[indice:])
                    return
                try:
                    self._entregar(broker, linha)
                except BrokerIndisponivelError:
                    # Queda do broker nao gasta tentativa: as linhas que sobraram
                    # voltam a valer ja, sem esperar o lease.
                    self._liberar(linhas[indice:])
                    raise

    def _entregar(self, broker: _Broker, reivindicada: LinhaDaOutbox) -> None:
        with structlog.contextvars.bound_contextvars(
            correlation_id=str(reivindicada.correlation_id),
            outbox_id=reivindicada.id,
        ):
            linha = self._outbox.renovar(reivindicada, _LEASE)
            if linha is None:
                # O lease venceu e outra replica pegou a linha, ou ja a entregou.
                _log.info("outbox_row_taken_by_another_replica")
                return
            try:
                validar(linha.envelope)
            except MensagemInvalidaError:
                # Nenhuma nova tentativa conserta o envelope: dead direto.
                if self._outbox.marcar_dead(linha, "envelope fora do contrato"):
                    _log.error("message_dead", tipo=linha.tipo, motivo="contrato")
                return
            try:
                falha = self._publicar(broker, linha)
            except BrokerIndisponivelError:
                # O lease renovado e o desta linha: o _drenar libera as outras.
                self._liberar([linha])
                raise
            except Exception as exc:  # noqa: BLE001 - a linha falha, o relay segue
                falha = f"falha ao publicar ({type(exc).__name__})"
            self._registrar_desfecho(linha, falha)

    @staticmethod
    def _publicar(broker: _Broker, linha: LinhaDaOutbox) -> str | None:
        """Span PRODUCER filho do contexto gravado na outbox; ele vai nos headers."""
        pai = contexto_de(
            {"traceparent": linha.traceparent, "tracestate": linha.tracestate}
        )
        with tracer.start_as_current_span(
            f"publish {linha.tipo}",
            context=pai,
            kind=SpanKind.PRODUCER,
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.destination.name": linha.exchange,
                "messaging.rabbitmq.destination.routing_key": linha.routing_key,
                "messaging.message.id": linha.envelope["id"],
                "messaging.message.conversation_id": str(linha.correlation_id),
            },
        ) as span:
            falha = broker.publicar(linha)
            if falha is not None:
                span.set_status(StatusCode.ERROR, falha)
        return falha

    def _registrar_desfecho(self, linha: LinhaDaOutbox, falha: str | None) -> None:
        """Marca a linha: entregue, nova tentativa com atraso ou ``dead``."""
        if falha is None:
            _PUBLICADAS.labels(linha.tipo).inc()
            if self._outbox.marcar_entregue(linha):
                _log.info("message_published", tipo=linha.tipo)
            else:
                # O publish passou do lease e outra replica pegou a linha: a
                # mensagem pode sair de novo, e o consumidor descarta pelo id.
                _log.warning("message_published_after_losing_the_lease")
            return
        desfecho = self._outbox.registrar_falha(linha, falha)
        tentativas = linha.tentativas + 1
        if desfecho == "perdida":
            _log.warning("message_publish_failed_after_losing_the_lease")
        elif desfecho == "dead":
            _log.error(
                "message_dead", tipo=linha.tipo, tentativas=tentativas, error=falha
            )
        else:
            _log.warning(
                "message_publish_failed",
                tipo=linha.tipo,
                tentativas=tentativas,
                error=falha,
            )

    def _liberar(self, linhas: Sequence[LinhaDaOutbox]) -> None:
        try:
            self._outbox.liberar(linhas)
        except SQLAlchemyError as exc:
            # As linhas voltam quando o lease vencer.
            _log.warning("outbox_lease_return_failed", error=type(exc).__name__)

    def _limpar_entregues_antigas(self) -> None:
        try:
            apagadas = self._outbox.limpar()
        except SQLAlchemyError as exc:
            # A janela ja avancou: a proxima tentativa e daqui a uma hora, nao a
            # cada volta do laco, e o relay segue publicando.
            _log.warning("outbox_cleanup_failed", error=type(exc).__name__)
            return
        _log.info("outbox_cleanup", apagadas=apagadas)
