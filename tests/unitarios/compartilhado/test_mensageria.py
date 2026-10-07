"""Relay e consumidor sem broker: sinais do processo, telemetria e desfechos.

O desfecho de cada mensagem (ack, retry ou DLQ) e decidido no callback do
consumidor; aqui ele recebe um canal falso e um banco falso. O caminho com o
RabbitMQ de verdade esta em ``tests/integracao/test_mensageria.py``.
"""

from __future__ import annotations

import json
import os
import signal
from typing import TYPE_CHECKING, Any, ClassVar, Self

import pika
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import REGISTRY
from sqlalchemy.exc import OperationalError

from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelException,
    ValorInvalidoError,
    ViolacaoRegraDeNegocioException,
)
from src.compartilhado.infraestrutura.mensageria import processo, telemetria
from src.compartilhado.infraestrutura.mensageria.consumidor import Consumidor
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS
from src.compartilhado.infraestrutura.unit_of_work import MensagemJaProcessadaError

if TYPE_CHECKING:
    import io
    from collections.abc import Mapping
    from pathlib import Path

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork

_COMANDO = (CONTRATOS / "exemplos" / "ReservarPecas.json").read_bytes()
_URL = "amqp://execucao:x@broker.test:5672/%2F"  # gitleaks:allow - nunca conecta


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


def test_backoff_dobra_ate_o_teto_e_reinicia() -> None:
    backoff, espera = processo.Backoff(1.0, 5.0), _Espera()
    for _ in range(5):
        backoff.esperar(espera)  # type: ignore[arg-type]
    backoff.reiniciar()
    backoff.esperar(espera)  # type: ignore[arg-type]
    assert espera.esperas == [1.0, 2.0, 4.0, 5.0, 5.0, 1.0]


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
    ("ambiente", "porta"), [({}, 9100), ({"METRICS_PORT": "9200"}, 9200)]
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


def test_provedor_sem_otel_enabled_nao_exporta(monkeypatch: pytest.MonkeyPatch) -> None:
    _ExportadorFalso.criados = []
    monkeypatch.setattr(telemetria, "OTLPSpanExporter", _ExportadorFalso)
    provedor = telemetria.criar_provedor({})
    assert provedor.resource.attributes["service.name"] == "execution-service"
    assert _ExportadorFalso.criados == []


@pytest.mark.parametrize(
    ("endpoint", "inseguro"),
    [("http://jaeger:4317", True), ("https://otlp.exemplo:4317", False)],
)
def test_provedor_com_otel_enabled_exporta_por_otlp(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, inseguro: bool
) -> None:
    _ExportadorFalso.criados = []
    monkeypatch.setattr(telemetria, "OTLPSpanExporter", _ExportadorFalso)
    provedor = telemetria.criar_provedor(
        {
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
            "OTEL_SERVICE_NAME": "execution-service-relay",
            "PYTSTOP_GIT_SHA": "0123456789abcdef",
        }
    )
    provedor.shutdown()
    assert _ExportadorFalso.criados == [{"endpoint": endpoint, "insecure": inseguro}]
    atributos = provedor.resource.attributes
    assert atributos["service.name"] == "execution-service-relay"
    assert atributos["service.version"] == "0123456789ab"


def test_configurar_telemetria_instala_o_provider_do_processo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instalados: list[object] = []
    monkeypatch.setattr(telemetria.trace, "set_tracer_provider", instalados.append)
    telemetria.configurar_telemetria()
    (provedor,) = instalados
    assert isinstance(provedor, TracerProvider)


# --- desfecho de cada mensagem no consumidor ----------------------------------


class _Canal:
    def __init__(self) -> None:
        self.acks: list[int] = []
        self.rejeicoes: list[tuple[int, bool]] = []
        self.publicadas: list[dict[str, Any]] = []

    def basic_ack(self, delivery_tag: int) -> None:
        self.acks.append(delivery_tag)

    def basic_reject(self, delivery_tag: int, requeue: bool) -> None:
        self.rejeicoes.append((delivery_tag, requeue))

    def basic_publish(self, **kwargs: Any) -> None:
        self.publicadas.append(kwargs)


class _Entrega:
    delivery_tag = 7


class _Sessao:
    """Sessao falsa: ``processada`` diz se o id ja esta em mensagens_processadas."""

    def __init__(self, processada: bool = False) -> None:
        self._processada = processada

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, *_: object) -> _Sessao:
        return self

    def first(self) -> object:
        return object() if self._processada else None

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Handler:
    def __init__(self, erro: Exception | None = None) -> None:
        self.chamadas = 0
        self._erro = erro

    def __call__(
        self, dados: Mapping[str, Any], sessao: Session, uow: UnitOfWork
    ) -> None:
        self.chamadas += 1
        if self._erro is not None:
            raise self._erro


