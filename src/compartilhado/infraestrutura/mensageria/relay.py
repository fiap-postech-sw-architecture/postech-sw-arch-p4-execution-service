"""Relay da outbox para o RabbitMQ (ADR-036), evolucao do relay do p3.

Acorda pelo ``NOTIFY`` da outbox ou pelo poll de seguranca e reivindica um lote
com ``FOR UPDATE SKIP LOCKED``, em ordem de ``id``, sem passar a frente de uma
mensagem pendente da mesma ordem (``correlation_id``), e estende o lease das
linhas. Cada linha e entregue na propria transacao, que volta a travar a linha
(fencing: duas replicas nunca publicam a mesma linha juntas), publica com
publisher confirms e ``mandatory`` como filho do contexto OTel gravado na linha
e so entao marca ``entregue``.

Mensagem sem rota (devolvida pelo ``mandatory``), recusada (nack) ou que fecha o
canal conta tentativa, com os atrasos do relay do p3 ate ``dead``. Queda do
broker nao conta: sem conexao nao ha claim, e o lease das linhas em maos e
devolvido para a entrega seguir assim que ele voltar. Entregues ha mais de 7
dias sao apagadas pelo proprio relay.
"""

from __future__ import annotations

import contextlib
import json
import math
import select
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from opentelemetry.trace import SpanKind
from pika.exceptions import AMQPChannelError, AMQPConnectionError, AMQPError
from prometheus_client import Counter, Gauge
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.compartilhado.infraestrutura.logging import redigir_pii_erro
from src.compartilhado.infraestrutura.mensageria.amqp import (
    abrir_canal,
    fechar,
    propriedades,
    usuario_da_url,
)
from src.compartilhado.infraestrutura.mensageria.contratos import EXCHANGE_EVENTOS
from src.compartilhado.infraestrutura.mensageria.processo import Backoff
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    contexto_de,
    tracer,
)
from src.compartilhado.infraestrutura.outbox_mapping import CANAL_NOTIFY

if TYPE_CHECKING:
    import threading
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy import Connection, Engine

    from src.compartilhado.infraestrutura.mensageria.processo import SinaisDoProcesso

_log = structlog.get_logger(__name__)

# Atrasos do relay do p3 (1, 4, 16 e 64 s): a quinta falha vira ``dead``.
_ATRASOS_S: Final = (1, 4, 16, 64)
# Lote pequeno: um crash repete no maximo a linha em voo. O lease passa com
# folga a espera da confirmacao do broker (no maximo 30 s bloqueado).
_LOTE: Final = 10
_LEASE: Final = timedelta(seconds=60)
_RETENCAO_DAS_ENTREGUES: Final = timedelta(days=7)
_LIMPEZA_A_CADA_S: Final = 3600.0
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

_CLAIM = text(
    "SELECT o.id, o.tipo, o.correlation_id, o.exchange, o.routing_key, "
    "o.envelope, o.traceparent, o.tracestate, o.tentativas "
    "FROM outbox o "
    "WHERE o.status = 'pendente' AND o.proxima_tentativa_em <= now() "
    "AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.correlation_id = "
    "o.correlation_id AND p.id < o.id AND p.status = 'pendente') "
    "ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT :limite"
)
# Tempos pelo relogio do banco, o mesmo do default de proxima_tentativa_em: com
# o relogio do processo, uma linha recem-gravada podia parecer do futuro.
_ESTENDER_LEASE = text(
    "UPDATE outbox SET proxima_tentativa_em = now() + :lease WHERE id = ANY(:ids)"
)
_DEVOLVER_LEASE = text(
    "UPDATE outbox SET proxima_tentativa_em = now() "
    "WHERE id = ANY(:ids) AND status = 'pendente'"
)
_TRAVAR = text(
    "SELECT 1 FROM outbox WHERE id = :id AND status = 'pendente' FOR UPDATE SKIP LOCKED"
)
_MARCAR_ENTREGUE = text(
    "UPDATE outbox SET status = 'entregue', entregue_em = now(), "
    "ultimo_erro = NULL WHERE id = :id"
)
_REAGENDAR = text(
    "UPDATE outbox SET tentativas = :tentativas, "
    "proxima_tentativa_em = now() + :atraso, ultimo_erro = :erro WHERE id = :id"
)
_MARCAR_DEAD = text(
    "UPDATE outbox SET status = 'dead', tentativas = :tentativas, "
    "ultimo_erro = :erro WHERE id = :id"
)
_APAGAR_ENTREGUES = text(
    "DELETE FROM outbox WHERE status = 'entregue' AND entregue_em < now() - :retencao"
)
_CONTAR = text("SELECT count(*) FROM outbox WHERE status = :status")


