"""Relay sem broker nem banco: politica de tentativas, janela da limpeza e o _Broker.

O caminho com PostgreSQL e RabbitMQ esta em ``tests/integracao/test_relay.py``.
"""

from __future__ import annotations

import ast
import errno
import io
import json
import os
import socket
import ssl
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

import pytest
from pika.adapters.utils.connection_workflow import AMQPConnectorStackTimeout
from pika.exceptions import (
    AMQPConnectionError,
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
    Backoff,
    Periodico,
    SinaisDoProcesso,
)
from src.compartilhado.infraestrutura.mensageria.relay import BrokerIndisponivelError

_URL = "amqp://execucao:x@broker.test:5672/%2F"  # gitleaks:allow - nunca conecta
_MENSAGERIA = Path(modulo_relay.__file__).parent


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
        self.is_open = True
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

    def close(self) -> None:
        self.is_open = False


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


@pytest.mark.parametrize(
    "modulo",
    [
        pytest.param("relay", id="relay"),
        pytest.param("outbox", id="outbox"),
        pytest.param("consumidor", id="consumidor"),
    ],
)
def test_vencimento_e_retencao_usam_o_relogio_do_banco(modulo: str) -> None:
    # Com o relogio do processo, uma linha recem-gravada (default now() do
    # banco) parecia do futuro para o claim: vencimento, lease e retencao sao
    # sempre now() do banco; o processo so usa o monotonico para as janelas.
    fonte = (_MENSAGERIA / f"{modulo}.py").read_text()
    chamadas = {
        f"{no.func.value.id}.{no.func.attr}"
        for no in ast.walk(ast.parse(fonte))
        if isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute)
        and isinstance(no.func.value, ast.Name)
    }
    assert chamadas.isdisjoint({"datetime.now", "datetime.utcnow", "time.time"})


def test_canal_que_nao_reabre_e_broker_indisponivel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, conexao = _broker(
        monkeypatch, _Canal(ChannelClosedByBroker(404, "NOT_FOUND"))
    )

    def sem_canal() -> _Canal:
        raise StreamLostError("caiu junto")

    monkeypatch.setattr(conexao, "channel", sem_canal)
    with pytest.raises(BrokerIndisponivelError):
        broker.publicar(_linha())


# --- abertura da conexao no laco do relay -----------------------------------

_BANCO_MORTO = "postgresql://x:y@127.0.0.1:1/nada"  # gitleaks:allow - nunca conecta


class _EsperaQueAnota(Backoff):
    """Anota a idade do heartbeat na espera e para o laco na primeira."""

    def __init__(self, heartbeat: Path) -> None:
        super().__init__(0.0, 0.0)
        self._heartbeat = heartbeat
        self.idades: list[float] = []

    def esperar(self, parar: threading.Event) -> None:
        self.idades.append(time.time() - self._heartbeat.stat().st_mtime)
        parar.set()


class _Ouvinte:
    """Conexao do LISTEN que nunca e usada: o teste para antes do select."""

    def close(self) -> None:
        pass


def _rodar_relay(
    tmp_path: Path, abrir: Any, *, pronto: str = "pronto"
) -> _EsperaQueAnota:
    backoff = _EsperaQueAnota(tmp_path / "hb")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(modulo_relay, "abrir_canal", abrir)
        patch.setattr(modulo_relay.Relay, "_ouvir", lambda _self: _Ouvinte())
        modulo_relay.Relay(
            criar_engine(_BANCO_MORTO),
            _URL,
            SinaisDoProcesso(tmp_path / "hb", tmp_path / pronto),
            backoff=backoff,
        ).executar(threading.Event())
    return backoff


def _falha(erro: Exception) -> Any:
    def abrir(*_: object, **__: object) -> Any:
        raise erro

    return abrir


@pytest.mark.parametrize(
    ("erro", "nome"),
    [
        (AMQPConnectionError("broker fora"), "AMQPConnectionError"),
        (socket.gaierror(socket.EAI_NONAME, "Name or service not known"), "gaierror"),
        (AMQPConnectorStackTimeout("15 s"), "AMQPConnectorStackTimeout"),
    ],
    ids=["broker-fora", "sem-dns", "broker-mudo"],
)
def test_broker_fora_na_abertura_reconecta_com_backoff_fora_da_prontidao(
    tmp_path: Path, log_capturado: io.StringIO, erro: Exception, nome: str
) -> None:
    backoff = _rodar_relay(tmp_path, _falha(erro))
    assert len(backoff.idades) == 1
    assert not (tmp_path / "pronto").exists()
    [aviso] = [
        json.loads(linha)
        for linha in log_capturado.getvalue().splitlines()
        if "relay_dependency_unavailable" in linha
    ]
    assert (aviso["dependencia"], aviso["error"]) == ("broker", nome)


def test_abertura_lenta_que_falha_toca_o_heartbeat_antes_da_espera(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "hb"

    def resolucao_lenta(*_: object, **__: object) -> Any:
        # O pika nao poe prazo na resolucao do nome: com o DNS mudo a tentativa
        # leva dezenas de segundos, e o heartbeat, tocado antes dela, envelhece.
        antigo = time.time() - 120
        os.utime(heartbeat, (antigo, antigo))
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    [idade] = _rodar_relay(tmp_path, resolucao_lenta).idades
    assert idade < 5


@pytest.mark.parametrize(
    ("erro", "mensagem"),
    [
        pytest.param(
            OSError(errno.EMFILE, "Too many open files"),
            "Too many open files",
            id="sem-descritores",
        ),
        pytest.param(
            ssl.SSLCertVerificationError(1, "certificate verify failed"),
            "certificate verify failed",
            id="tls",
        ),
    ],
)
def test_oserror_na_abertura_que_nao_e_broker_fora_derruba_o_relay(
    tmp_path: Path, erro: OSError, mensagem: str
) -> None:
    # Nem broker fora nem banco fora: reconectar em laco esconderia o defeito.
    with pytest.raises(OSError, match=mensagem):
        _rodar_relay(tmp_path, _falha(erro))


def test_disco_no_arquivo_de_prontidao_derruba_o_relay_em_vez_de_virar_banco_fora(
    tmp_path: Path,
) -> None:
    def conecta(*_: object, **__: object) -> tuple[_Conexao, _Canal]:
        return _Conexao(), _Canal()

    with pytest.raises(FileNotFoundError):
        _rodar_relay(tmp_path, conecta, pronto="sem-diretorio/pronto")