def _consumir(
    handler: _Handler,
    *,
    corpo: bytes = _COMANDO,
    user_id: str | None = "os",
    headers: dict[str, Any] | None = None,
    processada: bool = False,
    tipo: str = "ReservarPecas",
) -> _Canal:
    consumidor = Consumidor(
        lambda: _Sessao(processada),  # type: ignore[arg-type,return-value]
        _URL,
        {"ReservarPecas": handler},
        processo.SinaisDoProcesso.do_processo("teste"),
        atrasos_ms=(10, 20, 30, 40, 50),
    )
    canal = _Canal()
    props = pika.BasicProperties(user_id=user_id, type=tipo, headers=headers)
    consumidor._ao_receber(canal, _Entrega(), props, corpo)  # type: ignore[arg-type]
    return canal


def _consumidas(tipo: str, resultado: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total", {"tipo": tipo, "resultado": resultado}
    )
    return valor or 0.0


@pytest.mark.parametrize(
    ("erro", "resultado"),
    [
        pytest.param(None, "ignorada", id="sem-commit-e-ignorada"),
        pytest.param(MensagemJaProcessadaError(), "duplicada", id="corrida-do-id"),
        pytest.param(
            ViolacaoRegraDeNegocioException(), "ignorada", id="estado-nao-corresponde"
        ),
    ],
)
def test_desfechos_com_ack(erro: Exception | None, resultado: str) -> None:
    antes = _consumidas("ReservarPecas", resultado)
    handler = _Handler(erro)
    canal = _consumir(handler)
    assert (canal.acks, canal.rejeicoes, canal.publicadas) == ([7], [], [])
    assert handler.chamadas == 1
    assert _consumidas("ReservarPecas", resultado) == antes + 1


def test_id_ja_processado_recebe_ack_sem_chamar_o_handler() -> None:
    handler = _Handler()
    canal = _consumir(handler, processada=True)
    assert canal.acks == [7]
    assert handler.chamadas == 0


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(DependenciaIndisponivelException("fora"), id="dependencia"),
        pytest.param(OperationalError("SELECT 1", {}, Exception()), id="banco"),
        pytest.param(RuntimeError("defeito"), id="inesperado"),
    ],
)
def test_erro_transitorio_publica_copia_na_retry_e_da_ack(erro: Exception) -> None:
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    canal = _consumir(
        _Handler(erro), headers={"traceparent": traceparent, "x-death": ["..."]}
    )
    (copia,) = canal.publicadas
    assert (copia["exchange"], copia["routing_key"]) == (
        "pytstop.retry",
        "execucao.comandos",
    )
    assert copia["body"] == _COMANDO
    assert copia["mandatory"] is True
    props = copia["properties"]
    assert props.expiration == "10"
    assert props.headers == {"traceparent": traceparent, "x-tentativa": 1}
    assert props.user_id == "execucao"
    assert props.message_id == json.loads(_COMANDO)["id"]
    assert canal.acks == [7]


def test_quinta_copia_que_falha_vai_para_a_dlq() -> None:
    canal = _consumir(
        _Handler(RuntimeError()), user_id="execucao", headers={"x-tentativa": 5}
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
        pytest.param(
            {"corpo": (CONTRATOS / "exemplos" / "LiberarReserva.json").read_bytes()},
            None,
            id="tipo-sem-handler",
        ),
        pytest.param({}, ValorInvalidoError("placa"), id="dado-recusado-pelo-dominio"),
    ],
)
def test_erro_permanente_vai_direto_para_a_dlq(
    kwargs: dict[str, Any], erro: Exception | None
) -> None:
    canal = _consumir(_Handler(erro), **kwargs)
    assert (canal.rejeicoes, canal.acks, canal.publicadas) == ([(7, False)], [], [])


def test_copia_de_retry_do_proprio_consumidor_e_aceita() -> None:
    handler = _Handler()
    canal = _consumir(handler, user_id="execucao", headers={"x-tentativa": 1})
    assert handler.chamadas == 1
    assert canal.acks == [7]


def test_tipo_fora_do_mapa_vira_label_desconhecido() -> None:
    # O label nao cresce com o que o publicador inventar na propriedade type.
    antes = _consumidas("desconhecido", "ignorada")
    _consumir(_Handler(), tipo="Qualquer" * 20)
    assert _consumidas("desconhecido", "ignorada") == antes + 1


def test_log_do_erro_inesperado_leva_o_traceback_e_o_de_banco_nao(
    log_capturado: io.StringIO,
) -> None:
    _consumir(_Handler(RuntimeError("defeito")))
    _consumir(_Handler(OperationalError("SELECT placa", {}, Exception("ABC1234"))))
    eventos = [json.loads(linha) for linha in log_capturado.getvalue().splitlines()]
    falhas = [e for e in eventos if e["event"] == "message_failed_will_retry"]
    assert "RuntimeError: defeito" in falhas[0]["exception"]
    assert falhas[1]["error"] == "OperationalError"
    assert "ABC1234" not in log_capturado.getvalue()
    consumo = [e for e in eventos if e["event"] == "message_consumed"]
    assert consumo[0]["correlation_id"] == json.loads(_COMANDO)["correlation_id"]
    assert consumo[0]["trace_id"]