@dataclass(frozen=True, slots=True)
class _Linha:
    id: int
    tipo: str
    correlation_id: UUID
    exchange: str
    routing_key: str
    envelope: dict[str, Any]
    traceparent: str | None
    tracestate: str | None
    tentativas: int


class _Broker:
    """Conexao e canal com confirms do relay; reabre o canal que o broker fechou."""

    def __init__(self, url: str) -> None:
        self._usuario = usuario_da_url(url)
        self._conexao, self._canal = abrir_canal(url, exchanges=(EXCHANGE_EVENTOS,))

    def publicar(self, linha: _Linha) -> None:
        """Publica com ``mandatory`` e espera a confirmacao do broker.

        Raises:
            AMQPChannelError: sem rota, nack ou recusa (403, 406) da mensagem.
            AMQPConnectionError: o broker caiu.
        """
        try:
            self._canal.basic_publish(
                exchange=linha.exchange,
                routing_key=linha.routing_key,
                body=json.dumps(linha.envelope).encode(),
                properties=propriedades(
                    linha.envelope, usuario=self._usuario, headers=contexto_atual()
                ),
                mandatory=True,
            )
        except AMQPChannelError:
            # Recusa do broker fecha o canal; sem rota ou nack, nao.
            if not self._canal.is_open:
                self._canal = self._conexao.channel()
                self._canal.confirm_delivery()
            raise

    def manter_viva(self) -> None:
        """Heartbeat do AMQP; levanta se o broker caiu enquanto o relay esperava."""
        self._conexao.process_data_events(time_limit=0)

    def fechar(self) -> None:
        fechar(self._conexao)


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
        self._url = rabbitmq_url
        self._sinais = sinais
        self._poll_s = poll_s
        self._backoff = backoff or Backoff()
        # Erros do driver na conexao do LISTEN (psycopg2), sem importa-lo aqui.
        self._erro_do_driver: type[Exception] = engine.dialect.loaded_dbapi.Error
        self._broker: _Broker | None = None
        self._ouvinte: Any = None
        self._proxima_limpeza = time.monotonic()
        _PENDENTES.set_function(lambda: self._contar("pendente"))
        _DEAD.set_function(lambda: self._contar("dead"))

    def executar(self, parar: threading.Event) -> None:
        """Laco principal: conecta, drena, limpa e espera o NOTIFY ou o poll."""
        try:
            while not parar.is_set():
                self._sinais.heartbeat()
                try:
                    self._ciclo(parar)
                    self._backoff.reiniciar()
                except AMQPError as exc:
                    self._fora_do_ar("broker", exc)
                    self._fechar_broker()
                    self._backoff.esperar(parar)
                except (SQLAlchemyError, OSError, self._erro_do_driver) as exc:
                    self._fora_do_ar("database", exc)
                    self._fechar_ouvinte()
                    self._backoff.esperar(parar)
        finally:
            self._fechar_broker()
            self._fechar_ouvinte()
            self._sinais.indisponivel()

    def _ciclo(self, parar: threading.Event) -> None:
        if self._broker is None:
            self._broker = _Broker(self._url)
            _log.info("relay_broker_connected")
        if self._ouvinte is None:
            self._ouvinte = self._ouvir()
        self._sinais.pronto()
        self._drenar(self._broker, parar)
        self._limpar_entregues_antigas()
        prontos, _, _ = select.select([self._ouvinte], [], [], self._poll_s)
        if prontos:
            self._ouvinte.poll()
            self._ouvinte.notifies.clear()
        self._broker.manter_viva()

    def _ouvir(self) -> Any:  # noqa: ANN401 - conexao crua do driver (psycopg2)
        """Conexao dedicada (fora do pool) em autocommit com ``LISTEN`` na outbox."""
        argumentos, parametros = self._engine.dialect.create_connect_args(
            self._engine.url
        )
        conexao = self._engine.dialect.loaded_dbapi.connect(
            *argumentos, **{**parametros, **_KEEPALIVES}
        )
        conexao.autocommit = True
        with conexao.cursor() as cursor:
            cursor.execute(f"LISTEN {CANAL_NOTIFY}")
        return conexao

    def _fora_do_ar(self, dependencia: str, exc: Exception) -> None:
        self._sinais.indisponivel()
        _log.warning(
            "relay_dependency_unavailable",
            dependencia=dependencia,
            error=type(exc).__name__,
        )

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
        while not parar.is_set():
            self._sinais.heartbeat()
            linhas = self._reivindicar()
            if not linhas:
                return
            self._entregar_lote(broker, linhas)

    def _reivindicar(self) -> list[_Linha]:
        with self._engine.begin() as conexao:
            resultado = conexao.execute(_CLAIM, {"limite": _LOTE})
            linhas = [_Linha(**linha._mapping) for linha in resultado]
            if linhas:
                conexao.execute(
                    _ESTENDER_LEASE,
                    {"lease": _LEASE, "ids": [linha.id for linha in linhas]},
                )
        return linhas

    def _entregar_lote(self, broker: _Broker, linhas: Sequence[_Linha]) -> None:
        for indice, linha in enumerate(linhas):
            try:
                self._entregar(broker, linha)
            except AMQPConnectionError:
                # Queda do broker nao gasta tentativa: as linhas que sobraram
                # voltam a valer ja, sem esperar o lease.
                self._devolver(linhas[indice:])
                raise

    def _devolver(self, linhas: Sequence[_Linha]) -> None:
        with contextlib.suppress(SQLAlchemyError), self._engine.begin() as conexao:
            conexao.execute(
                _DEVOLVER_LEASE,
                {"ids": [linha.id for linha in linhas]},
            )

    def _entregar(self, broker: _Broker, linha: _Linha) -> None:
        with (
            self._engine.begin() as conexao,
            structlog.contextvars.bound_contextvars(
                correlation_id=str(linha.correlation_id), outbox_id=linha.id
            ),
        ):
            if conexao.execute(_TRAVAR, {"id": linha.id}).first() is None:
                return  # outra replica esta com a linha, ou ela ja saiu
            try:
                self._publicar(broker, linha)
            except AMQPChannelError as exc:
                self._registrar_falha(conexao, linha, exc)
                return
            conexao.execute(_MARCAR_ENTREGUE, {"id": linha.id})
            _PUBLICADAS.labels(linha.tipo).inc()
            _log.info("message_published", tipo=linha.tipo)

    @staticmethod
    def _publicar(broker: _Broker, linha: _Linha) -> None:
        """Span PRODUCER filho do contexto gravado na outbox; ele vai nos headers.

        Falha fica no span (status e evento de excecao): as mensagens das
        excecoes do pika trazem classe e codigo do broker, nunca o corpo.
        """
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
        ):
            broker.publicar(linha)

    def _registrar_falha(
        self, conexao: Connection, linha: _Linha, falha: AMQPChannelError
    ) -> None:
        tentativas = linha.tentativas + 1
        # repr: classe e codigo do broker (ex.: "(406) 'PRECONDITION_FAILED ...'").
        erro = redigir_pii_erro(repr(falha))
        if tentativas > len(_ATRASOS_S):
            conexao.execute(
                _MARCAR_DEAD, {"id": linha.id, "tentativas": tentativas, "erro": erro}
            )
            _log.error(
                "message_dead",
                tipo=linha.tipo,
                tentativas=tentativas,
                error=type(falha).__name__,
            )
            return
        conexao.execute(
            _REAGENDAR,
            {
                "id": linha.id,
                "tentativas": tentativas,
                "atraso": timedelta(seconds=_ATRASOS_S[tentativas - 1]),
                "erro": erro,
            },
        )
        _log.warning(
            "message_publish_failed",
            tipo=linha.tipo,
            tentativas=tentativas,
            error=type(falha).__name__,
        )

    def _limpar_entregues_antigas(self) -> None:
        agora = time.monotonic()
        if agora < self._proxima_limpeza:
            return
        with self._engine.begin() as conexao:
            apagadas = conexao.execute(
                _APAGAR_ENTREGUES, {"retencao": _RETENCAO_DAS_ENTREGUES}
            ).rowcount
        self._proxima_limpeza = agora + _LIMPEZA_A_CADA_S
        _log.info("outbox_cleanup", apagadas=apagadas)

    def _contar(self, status: str) -> float:
        # Lido a cada scrape; banco fora vira NaN, nao derruba o /metrics.
        try:
            with self._engine.connect() as conexao:
                return float(conexao.execute(_CONTAR, {"status": status}).scalar_one())
        except SQLAlchemyError:
            return math.nan
