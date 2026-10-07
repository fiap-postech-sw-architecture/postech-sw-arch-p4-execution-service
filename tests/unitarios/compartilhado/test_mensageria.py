"""Relay e consumidor sem broker: sinais do processo, telemetria e desfechos.

O desfecho de cada mensagem (ack, retry ou DLQ) e decidido no callback do
consumidor; aqui ele recebe um canal falso e um banco falso. O caminho com o
RabbitMQ de verdade esta em ``tests/integracao/test_mensageria.py``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

import pika
import pytest
import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc import trace_exporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import StatusCode
from pika.exceptions import (
    ChannelClosedByBroker,
    NackError,
    StreamLostError,
    UnroutableError,
)
from prometheus_client import REGISTRY
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

import src.consumidor
import src.relay
from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelException,
    EntidadeDuplicadaException,
    EntidadeNaoEncontradaException,
    RespostaInvalidaDaDependenciaException,
    TransicaoStatusInvalidaException,
    ValorInvalidoError,
    ViolacaoRegraDeNegocioException,
)
from src.compartilhado.infraestrutura.mensageria import amqp, processo, telemetria
from src.compartilhado.infraestrutura.mensageria import consumidor as modulo_consumidor
from src.compartilhado.infraestrutura.mensageria.consumidor import Consumidor
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS
from tests.fakes import FakeTransacaoDoComando

if TYPE_CHECKING:
    import io
    from collections.abc import Callable, Iterator, Mapping

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pika.adapters.blocking_connection import BlockingChannel
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWorkDoComando

_COMANDO = (CONTRATOS / "exemplos" / "ReservarPecas.json").read_bytes()
RAIZ = Path(__file__).resolve().parents[3]
_URL = "amqp://execucao:x@broker.test:5672/%2F"  # gitleaks:allow - nunca conecta
_BANCO_FALSO = "postgresql://x:y@127.0.0.1:1/nada"  # gitleaks:allow - nunca conecta


# --- sinais do processo -----------------------------------------------------


def test_heartbeat_e_prontidao_em_arquivo(tmp_path: Path) -> None:
    sinais = processo.SinaisDoProcesso(tmp_path / "hb", tmp_path / "pronto")
    sinais.indisponivel()  # sem arquivo ainda: nao e erro
    sinais.heartbeat()
    sinais.pronto()
    assert (tmp_path / "hb").exists()
    assert (tmp_path / "pronto").exists()
    sinais.indisponivel()
    assert not (tmp_path / "pronto").exists()


def test_arquivos_do_processo_ficam_no_tmp() -> None:
    sinais = processo.SinaisDoProcesso.do_processo("relay")
    assert sinais._heartbeat.name == "relay-heartbeat"
    assert sinais._pronto.name == "relay-pronto"


class _Espera:
    def __init__(self) -> None:
        self.esperas: list[float] = []

    def wait(self, segundos: float) -> bool:
        self.esperas.append(segundos)
        return False


def _evento(espera: _Espera) -> threading.Event:
    # Fake so com o wait que o Backoff usa.
    return cast("threading.Event", espera)


def test_backoff_sorteia_ate_o_atraso_que_dobra_ate_o_teto_e_reinicia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sorteios: list[tuple[float, float]] = []

    def metade(inicio: float, fim: float) -> float:
        sorteios.append((inicio, fim))
        return fim / 2

    monkeypatch.setattr(processo.random, "uniform", metade)
    backoff, espera = processo.Backoff(1.0, 5.0), _Espera()
    for _ in range(5):
        backoff.esperar(_evento(espera))
    backoff.reiniciar()
    backoff.esperar(_evento(espera))
    assert sorteios == [(0, 1.0), (0, 2.0), (0, 4.0), (0, 5.0), (0, 5.0), (0, 1.0)]
    assert espera.esperas == [0.5, 1.0, 2.0, 2.5, 2.5, 0.5]


def test_backoff_sem_sorteio_falso_fica_entre_zero_e_o_atraso() -> None:
    backoff, espera = processo.Backoff(1.0, 4.0), _Espera()
    for _ in range(50):
        backoff.esperar(_evento(espera))
    assert all(0 <= valor <= 4.0 for valor in espera.esperas)
    assert len(set(espera.esperas)) > 1  # replicas nao voltam juntas


def test_sigterm_liga_a_parada() -> None:
    anteriores = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        parar = processo.parada_por_sinal()
        assert not parar.is_set()
        os.kill(os.getpid(), signal.SIGTERM)
        assert parar.wait(2)
    finally:
        for sinal, handler in anteriores.items():
            signal.signal(sinal, handler)


@pytest.mark.parametrize(
    ("ambiente", "porta"),
    [
        pytest.param({}, 9100, id="padrao"),
        pytest.param({"METRICS_PORT": "9200"}, 9200, id="do-ambiente"),
    ],
)
def test_metricas_na_porta_metrics(
    monkeypatch: pytest.MonkeyPatch, ambiente: dict[str, str], porta: int
) -> None:
    portas: list[int] = []
    monkeypatch.delenv("METRICS_PORT", raising=False)
    for chave, valor in ambiente.items():
        monkeypatch.setenv(chave, valor)
    monkeypatch.setattr(processo, "start_http_server", portas.append)
    processo.servir_metricas()
    assert portas == [porta]


# --- telemetria ---------------------------------------------------------------


def test_contexto_w3c_so_dentro_de_span() -> None:
    assert telemetria.contexto_atual() == {}
    with telemetria.tracer.start_as_current_span("x") as span:
        portador = telemetria.contexto_atual()
    contexto = telemetria.contexto_de({**portador, "x-tentativa": 2, "nulo": None})
    lido = trace.get_current_span(contexto).get_span_context()
    assert lido.span_id == span.get_span_context().span_id


def test_tracestate_acima_do_limite_w3c_e_descartado() -> None:
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    grande = ",".join(f"v{i}=" + "x" * 20 for i in range(30))  # 30 membros, >512
    assert len(grande) > 512
    contexto = telemetria.contexto_de(
        {"traceparent": traceparent, "tracestate": grande}
    )
    lido = trace.get_current_span(contexto).get_span_context()
    assert lido.span_id == 0xB7AD6B7169203331
    assert len(lido.trace_state) == 0
    pequeno = telemetria.contexto_de({"traceparent": traceparent, "tracestate": "a=1"})
    assert trace.get_current_span(pequeno).get_span_context().trace_state["a"] == "1"


class _ExportadorFalso:
    criados: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).criados.append(kwargs)

    def export(self, _spans: object) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, _timeout: int = 0) -> bool:
        return True


def _exportador_falso(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ExportadorFalso, "criados", [])
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", _ExportadorFalso)


def test_provedor_sem_otel_enabled_nao_exporta(monkeypatch: pytest.MonkeyPatch) -> None:
    _exportador_falso(monkeypatch)
    provedor = telemetria.criar_provedor({}, processo="relay")
    assert provedor.resource.attributes["service.name"] == "execution-service"
    assert provedor.resource.attributes["pytstop.processo"] == "relay"
    assert _ExportadorFalso.criados == []


@pytest.mark.parametrize(
    ("endpoint", "inseguro"),
    [
        pytest.param("http://jaeger:4317", True, id="http"),
        pytest.param("https://otlp.exemplo:4317", False, id="https"),
    ],
)
def test_provedor_com_otel_enabled_exporta_por_otlp(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, inseguro: bool
) -> None:
    _exportador_falso(monkeypatch)
    provedor = telemetria.criar_provedor(
        {
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
            "OTEL_SERVICE_NAME": "execution-service-relay",
            "PYTSTOP_GIT_SHA": "0123456789abcdef",
        },
        processo="consumidor",
    )
    provedor.shutdown()
    assert _ExportadorFalso.criados == [{"endpoint": endpoint, "insecure": inseguro}]
    atributos = provedor.resource.attributes
    assert atributos["service.name"] == "execution-service-relay"
    assert atributos["service.version"] == "0123456789ab"
    assert atributos["pytstop.processo"] == "consumidor"


def test_configurar_telemetria_instala_o_provider_do_processo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instalados: list[object] = []
    monkeypatch.setattr(telemetria.trace, "set_tracer_provider", instalados.append)
    telemetria.configurar_telemetria("relay")
    (provedor,) = instalados
    assert isinstance(provedor, TracerProvider)
    assert provedor.resource.attributes["pytstop.processo"] == "relay"


def test_contexto_de_trace_nao_carrega_o_sdk_nem_o_grpc() -> None:
    # A UoW da API importa a telemetria: o SDK e o exportador so carregam no
    # relay e no consumidor (criar_provedor).
    codigo = (
        "import sys\n"
        "import src.compartilhado.infraestrutura.unit_of_work\n"
        "carregados = [m for m in sys.modules if m.startswith(('grpc', "
        "'opentelemetry.sdk', 'opentelemetry.exporter'))]\n"
        "assert carregados == [], carregados\n"
    )
    subprocess.run([sys.executable, "-c", codigo], check=True, cwd=RAIZ)  # noqa: S603 - comando fixo do teste


# --- desfecho de cada mensagem no consumidor ----------------------------------


class _Canal:
    """Canal falso; ``passos`` registra a ordem de publish, ack e reject."""

    def __init__(self, erro_na_copia: Exception | None = None) -> None:
        self.acks: list[int] = []
        self.rejeicoes: list[tuple[int, bool]] = []
        self.publicadas: list[dict[str, Any]] = []
        self.passos: list[str] = []
        self._erro_na_copia = erro_na_copia

    def basic_ack(self, delivery_tag: int) -> None:
        self.passos.append("ack")
        self.acks.append(delivery_tag)

    def basic_reject(self, delivery_tag: int, requeue: bool) -> None:
        self.passos.append("reject")
        self.rejeicoes.append((delivery_tag, requeue))

    def basic_publish(self, **kwargs: Any) -> None:
        # Com confirms, o basic_publish so volta depois da confirmacao do broker.
        self.passos.append("copia")
        if self._erro_na_copia is not None:
            raise self._erro_na_copia
        self.publicadas.append(kwargs)


class _Entrega:
    delivery_tag = 7


class _Sessao:
    """Sessao e engine falsas: ``begin`` e a limpeza da retencao (nada a apagar)."""

    rowcount = 0

    def begin(self) -> _Sessao:
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, *_: object) -> _Sessao:
        return self

    def rollback(self) -> None:
        pass


class _Transacoes:
    """Transacao da mensagem em memoria; ``processada`` simula o id ja gravado.

    ``comitadas`` conta as transacoes que o consumidor fechou sem excecao.
    """

    def __init__(self, processada: bool = False) -> None:
        self.processada = processada
        self.comitadas = 0
        self.ultima: FakeTransacaoDoComando | None = None

    @contextmanager
    def __call__(
        self, _comando_id: object
    ) -> Iterator[tuple[_Sessao, FakeTransacaoDoComando] | None]:
        if self.processada:
            yield None
            return
        self.ultima = FakeTransacaoDoComando()
        yield _Sessao(), self.ultima
        self.comitadas += 1


class _Handler:
    def __init__(
        self, erro: Exception | None = None, *, descartar: bool = False
    ) -> None:
        self.chamadas = 0
        self.envelopes: list[Mapping[str, Any]] = []
        self._erro = erro
        self._descartar = descartar

    def __call__(
        self, envelope: Mapping[str, Any], sessao: Session, uow: UnitOfWorkDoComando
    ) -> None:
        self.chamadas += 1
        self.envelopes.append(envelope)
        structlog.get_logger("teste.handler").info("handler_ran")
        if self._descartar:
            uow.descartar()
        if self._erro is not None:
            raise self._erro


def _consumidor(handler: _Handler, transacoes: _Transacoes | None = None) -> Consumidor:
    consumidor = Consumidor(
        _engine_falsa(),
        _URL,
        {"ReservarPecas": handler},
        processo.SinaisDoProcesso.do_processo("teste"),
    )
    # A transacao da mensagem em memoria (a de verdade e testada no Postgres).
    vars(consumidor)["_transacao"] = transacoes or _Transacoes()
    return consumidor


def _engine_falsa() -> Engine:
    # So a limpeza da retencao usa o engine fora da transacao da mensagem.
    return cast("Engine", _Sessao())


def _receber(
    consumidor: Consumidor, canal: _Canal, props: pika.BasicProperties, corpo: bytes
) -> None:
    consumidor._ao_receber(
        cast("BlockingChannel", canal),
        cast("pika.spec.Basic.Deliver", _Entrega()),
        props,
        corpo,
    )


def _consumir(
    handler: _Handler,
    *,
    corpo: bytes = _COMANDO,
    user_id: str | None = "os",
    headers: dict[str, Any] | None = None,
    transacoes: _Transacoes | None = None,
    tipo: str = "ReservarPecas",
    canal: _Canal | None = None,
) -> _Canal:
    canal = canal or _Canal()
    props = pika.BasicProperties(user_id=user_id, type=tipo, headers=headers)
    _receber(_consumidor(handler, transacoes), canal, props, corpo)
    return canal


def _consumidas(tipo: str, resultado: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total", {"tipo": tipo, "resultado": resultado}
    )
    return valor or 0.0


@pytest.mark.parametrize(
    ("handler", "resultado"),
    [
        pytest.param(_Handler(), "processada", id="processada"),
        pytest.param(_Handler(descartar=True), "ignorada", id="atrasado-descartado"),
        pytest.param(
            _Handler(ViolacaoRegraDeNegocioException()),
            "ignorada",
            id="estado-nao-corresponde",
        ),
    ],
)
def test_desfechos_com_ack_comitam_a_transacao_da_mensagem(
    handler: _Handler, resultado: str
) -> None:
    antes = _consumidas("ReservarPecas", resultado)
    transacoes = _Transacoes()
    canal = _consumir(handler, transacoes=transacoes)
    assert (canal.acks, canal.rejeicoes, canal.publicadas) == ([7], [], [])
    assert handler.envelopes[-1] == json.loads(_COMANDO)
    # Ignorada tambem comita: o id fica gravado e a reentrega e duplicada.
    assert transacoes.comitadas == 1
    assert _consumidas("ReservarPecas", resultado) == antes + 1


def test_id_ja_processado_recebe_ack_sem_chamar_o_handler() -> None:
    handler = _Handler()
    canal = _consumir(handler, transacoes=_Transacoes(processada=True))
    assert canal.acks == [7]
    assert handler.chamadas == 0


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(DependenciaIndisponivelException("fora"), id="dependencia"),
        pytest.param(OperationalError("SELECT 1", {}, Exception()), id="banco"),
        pytest.param(ConnectionResetError(), id="rede"),
    ],
)
def test_erro_transitorio_publica_copia_no_primeiro_nivel_e_da_ack(
    erro: Exception,
) -> None:
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    canal = _consumir(
        _Handler(erro), headers={"traceparent": traceparent, "x-death": ["..."]}
    )
    (copia,) = canal.publicadas
    assert (copia["exchange"], copia["routing_key"]) == (
        "pytstop.retry",
        "execucao.comandos.retry.1s",
    )
    assert copia["body"] == _COMANDO
    assert copia["mandatory"] is True
    props = copia["properties"]
    assert props.expiration is None  # o atraso e o TTL da fila do nivel
    # A copia leva o contexto do span do consumo: mesmo trace, filha dele.
    assert set(props.headers) == {"traceparent", "x-tentativa"}
    assert props.headers["x-tentativa"] == 1
    assert props.headers["traceparent"].split("-")[1] == traceparent.split("-")[1]
    assert props.headers["traceparent"] != traceparent
    assert props.user_id == "execucao"
    assert props.message_id == json.loads(_COMANDO)["id"]
    # A original so recebe ack depois da copia confirmada.
    assert canal.passos == ["copia", "ack"]


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(UnroutableError([]), id="sem-rota"),
        pytest.param(NackError([]), id="nack"),
    ],
)
def test_copia_de_retry_recusada_leva_a_original_para_a_dlq(erro: Exception) -> None:
    antes = _consumidas("ReservarPecas", "dlq")
    canal = _consumir(_Handler(OSError()), canal=_Canal(erro_na_copia=erro))
    assert canal.passos == ["copia", "reject"]
    assert (canal.rejeicoes, canal.acks) == ([(7, False)], [])
    assert _consumidas("ReservarPecas", "dlq") == antes + 1


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(ChannelClosedByBroker(403, "ACCESS_REFUSED"), id="recusada-403"),
        pytest.param(StreamLostError("caiu"), id="broker-caiu"),
    ],
)
def test_copia_de_retry_sem_canal_deixa_a_original_sem_ack_e_sem_reject(
    erro: Exception,
) -> None:
    # Canal fechado ou conexao perdida: a original volta na reconexao.
    canal = _Canal(erro_na_copia=erro)
    with pytest.raises(type(erro)):
        _consumir(_Handler(OSError()), canal=canal)
    assert (canal.acks, canal.rejeicoes) == ([], [])


def test_tracestate_acima_do_limite_nao_segue_para_a_copia_de_retry() -> None:
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    grande = ",".join(f"v{i}=" + "x" * 20 for i in range(30))
    canal = _consumir(
        _Handler(OSError()), headers={"traceparent": traceparent, "tracestate": grande}
    )
    (copia,) = canal.publicadas
    assert "tracestate" not in copia["properties"].headers


@pytest.mark.parametrize(
    ("tentativa", "nivel"),
    [
        pytest.param(1, "5s", id="segunda-falha-5s"),
        pytest.param(2, "15s", id="terceira-falha-15s"),
        pytest.param(3, "60s", id="quarta-falha-60s"),
        pytest.param(4, "300s", id="quinta-falha-300s"),
    ],
)
def test_cada_tentativa_vai_para_a_fila_do_proprio_atraso(
    tentativa: int, nivel: str
) -> None:
    canal = _consumir(
        _Handler(OSError()), user_id="execucao", headers={"x-tentativa": tentativa}
    )
    (copia,) = canal.publicadas
    assert copia["routing_key"] == f"execucao.comandos.retry.{nivel}"
    assert copia["properties"].headers["x-tentativa"] == tentativa + 1


def test_falha_depois_da_quinta_copia_vai_para_a_dlq() -> None:
    canal = _consumir(
        _Handler(OSError()), user_id="execucao", headers={"x-tentativa": 5}
    )
    assert (canal.rejeicoes, canal.acks, canal.publicadas) == ([(7, False)], [], [])


@pytest.mark.parametrize(
    ("kwargs", "erro"),
    [
        pytest.param({"corpo": b"{nao e json"}, None, id="corpo-invalido"),
        pytest.param({"user_id": "billing"}, None, id="produtor-errado"),
        pytest.param({"user_id": None}, None, id="sem-user-id"),
        pytest.param({"user_id": "execucao"}, None, id="proprio-usuario-sem-tentativa"),
        pytest.param({"headers": {"x-tentativa": "1"}}, None, id="tentativa-texto"),
        pytest.param({"headers": {"x-tentativa": -1}}, None, id="tentativa-negativa"),
        pytest.param({"headers": {"x-tentativa": 6}}, None, id="tentativa-acima-de-5"),
        pytest.param(
            {"corpo": (CONTRATOS / "exemplos" / "LiberarReserva.json").read_bytes()},
            None,
            id="tipo-sem-handler",
        ),
    ],
)
def test_erro_permanente_vai_direto_para_a_dlq(
    kwargs: dict[str, Any], erro: Exception | None
) -> None:
    canal = _consumir(_Handler(erro), **kwargs)
    assert (canal.rejeicoes, canal.acks, canal.publicadas) == ([(7, False)], [], [])


@pytest.mark.parametrize(
    ("erro", "resultado"),
    [
        pytest.param(
            OperationalError("SELECT 1", {}, Exception()), "retry", id="banco-timeout"
        ),
        pytest.param(
            InterfaceError("SELECT 1", {}, Exception()), "retry", id="conexao-fechada"
        ),
        pytest.param(PoolTimeoutError("pool cheio"), "retry", id="pool-cheio"),
        pytest.param(ConnectionResetError(), "retry", id="rede"),
        pytest.param(TimeoutError(), "retry", id="timeout-de-socket"),
        pytest.param(
            DependenciaIndisponivelException("fora"), "retry", id="dependencia-fora"
        ),
        pytest.param(
            EntidadeDuplicadaException("corrida"), "retry", id="corrida-nao-resolvida"
        ),
        pytest.param(ValorInvalidoError("placa"), "dlq", id="dominio-recusa-o-dado"),
        pytest.param(
            RespostaInvalidaDaDependenciaException("4xx"),
            "dlq",
            id="dependencia-recusa",
        ),
        pytest.param(
            IntegrityError("INSERT", {}, Exception()), "dlq", id="banco-recusa-o-dado"
        ),
        pytest.param(RuntimeError("defeito"), "dlq", id="erro-nao-classificado"),
        pytest.param(
            TransicaoStatusInvalidaException(),
            "ignorada",
            id="transicao-fora-do-estado",
        ),
        pytest.param(
            ViolacaoRegraDeNegocioException(), "ignorada", id="regra-do-estado"
        ),
        pytest.param(
            EntidadeNaoEncontradaException(), "ignorada", id="entidade-ausente"
        ),
    ],
)
def test_classificacao_de_cada_erro_do_handler(erro: Exception, resultado: str) -> None:
    # Transitorio nunca vira ack silencioso: copia confirmada, depois o ack.
    antes = _consumidas("ReservarPecas", resultado)
    transacoes = _Transacoes()
    canal = _consumir(_Handler(erro), transacoes=transacoes)
    passos = {
        "retry": ["copia", "ack"],
        "dlq": ["reject"],
        "ignorada": ["ack"],
    }
    assert canal.passos == passos[resultado]
    assert [c["routing_key"] for c in canal.publicadas] == (
        ["execucao.comandos.retry.1s"] if resultado == "retry" else []
    )
    # So o comando ignorado comita (o id fica gravado); o resto desfaz tudo.
    assert transacoes.comitadas == (1 if resultado == "ignorada" else 0)
    assert _consumidas("ReservarPecas", resultado) == antes + 1


_ANINHADO = b'{"id":' + b'{"a":' * 10_000 + b"1" + b"}" * 10_000 + b"}"


@pytest.mark.parametrize(
    "corpo",
    [
        pytest.param(b"[" * 100_000, id="acima-do-teto"),
        pytest.param(_ANINHADO, id="aninhado-no-id-dentro-do-teto"),
        pytest.param(b"\xff\xfe", id="utf8-invalido"),
        pytest.param(b"null", id="json-nao-objeto"),
    ],
)
def test_corpo_hostil_vai_para_a_dlq_sem_derrubar_o_consumidor(corpo: bytes) -> None:
    assert len(_ANINHADO) < 64 * 1024
    handler = _Handler()
    canal = _consumir(handler, corpo=corpo)
    assert (canal.rejeicoes, canal.acks, canal.publicadas) == ([(7, False)], [], [])
    assert handler.chamadas == 0


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(RecursionError(), id="recursao-na-validacao"),
        pytest.param(TypeError("x"), id="tipo-inesperado"),
        pytest.param(MemoryError(), id="memoria"),
    ],
)
def test_erro_inesperado_no_parse_vai_para_a_dlq(
    monkeypatch: pytest.MonkeyPatch, erro: Exception
) -> None:
    # Nenhuma excecao do parse sai do callback (derrubaria o processo e a
    # mensagem voltaria primeiro a cada reinicio).
    def explode(_envelope: object) -> None:
        raise erro

    monkeypatch.setattr(modulo_consumidor, "validar", explode)
    canal = _consumir(_Handler())
    assert canal.passos == ["reject"]


def test_corpo_acima_do_teto_nem_chega_ao_parse(log_capturado: io.StringIO) -> None:
    _consumir(_Handler(), corpo=b" " * (64 * 1024 + 1))
    assert "corpo acima de 65536 bytes" in log_capturado.getvalue()


@pytest.mark.parametrize(
    ("user_id", "tentativa"),
    [
        pytest.param("os", 1, id="produtor-com-x-tentativa"),
        pytest.param("billing", 1, id="outro-servico-com-x-tentativa"),
        pytest.param("admin", 2, id="admin-com-x-tentativa"),
    ],
)
def test_x_tentativa_so_vale_na_copia_do_proprio_consumidor(
    user_id: str, tentativa: int
) -> None:
    handler = _Handler()
    canal = _consumir(handler, user_id=user_id, headers={"x-tentativa": tentativa})
    assert (canal.rejeicoes, canal.acks, canal.publicadas) == ([(7, False)], [], [])
    assert handler.chamadas == 0


def test_atributos_do_span_sao_so_messaging_com_ids_conferidos(
    spans: InMemorySpanExporter,
) -> None:
    corpo = json.loads(_COMANDO)
    canal = _Canal()
    props = pika.BasicProperties(
        user_id="os",
        type="ReservarPecas",
        message_id=corpo["id"],
        correlation_id="x" * 10_000,  # nao e UUID: fica fora do span
    )
    _receber(_consumidor(_Handler()), canal, props, _COMANDO)
    (span,) = [s for s in spans.get_finished_spans() if s.name.startswith("process")]
    assert dict(span.attributes or {}) == {
        "messaging.system": "rabbitmq",
        "messaging.destination.name": "execucao.comandos",
        "messaging.message.id": corpo["id"],
        "pytstop.resultado": "processada",
    }


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        pytest.param(None, None, id="ausente"),
        pytest.param(7, None, id="nao-e-texto"),
        pytest.param("z" * 36, None, id="36-caracteres-sem-forma-de-uuid"),
        pytest.param("y" * 37, None, id="longo-demais"),
        pytest.param(
            "3277DB7E-8283-4DD9-89E7-DF3EEACB8710",
            "3277db7e-8283-4dd9-89e7-df3eeacb8710",
            id="uuid-canonico",
        ),
    ],
)
def test_so_o_que_tem_forma_de_uuid_entra_no_log_e_no_span(
    valor: object, esperado: str | None
) -> None:
    assert modulo_consumidor._uuid_ou_none(valor) == esperado


def test_log_de_mensagem_rejeitada_nao_carrega_o_corpo(
    log_capturado: io.StringIO,
) -> None:
    corpo = json.loads(_COMANDO)
    corpo["dados"]["pecas"] = [{"sku": "MARCADOR-DO-CORPO", "quantidade": 0}]
    canal = _Canal()
    props = pika.BasicProperties(
        user_id="os", type="ReservarPecas", message_id="y" * 5_000
    )
    _receber(_consumidor(_Handler()), canal, props, json.dumps(corpo).encode())
    saida = log_capturado.getvalue()
    (rejeicao,) = [
        json.loads(linha)
        for linha in saida.splitlines()
        if '"message_rejected"' in linha
    ]
    assert rejeicao["message_id"] is None
    assert "MARCADOR-DO-CORPO" not in saida
    assert "yyyy" not in saida


def test_copia_de_retry_do_proprio_consumidor_e_aceita() -> None:
    handler = _Handler()
    canal = _consumir(handler, user_id="execucao", headers={"x-tentativa": 1})
    assert handler.chamadas == 1
    assert canal.acks == [7]


def test_tipo_fora_do_mapa_vira_label_desconhecido() -> None:
    # O label nao cresce com o que o publicador inventar na propriedade type.
    antes = _consumidas("desconhecido", "processada")
    _consumir(_Handler(), tipo="Qualquer" * 20)
    assert _consumidas("desconhecido", "processada") == antes + 1


def test_logs_do_handler_saem_no_span_do_consumidor_e_sem_dado(
    log_capturado: io.StringIO, spans: InMemorySpanExporter
) -> None:
    _consumir(_Handler(RuntimeError("defeito")))
    _consumir(_Handler(OperationalError("SELECT placa", {}, Exception("ABC1234"))))
    eventos = [json.loads(linha) for linha in log_capturado.getvalue().splitlines()]
    processos = [s for s in spans.get_finished_spans() if s.name.startswith("process")]

    handler = [e for e in eventos if e["event"] == "handler_ran"]
    assert [e["span_id"] for e in handler] == [
        f"{s.context.span_id:016x}" for s in processos
    ]
    assert handler[0]["correlation_id"] == json.loads(_COMANDO)["correlation_id"]
    (falha,) = [e for e in eventos if e["event"] == "command_failed"]
    assert "RuntimeError: defeito" in falha["exception"]
    (retry,) = [e for e in eventos if e["event"] == "message_failed_will_retry"]
    assert retry["error"] == "OperationalError"
    assert "ABC1234" not in log_capturado.getvalue()


def test_trace_id_do_log_e_o_do_span_do_consumidor(
    log_capturado: io.StringIO, spans: InMemorySpanExporter
) -> None:
    _consumir(_Handler())
    (processo_,) = [
        s for s in spans.get_finished_spans() if s.name.startswith("process")
    ]
    eventos = [json.loads(linha) for linha in log_capturado.getvalue().splitlines()]
    (handler,) = [e for e in eventos if e["event"] == "handler_ran"]
    assert handler["trace_id"] == f"{processo_.context.trace_id:032x}"


@pytest.mark.parametrize(
    ("erro", "status"),
    [
        pytest.param(OSError(), StatusCode.ERROR, id="retry"),
        pytest.param(RuntimeError("defeito"), StatusCode.ERROR, id="dlq"),
        pytest.param(None, StatusCode.UNSET, id="processada"),
        pytest.param(
            ViolacaoRegraDeNegocioException(), StatusCode.UNSET, id="ignorada"
        ),
    ],
)
def test_span_do_consumidor_marca_erro_so_em_retry_e_dlq(
    spans: InMemorySpanExporter, erro: Exception | None, status: StatusCode
) -> None:
    _consumir(_Handler(erro))
    (span,) = [s for s in spans.get_finished_spans() if s.name.startswith("process")]
    assert span.status.status_code is status


class _ProcessoFalso:
    """Relay ou Consumidor no main(): registra como foi montado e executado."""

    criados: ClassVar[list[_ProcessoFalso]] = []

    def __init__(self, *args: Any) -> None:
        self.args = args
        self.parar: Any = None
        type(self).criados.append(self)

    def executar(self, parar: Any) -> None:
        self.parar = parar


class _EngineFalsa:
    disposta = False

    def dispose(self) -> None:
        type(self).disposta = True


@pytest.mark.parametrize(
    ("modulo", "classe", "nome"),
    [
        pytest.param(src.relay, "Relay", "relay", id="relay"),
        pytest.param(src.consumidor, "Consumidor", "consumidor", id="consumidor"),
    ],
)
def test_main_executa_o_processo_com_os_sinais_proprios_e_libera_o_engine(
    modulo: Any, classe: str, nome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arquivos de sinal trocados (o healthcheck do compose nao os acharia) ou
    # main sem executar o processo passavam no teste de subida.
    parado = threading.Event()
    monkeypatch.setattr(_ProcessoFalso, "criados", [])
    monkeypatch.setattr(_EngineFalsa, "disposta", False)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://x:y@127.0.0.1:1/nada"
    )  # gitleaks:allow
    monkeypatch.setenv("RABBITMQ_URL", _URL)
    monkeypatch.setattr(modulo, classe, _ProcessoFalso)
    monkeypatch.setattr(modulo, "criar_engine", lambda _url: _EngineFalsa())
    monkeypatch.setattr(modulo, "parada_por_sinal", lambda: parado)
    monkeypatch.setattr(modulo, "configurar_logging", lambda: None)
    monkeypatch.setattr(modulo, "configurar_telemetria", lambda _processo: None)
    monkeypatch.setattr(modulo, "servir_metricas", lambda: None)

    modulo.main()

    (processo_,) = _ProcessoFalso.criados
    sinais = next(a for a in processo_.args if isinstance(a, processo.SinaisDoProcesso))
    assert sinais._heartbeat.name == f"{nome}-heartbeat"
    assert sinais._pronto.name == f"{nome}-pronto"
    assert processo_.parar is parado
    assert _EngineFalsa.disposta


# --- laco do consumidor (sem broker) ------------------------------------------


class _BackoffGravado(processo.Backoff):
    def __init__(self) -> None:
        super().__init__(0.0, 0.0)
        self.esperas = 0

    def esperar(self, parar: threading.Event) -> None:
        self.esperas += 1


class _ConexaoFalsa:
    """Uma volta do laco por chamada; ``passos`` diz o que cada volta faz."""

    def __init__(self, passos: list[Callable[[], None]]) -> None:
        self._passos = passos
        self.is_open = True

    def process_data_events(self, time_limit: float) -> None:
        self._passos.pop(0)()

    def close(self) -> None:
        self.is_open = False


class _CanalFalso:
    def __init__(self) -> None:
        self.ao_cancelar: Callable[[object], None] | None = None
        self.prefetch: int | None = None

    def basic_qos(self, prefetch_count: int) -> None:
        self.prefetch = prefetch_count

    def add_on_cancel_callback(self, callback: Callable[[object], None]) -> None:
        self.ao_cancelar = callback

    def basic_consume(self, fila: str, callback: object) -> None:
        pass

    def confirm_delivery(self) -> None:
        pass


def _rodar_laco(
    monkeypatch: pytest.MonkeyPatch,
    primeira_volta: Callable[[_CanalFalso], None],
    tmp_path: Path,
) -> tuple[int, _BackoffGravado]:
    """Primeira conexao: ``primeira_volta``; a segunda conexao pede a parada."""
    parar = threading.Event()
    canais: list[_CanalFalso] = []

    def abrir(*_: object, **__: object) -> tuple[_ConexaoFalsa, _CanalFalso]:
        canal = _CanalFalso()
        canais.append(canal)
        volta = (lambda: primeira_volta(canal)) if len(canais) == 1 else parar.set
        return _ConexaoFalsa([volta]), canal

    monkeypatch.setattr(modulo_consumidor, "abrir_canal", abrir)
    backoff = _BackoffGravado()
    Consumidor(
        _engine_falsa(),
        _URL,
        {},
        processo.SinaisDoProcesso(tmp_path / "hb", tmp_path / "pronto"),
        backoff,
    ).executar(parar)
    return len(canais), backoff


def test_conexao_tem_heartbeat_explicito_e_teto_de_bloqueio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parametros: list[pika.URLParameters] = []

    class _ConexaoDoPika:
        is_open = True

        def __init__(self, params: pika.URLParameters) -> None:
            parametros.append(params)

        def channel(self) -> _CanalFalso:
            return _CanalFalso()

    monkeypatch.setattr(amqp.pika, "BlockingConnection", _ConexaoDoPika)
    amqp.abrir_canal(_URL)
    (params,) = parametros
    assert (params.heartbeat, params.blocked_connection_timeout) == (60, 30.0)


def test_topologia_fora_do_alcance_fecha_a_conexao_antes_de_levantar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fechadas: list[bool] = []

    class _ConexaoRecusada:
        is_open = True

        def __init__(self, _params: object) -> None:
            pass

        def channel(self) -> Any:
            raise ChannelClosedByBroker(403, "ACCESS_REFUSED")

        def close(self) -> None:
            fechadas.append(True)

    monkeypatch.setattr(amqp.pika, "BlockingConnection", _ConexaoRecusada)
    with pytest.raises(ChannelClosedByBroker):
        amqp.abrir_canal(_URL)
    assert fechadas == [True]


def test_assinatura_que_falha_fecha_a_conexao_antes_de_levantar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexao = _ConexaoFalsa([])

    class _CanalQueRecusa(_CanalFalso):
        def basic_consume(self, fila: str, callback: object) -> None:
            raise ChannelClosedByBroker(403, "ACCESS_REFUSED")

    monkeypatch.setattr(
        modulo_consumidor,
        "abrir_canal",
        lambda *_a, **_k: (conexao, _CanalQueRecusa()),
    )
    with pytest.raises(ChannelClosedByBroker):
        _consumidor(_Handler())._assinar()
    assert not conexao.is_open


def test_consumidor_assina_com_uma_mensagem_em_voo_por_vez(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canal = _CanalFalso()
    monkeypatch.setattr(
        modulo_consumidor, "abrir_canal", lambda *_a, **_k: (_ConexaoFalsa([]), canal)
    )
    _consumidor(_Handler())._assinar()
    assert canal.prefetch == 1


def test_assinatura_cancelada_pelo_broker_vira_nova_assinatura(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_capturado: io.StringIO
) -> None:
    def cancelar(canal: _CanalFalso) -> None:
        assert canal.ao_cancelar is not None
        canal.ao_cancelar(object())  # Basic.Cancel: o pika so chama o callback

    assinaturas, backoff = _rodar_laco(monkeypatch, cancelar, tmp_path)
    assert (assinaturas, backoff.esperas) == (2, 1)
    assert '"error": "ConsumerCancelled"' in log_capturado.getvalue()


def test_copia_recusada_pelo_broker_reconecta_com_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_capturado: io.StringIO
) -> None:
    def recusar(_canal: _CanalFalso) -> None:
        # basic_publish da copia num canal que o broker fechou (403 ou 406).
        raise ChannelClosedByBroker(403, "ACCESS_REFUSED")

    assinaturas, backoff = _rodar_laco(monkeypatch, recusar, tmp_path)
    assert (assinaturas, backoff.esperas) == (2, 1)
    assert '"error": "ChannelClosedByBroker"' in log_capturado.getvalue()
