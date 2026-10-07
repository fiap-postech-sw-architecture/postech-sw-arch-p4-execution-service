"""Relay sem broker nem banco: politica de tentativas, janela da limpeza e o _Broker.

O caminho com PostgreSQL e RabbitMQ esta em ``tests/integracao/test_relay.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self
from uuid import uuid4

import pytest
from pika.exceptions import (
    ChannelClosedByBroker,
    ConnectionBlockedTimeout,
    NackError,
    StreamLostError,
    UnroutableError,
)

from src.compartilhado.infraestrutura.database import criar_engine
from src.compartilhado.infraestrutura.mensageria import relay as modulo_relay
from src.compartilhado.infraestrutura.mensageria.outbox import (
    ATRASOS_S,
    LinhaDaOutbox,
    atraso_depois_da_falha,
)
from src.compartilhado.infraestrutura.mensageria.processo import (
    Periodico,
    SinaisDoProcesso,
)
from src.compartilhado.infraestrutura.mensageria.relay import BrokerIndisponivelError

_URL = "amqp://execucao:x@broker.test:5672/%2F"  # gitleaks:allow - nunca conecta


def _sinais() -> SinaisDoProcesso:
    return SinaisDoProcesso.do_processo("teste-relay")


@pytest.mark.parametrize(
    ("falha", "atraso"),
    [
        pytest.param(1, 1, id="primeira-1s"),
        pytest.param(2, 4, id="segunda-4s"),
        pytest.param(3, 16, id="terceira-16s"),
        pytest.param(4, 64, id="quarta-64s"),
        pytest.param(5, None, id="quinta-dead"),
    ],
)
def test_atraso_de_cada_falha_e_o_do_relay_do_p3(
    falha: int, atraso: int | None
) -> None:
    assert ATRASOS_S == (1, 4, 16, 64)
    assert atraso_depois_da_falha(falha) == atraso


def _linha() -> LinhaDaOutbox:
    ordem = uuid4()
    return LinhaDaOutbox(
        id=1,
        tipo="DiagnosticoConcluido",
        correlation_id=ordem,
        exchange="pytstop.eventos",
        routing_key="evento.execucao.diagnostico_concluido",
        envelope={
            "id": str(uuid4()),
            "tipo": "DiagnosticoConcluido",
            "correlation_id": str(ordem),
            "dados": {"observacoes": "Joao da Silva"},
        },
        traceparent=None,
        tracestate=None,
        tentativas=0,
        lease_ate=datetime.now(UTC),
    )


def test_repr_da_linha_nao_mostra_o_envelope() -> None:
    # Texto livre do envelope num traceback ou log.
    assert "Joao" not in repr(_linha())


def test_janela_da_limpeza_avanca_na_chamada() -> None:
    janela = Periodico(intervalo_s=3600)
    assert janela.devida()
    # Mesmo com a limpeza falhando, a proxima e na janela seguinte.
    assert not janela.devida()
    assert Periodico(intervalo_s=0).devida()


class _Canal:
    def __init__(self, erro: Exception | None = None) -> None:
        self.erro = erro
        self.is_open = True
        self.confirmado = False
        self.publicadas: list[dict[str, Any]] = []

    def basic_publish(self, **kwargs: Any) -> None:
        if self.erro is not None:
            if isinstance(self.erro, ChannelClosedByBroker):
                self.is_open = False
            raise self.erro
        self.publicadas.append(kwargs)

    def confirm_delivery(self) -> None:
        self.confirmado = True


class _Conexao:
    def __init__(self) -> None:
        self.ao_bloquear: Any = None
        self.ao_desbloquear: Any = None
        self.canais: list[_Canal] = []
        self.erro_nos_eventos: Exception | None = None

    def add_on_connection_blocked_callback(self, callback: Any) -> None:
        self.ao_bloquear = callback

    def add_on_connection_unblocked_callback(self, callback: Any) -> None:
        self.ao_desbloquear = callback

    def channel(self) -> _Canal:
        canal = _Canal()
        self.canais.append(canal)
        return canal

    def process_data_events(self, time_limit: float) -> None:
        if self.erro_nos_eventos is not None:
            raise self.erro_nos_eventos


def _broker(
    monkeypatch: pytest.MonkeyPatch, canal: _Canal
) -> tuple[modulo_relay._Broker, _Conexao]:
    conexao = _Conexao()
    monkeypatch.setattr(modulo_relay, "abrir_canal", lambda *_a, **_k: (conexao, canal))
    return modulo_relay._Broker(_URL), conexao


def test_bloqueio_e_desbloqueio_do_broker_marcam_a_conexao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, conexao = _broker(monkeypatch, _Canal())
    assert not broker.bloqueada
    conexao.ao_bloquear(conexao, object())  # Connection.Blocked
    assert broker.bloqueada
    conexao.ao_desbloquear(conexao, object())  # Connection.Unblocked
    assert not broker.bloqueada


def test_publicacao_confirmada_vai_com_mandatory_e_o_usuario_do_servico(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canal = _Canal()
    broker, _ = _broker(monkeypatch, canal)
    linha = _linha()
    assert broker.publicar(linha) is None
    (publicada,) = canal.publicadas
    assert publicada["mandatory"] is True
    assert publicada["properties"].user_id == "execucao"
    assert publicada["properties"].message_id == linha.envelope["id"]


@pytest.mark.parametrize(
    ("erro", "texto", "reaberto"),
    [
        pytest.param(UnroutableError([]), "UnroutableError", False, id="sem-rota"),
        pytest.param(NackError([]), "NackError", False, id="nack"),
        pytest.param(
            ChannelClosedByBroker(403, "ACCESS_REFUSED"),
            "ChannelClosedByBroker (403)",
            True,
            id="recusa-fecha-o-canal",
        ),
    ],
)
def test_falha_da_mensagem_volta_em_texto_fixo(
    monkeypatch: pytest.MonkeyPatch, erro: Exception, texto: str, reaberto: bool
) -> None:
    broker, conexao = _broker(monkeypatch, _Canal(erro))
    assert broker.publicar(_linha()) == texto
    assert (len(conexao.canais) == 1) is reaberto
    assert all(canal.confirmado for canal in conexao.canais)


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(StreamLostError("caiu"), id="conexao-perdida"),
        pytest.param(ConnectionBlockedTimeout("30 s"), id="bloqueio-passou-do-limite"),
        pytest.param(OSError("socket"), id="socket"),
    ],
)
def test_queda_do_broker_na_publicacao_nao_e_falha_da_linha(
    monkeypatch: pytest.MonkeyPatch, erro: Exception
) -> None:
    broker, _ = _broker(monkeypatch, _Canal(erro))
    with pytest.raises(BrokerIndisponivelError):
        broker.publicar(_linha())


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(StreamLostError("caiu"), id="queda-no-laco-ocioso"),
        # O pika usado depois de fechar a conexao sozinho (timeout do bloqueio).
        pytest.param(ValueError("Timeout closed before call"), id="conexao-fechada"),
    ],
)
def test_queda_no_laco_ocioso_e_broker_indisponivel(
    monkeypatch: pytest.MonkeyPatch, erro: Exception
) -> None:
    broker, conexao = _broker(monkeypatch, _Canal())
    conexao.erro_nos_eventos = erro
    with pytest.raises(BrokerIndisponivelError):
        broker.manter_viva()


def test_ouvinte_do_notify_usa_os_tetos_de_conexao_do_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # connect_timeout e socket sem resposta derrubado tambem na conexao do
    # LISTEN, fora do pool: sem eles o relay ficaria surdo ao NOTIFY.
    engine = criar_engine("postgresql://x:y@127.0.0.1:1/nada")  # gitleaks:allow
    argumentos: dict[str, Any] = {}

    class _Cursor:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def execute(self, _sql: str) -> None:
            pass

    class _ConexaoDoDriver:
        autocommit = False

        def cursor(self) -> _Cursor:
            return _Cursor()

    def conectar(*_args: object, **kwargs: Any) -> _ConexaoDoDriver:
        argumentos.update(kwargs)
        return _ConexaoDoDriver()

    monkeypatch.setattr(engine.dialect.loaded_dbapi, "connect", conectar)
    relay = modulo_relay.Relay(engine, _URL, _sinais())
    relay._ouvir()
    assert argumentos["connect_timeout"] == 3
    assert argumentos["tcp_user_timeout"] == 10_000
    assert argumentos["keepalives"] == 1


def test_ouvinte_que_falha_no_listen_fecha_a_conexao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = criar_engine("postgresql://x:y@127.0.0.1:1/nada")  # gitleaks:allow
    fechadas: list[bool] = []

    class _ConexaoDoDriver:
        autocommit = False

        def cursor(self) -> Any:
            raise OSError("LISTEN recusado")

        def close(self) -> None:
            fechadas.append(True)

    monkeypatch.setattr(
        engine.dialect.loaded_dbapi, "connect", lambda *_a, **_k: _ConexaoDoDriver()
    )
    relay = modulo_relay.Relay(engine, _URL, _sinais())
    with pytest.raises(OSError, match="LISTEN"):
        relay._ouvir()
    assert fechadas == [True]
