"""Relay da outbox: lease, fencing, ordem, falhas do broker e do banco, retencao.

Nos caminhos do RabbitMQ (sem rota, nack, recusa 403, queda, alarme de memoria)
o broker e o de verdade, com a topologia do platform; na coordenacao entre
replicas, que depende da ordem exata dos passos, um broker falso faz o papel do
``_Broker`` e o banco e o real.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from src.compartilhado.infraestrutura.database import criar_engine
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria import consumidor as modulo_consumidor
from src.compartilhado.infraestrutura.mensageria import relay as modulo_relay
from src.compartilhado.infraestrutura.mensageria.contratos import (
    CONTRATOS,
    MensagemInvalidaError,
)
from src.compartilhado.infraestrutura.mensageria.outbox import Outbox
from src.compartilhado.infraestrutura.mensageria.processo import (
    Backoff,
    SinaisDoProcesso,
)
from src.compartilhado.infraestrutura.mensageria.relay import (
    BrokerIndisponivelError,
    Relay,
)
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.transacao import linha_pendente

if TYPE_CHECKING:
    import io
    from collections.abc import Callable
    from pathlib import Path

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.compartilhado.infraestrutura.mensageria.consumidor import Consumidor
    from src.compartilhado.infraestrutura.mensageria.outbox import LinhaDaOutbox
    from tests.integracao.broker import Broker

_URL_FALSA = "amqp://execucao:x@broker.test/%2F"  # gitleaks:allow - nunca conecta


def _gravar(
    engine: Engine,
    *,
    ordem_id: UUID | None = None,
    envelope: dict[str, Any] | None = None,
    daqui_a: timedelta = timedelta(0),
) -> str:
    """Grava uma ReservaLiberada pendente direto na outbox; devolve o id."""
    ordem = ordem_id or uuid4()
    mensagem = (
        envelope
        if envelope is not None
        else {
            "id": str(uuid4()),
            "tipo": "ReservaLiberada",
            "versao": 1,
            "origem": "execution-service",
            "correlation_id": str(ordem),
            "causation_id": str(uuid4()),
            "ocorrido_em": "2026-10-06T12:00:00+00:00",
            "dados": {"ordem_id": str(ordem)},
        }
    )
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
                "routing_key, envelope, proxima_tentativa_em) VALUES (:id, "
                "'ReservaLiberada', :ordem, 'pytstop.eventos', "
                "'evento.execucao.reserva_liberada', CAST(:envelope AS jsonb), "
                "now() + :daqui_a)"
            ),
            {
                "id": mensagem.get("id", str(uuid4())),
                "ordem": ordem,
                "envelope": json.dumps(mensagem),
                "daqui_a": daqui_a,
            },
        )
    return str(mensagem.get("id"))


def _pendentes(engine: Engine) -> int:
    with engine.connect() as conexao:
        return int(
            conexao.execute(
                text("SELECT count(*) FROM outbox WHERE status = 'pendente'")
            ).scalar_one()
        )


# --- com o broker de verdade ---------------------------------------------------


def test_relay_acorda_pelo_notify_sem_esperar_o_poll(
    broker: Broker,
    engine: Engine,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    tmp_path: Path,
) -> None:
    processo = EmSegundoPlano(relay(poll_s=60))
    with processo:
        esperar_ate((tmp_path / "relay-pronto").exists)
        linha_pendente(session_factory)
        esperar_ate(lambda: broker.pegar("os.eventos"), prazo_s=10)
        # Acorda o select para o relay ver o pedido de parada.
        processo.parar.set()
        with engine.begin() as conexao:
            conexao.execute(text("SELECT pg_notify('outbox_novo', '')"))


def test_mensagem_sem_rota_conta_tentativa_e_nao_vira_entregue(
    broker: Broker,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    with broker.canal() as canal:
        canal.queue_unbind("os.eventos", "pytstop.eventos", "evento.execucao.#")
    try:
        linha_pendente(session_factory)
        with EmSegundoPlano(relay()):
            esperar_ate(lambda: outbox()[0]["tentativas"] == 1)
    finally:
        with broker.canal() as canal:
            canal.queue_bind("os.eventos", "pytstop.eventos", "evento.execucao.#")

    (linha,) = outbox()
    assert linha["status"] == "pendente"
    assert linha["ultimo_erro"] == "UnroutableError"


def test_publicacao_devolvida_nao_leva_o_corpo_para_o_log(
    broker: Broker,
    engine: Engine,
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
    log_capturado: io.StringIO,
) -> None:
    # O pika loga em WARNING os 255 primeiros bytes do corpo devolvido pelo
    # broker (mandatory); o texto livre do envelope nao pode chegar ao log.
    envelope = json.loads(
        (CONTRATOS / "exemplos" / "DiagnosticoConcluido.json").read_text()
    )
    envelope["dados"]["observacoes"] = "MARCADOR-DO-CORPO Joao da Silva"
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
                "routing_key, envelope) VALUES (:id, 'DiagnosticoConcluido', :ordem, "
                "'pytstop.eventos', 'evento.execucao.diagnostico_concluido', "
                "CAST(:envelope AS jsonb))"
            ),
            {
                "id": envelope["id"],
                "ordem": envelope["correlation_id"],
                "envelope": json.dumps(envelope),
            },
        )
    with broker.canal() as canal:
        canal.queue_unbind("os.eventos", "pytstop.eventos", "evento.execucao.#")
    try:
        with EmSegundoPlano(relay()):
            esperar_ate(lambda: outbox()[0]["tentativas"] == 1)
    finally:
        with broker.canal() as canal:
            canal.queue_bind("os.eventos", "pytstop.eventos", "evento.execucao.#")

    saida = log_capturado.getvalue()
    assert "message_publish_failed" in saida
    assert "MARCADOR-DO-CORPO" not in saida
    assert "Published message was returned" not in saida


def test_quinta_falha_de_publicacao_vira_dead(
    broker: Broker,
    engine: Engine,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    linha_pendente(session_factory)
    with engine.begin() as conexao:
        conexao.execute(text("UPDATE outbox SET tentativas = 4"))
    with broker.canal() as canal:
        canal.queue_unbind("os.eventos", "pytstop.eventos", "evento.execucao.#")
    try:
        with EmSegundoPlano(relay()):
            esperar_ate(lambda: outbox()[0]["status"] == "dead")
    finally:
        with broker.canal() as canal:
            canal.queue_bind("os.eventos", "pytstop.eventos", "evento.execucao.#")
    assert outbox()[0]["tentativas"] == 5
    assert REGISTRY.get_sample_value("outbox_dead") == 1


def test_nack_do_broker_conta_tentativa_e_nao_vira_entregue(
    broker: Broker,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    for _ in range(4):
        linha_pendente(session_factory)
    # max-length 1 com reject-publish: a quorum aceita duas e da nack na terceira.
    broker.rabbitmqctl(
        "set_policy", "--apply-to", "queues", "--priority", "100", "teste-nack",
        r"^os\.eventos$", '{"max-length":1,"overflow":"reject-publish"}',
    )  # fmt: skip
    try:
        with EmSegundoPlano(relay()):
            esperar_ate(lambda: any(linha["tentativas"] >= 1 for linha in outbox()))
    finally:
        broker.rabbitmqctl("clear_policy", "teste-nack")
    linhas = outbox()
    recusadas = [linha for linha in linhas if linha["tentativas"] >= 1]
    assert {(r["status"], r["ultimo_erro"]) for r in recusadas} == {
        ("pendente", "NackError")
    }
    assert any(linha["status"] == "entregue" for linha in linhas)


def test_recusa_do_broker_conta_tentativa_e_o_canal_e_reaberto(
    broker: Broker,
    engine: Engine,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # A permissao de topico do usuario execucao so cobre evento.execucao.*:
    # o broker recusa (403) e fecha o canal.
    linha_pendente(session_factory)
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "UPDATE outbox SET routing_key = 'evento.billing.pagamento_confirmado'"
            )
        )
    with EmSegundoPlano(relay()):
        esperar_ate(lambda: outbox()[0]["tentativas"] == 1)
        linha_pendente(session_factory)
        esperar_ate(lambda: len(outbox()) == 2 and outbox()[1]["status"] == "entregue")

    recusada = outbox()[0]
    assert recusada["status"] == "pendente"
    # Texto fixo: classe e codigo, nada do que o broker devolveu.
    assert recusada["ultimo_erro"] == "ChannelClosedByBroker (403)"


def _vezes_fora_do_ar(log: io.StringIO) -> int:
    return log.getvalue().count('"dependencia": "broker"')


def test_broker_parado_nao_gasta_tentativa_e_a_entrega_sai_na_volta(
    broker: Broker,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
    tmp_path: Path,
    log_capturado: io.StringIO,
) -> None:
    pronto = tmp_path / "relay-pronto"
    with EmSegundoPlano(relay()):
        esperar_ate(pronto.exists)
        broker.rabbitmqctl("stop_app")
        try:
            esperar_ate(lambda: not pronto.exists())
            linha_pendente(session_factory)
            # Janela medida em tentativas de reconexao, nao em segundos.
            vistas = _vezes_fora_do_ar(log_capturado)
            esperar_ate(lambda: _vezes_fora_do_ar(log_capturado) >= vistas + 2)
            (linha,) = outbox()
            assert (linha["status"], linha["tentativas"]) == ("pendente", 0)
        finally:
            broker.rabbitmqctl("start_app")
        esperar_ate(lambda: outbox()[0]["status"] == "entregue", prazo_s=60)
    assert outbox()[0]["tentativas"] == 0
    assert esperar_ate(lambda: broker.pegar("os.eventos"))


def test_queda_real_do_broker_no_meio_do_lote_devolve_as_linhas_sem_gastar_tentativa(
    broker: Broker,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for _ in range(3):
        linha_pendente(session_factory)
    publicar = modulo_relay._Broker.publicar
    chamadas: list[int] = []

    def derruba_o_broker_na_segunda(
        self: modulo_relay._Broker, linha: LinhaDaOutbox
    ) -> str | None:
        chamadas.append(linha.id)
        if len(chamadas) == 2:
            broker.rabbitmqctl("stop_app")
        return publicar(self, linha)

    monkeypatch.setattr(modulo_relay._Broker, "publicar", derruba_o_broker_na_segunda)
    try:
        with EmSegundoPlano(relay()):
            esperar_ate(lambda: len(chamadas) >= 2)
            esperar_ate(lambda: not (tmp_path / "relay-pronto").exists(), prazo_s=30)
            assert [(r["status"], r["tentativas"]) for r in outbox()][1:] == [
                ("pendente", 0),
                ("pendente", 0),
            ]
            broker.rabbitmqctl("start_app")
            # Prazo menor que o lease de 60 s: sem devolver o lease, as linhas
            # esperariam por ele.
            esperar_ate(
                lambda: all(r["status"] == "entregue" for r in outbox()), prazo_s=30
            )
    finally:
        broker.rabbitmqctl("start_app")
    assert [r["tentativas"] for r in outbox()] == [0, 0, 0]


def test_alarme_de_memoria_nao_trava_o_relay_nem_gasta_tentativa(
    broker: Broker,
    session_factory: sessionmaker[Session],
    relay: Callable[..., Relay],
    outbox: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Sem transacao aberta no publish, o bloqueio nao toma o timeout de
    # transacao ociosa do banco; o timeout do bloqueio derruba a conexao como
    # uma queda (sem tentativa) e a entrega sai quando o alarme passa.
    monkeypatch.setattr(amqp, "_BLOQUEIO_MAXIMO_S", 1.0)
    linha_pendente(session_factory)
    broker.rabbitmqctl("set_vm_memory_high_watermark", "absolute", "1MiB")
    try:
        with EmSegundoPlano(relay()):
            esperar_ate((tmp_path / "relay-pronto").exists)
            esperar_ate(lambda: not (tmp_path / "relay-pronto").exists(), prazo_s=30)
            assert [(r["status"], r["tentativas"]) for r in outbox()] == [
                ("pendente", 0)
            ]
            broker.rabbitmqctl("set_vm_memory_high_watermark", "absolute", "512MiB")
            esperar_ate(lambda: outbox()[0]["status"] == "entregue", prazo_s=60)
    finally:
        # O valor do rabbitmq.conf do platform.
        broker.rabbitmqctl("set_vm_memory_high_watermark", "absolute", "512MiB")
    assert outbox()[0]["tentativas"] == 0


def test_banco_fora_tira_a_prontidao_e_o_gauge_vira_nan(
    broker: Broker, tmp_path: Path, log_capturado: io.StringIO
) -> None:
    morto = criar_engine("postgresql://x:y@127.0.0.1:1/nada")  # gitleaks:allow
    sinais = SinaisDoProcesso(tmp_path / "hb", tmp_path / "pronto")
    relay = Relay(morto, broker.url("execucao"), sinais, backoff=Backoff(0.05, 0.1))
    with EmSegundoPlano(relay):
        esperar_ate(lambda: "relay_dependency_unavailable" in log_capturado.getvalue())
        assert not (tmp_path / "pronto").exists()
        assert (tmp_path / "hb").exists()
    assert math.isnan(REGISTRY.get_sample_value("outbox_pendentes") or 0.0)
    assert '"dependencia": "database"' in log_capturado.getvalue()


def test_retencao_apaga_entregues_e_processadas_antigas(
    broker: Broker,
    engine: Engine,
    relay: Callable[..., Relay],
    consumidor: Callable[..., Consumidor],
    outbox: Callable[[], list[dict[str, Any]]],
    spans: InMemorySpanExporter,
) -> None:
    agora = datetime.now(UTC)
    with engine.begin() as conexao:
        for dias, status in [(8, "entregue"), (6, "entregue"), (30, "dead")]:
            conexao.execute(
                text(
                    "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
                    "routing_key, envelope, status, entregue_em) VALUES "
                    "(gen_random_uuid(), 'ReservaLiberada', gen_random_uuid(), "
                    "'pytstop.eventos', 'evento.execucao.reserva_liberada', '{}', "
                    ":status, :quando)"
                ),
                {"status": status, "quando": agora - timedelta(days=dias)},
            )
        for dias in (31, 29):
            conexao.execute(
                text(
                    "INSERT INTO mensagens_processadas (mensagem_id, processada_em) "
                    "VALUES (gen_random_uuid(), :quando)"
                ),
                {"quando": agora - timedelta(days=dias)},
            )

    def processadas() -> int:
        with engine.connect() as conexao:
            return int(
                conexao.execute(
                    text("SELECT count(*) FROM mensagens_processadas")
                ).scalar_one()
            )

    with EmSegundoPlano(relay()), EmSegundoPlano(consumidor()):
        esperar_ate(lambda: len(outbox()) == 2)
        esperar_ate(lambda: processadas() == 1)

    assert sorted(linha["status"] for linha in outbox()) == ["dead", "entregue"]
    # Varias voltas do relay sem nada a publicar: nenhum span de laco ocioso.
    assert spans.get_finished_spans() == ()


def test_dead_sai_em_30_dias_com_o_texto_livre_do_envelope(engine: Engine) -> None:
    with engine.begin() as conexao:
        for dias in (31, 29):
            conexao.execute(
                text(
                    "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
                    "routing_key, envelope, status, criado_em) VALUES "
                    "(gen_random_uuid(), 'DiagnosticoConcluido', gen_random_uuid(), "
                    "'pytstop.eventos', 'evento.execucao.diagnostico_concluido', "
                    "'{}', 'dead', now() - make_interval(days => :dias))"
                ),
                {"dias": dias},
            )
    assert Outbox(engine).limpar() == 1
    with engine.connect() as conexao:
        restantes = conexao.execute(
            text("SELECT now() - criado_em < interval '30 days' FROM outbox")
        ).scalars()
        assert list(restantes) == [True]


def test_limpeza_das_processadas_tambem_apaga_em_lotes(
    broker: Broker,
    engine: Engine,
    consumidor: Callable[..., Consumidor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo_consumidor, "LOTE_DA_LIMPEZA", 2)
    with engine.begin() as conexao:
        for _ in range(5):
            conexao.execute(
                text(
                    "INSERT INTO mensagens_processadas (mensagem_id, processada_em) "
                    "VALUES (gen_random_uuid(), now() - interval '31 days')"
                )
            )
    apagamentos: list[str] = []

    def contar(_conn: object, _cursor: object, sql: str, *_: object) -> None:
        if sql.startswith("DELETE FROM mensagens_processadas"):
            apagamentos.append(sql)

    event.listen(engine, "before_cursor_execute", contar)
    try:
        with EmSegundoPlano(consumidor()):
            esperar_ate(lambda: len(apagamentos) == 3)
    finally:
        event.remove(engine, "before_cursor_execute", contar)
    with engine.connect() as conexao:
        restantes = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas")
        ).scalar_one()
    assert restantes == 0


def test_limpeza_apaga_em_lotes_de_transacao_curta(engine: Engine) -> None:
    with engine.begin() as conexao:
        for _ in range(5):
            conexao.execute(
                text(
                    "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
                    "routing_key, envelope, status, entregue_em) VALUES "
                    "(gen_random_uuid(), 'ReservaLiberada', gen_random_uuid(), "
                    "'pytstop.eventos', 'evento.execucao.reserva_liberada', '{}', "
                    "'entregue', now() - interval '8 days')"
                )
            )
    apagamentos: list[str] = []

    def contar(_conn: object, _cursor: object, sql: str, *_: object) -> None:
        if sql.startswith("DELETE FROM outbox"):
            apagamentos.append(sql)

    event.listen(engine, "before_cursor_execute", contar)
    try:
        assert Outbox(engine).limpar(lote=2) == 5
    finally:
        event.remove(engine, "before_cursor_execute", contar)
    assert len(apagamentos) == 3  # 2 + 2 + 1, cada um na propria transacao
    assert _pendentes(engine) == 0


# --- coordenacao entre replicas, com o broker falso ---------------------------


class _BrokerFalso:
    """Faz o papel do ``_Broker``: a primeira conexao cai na primeira publicacao.

    ``ao_publicar`` roda a cada publicacao confirmada, na thread do relay.
    """

    conexoes = 0
    publicadas: ClassVar[list[str]] = []
    ao_publicar: ClassVar[list[Callable[[], None]]] = []
    bloqueada = False

    def __init__(self, _url: str) -> None:
        type(self).conexoes += 1
        self._numero = type(self).conexoes

    def publicar(self, linha: LinhaDaOutbox) -> str | None:
        if self._numero == 1:
            raise BrokerIndisponivelError
        type(self).publicadas.append(linha.envelope["id"])
        for acao in type(self).ao_publicar:
            acao()
        return None

    def manter_viva(self) -> None:
        pass

    def fechar(self) -> None:
        pass


@pytest.fixture
def broker_falso(monkeypatch: pytest.MonkeyPatch) -> type[_BrokerFalso]:
    monkeypatch.setattr(_BrokerFalso, "conexoes", 0)
    monkeypatch.setattr(_BrokerFalso, "publicadas", [])
    monkeypatch.setattr(_BrokerFalso, "ao_publicar", [])
    monkeypatch.setattr(_BrokerFalso, "bloqueada", False)
    monkeypatch.setattr(modulo_relay, "_Broker", _BrokerFalso)
    return _BrokerFalso


class _BackoffAteLiberar(Backoff):
    """Reconexao so quando o teste liberar: a janela entre a queda e a volta.

    O ``Backoff`` real sorteia a espera entre 0 e o atraso (full jitter), e uma
    espera curta fecharia a janela antes de o teste conferir as linhas.
    """

    def __init__(self) -> None:
        super().__init__()
        self.esperando = threading.Event()
        self.liberado = threading.Event()

    def esperar(self, parar: threading.Event) -> None:
        self.esperando.set()
        while not self.liberado.is_set() and not parar.wait(0.05):
            pass


def _relay_falso(
    engine: Engine, sinais: Callable[[str], SinaisDoProcesso], nome: str = "relay"
) -> Relay:
    return Relay(
        engine, _URL_FALSA, sinais(nome), poll_s=0.05, backoff=Backoff(0.05, 0.1)
    )


def test_queda_do_broker_no_meio_do_lote_devolve_as_linhas_sem_gastar_tentativa(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
    tmp_path: Path,
) -> None:
    for _ in range(3):
        _gravar(engine)
    backoff = _BackoffAteLiberar()
    relay = Relay(engine, _URL_FALSA, sinais("relay"), poll_s=0.1, backoff=backoff)
    processo = EmSegundoPlano(relay)
    # Parada pedida no meio do lote: o relay termina o lote e nao pega outro.
    broker_falso.ao_publicar.append(processo.parar.set)
    with processo:
        # No backoff, a queda ja devolveu as linhas e tirou a prontidao.
        esperar_ate(backoff.esperando.is_set)
        assert not (tmp_path / "relay-pronto").exists()
        assert broker_falso.conexoes == 1
        with engine.connect() as conexao:
            devolvidas = conexao.execute(
                text(
                    "SELECT count(*) FROM outbox WHERE status = 'pendente' "
                    "AND tentativas = 0 AND proxima_tentativa_em <= now()"
                )
            ).scalar_one()
        # Lease devolvido e nenhuma tentativa gasta: valem de novo ja.
        assert devolvidas == 3
        backoff.liberado.set()
        esperar_ate(lambda: len(broker_falso.publicadas) == 3)

    assert [(linha["status"], linha["tentativas"]) for linha in outbox()] == [
        ("entregue", 0)
    ] * 3


def test_claim_segura_o_lote_inteiro_com_o_lease(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
) -> None:
    for _ in range(3):
        _gravar(engine)
    broker_falso.conexoes = 1  # a primeira conexao ja caiu: esta publica
    com_lease: list[int] = []

    def contar_com_lease() -> None:
        with engine.connect() as conexao:
            com_lease.append(
                conexao.execute(
                    text(
                        "SELECT count(*) FROM outbox WHERE status = 'pendente' AND "
                        "proxima_tentativa_em > now() + interval '30 seconds'"
                    )
                ).scalar_one()
            )

    broker_falso.ao_publicar.append(contar_com_lease)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: len(broker_falso.publicadas) == 3)
    # No primeiro publish o lote inteiro esta com o lease de 60 s (a linha em
    # voo renovada, as outras desde o claim): outra replica nao as reivindica.
    assert com_lease[0] == 3


def test_relay_atrasado_nao_publica_linha_que_outra_replica_ja_entregou(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
) -> None:
    _gravar(engine)
    segunda = _gravar(engine)
    broker_falso.conexoes = 1

    def outra_replica_entrega_a_segunda() -> None:
        with engine.begin() as conexao:
            conexao.execute(
                text(
                    "UPDATE outbox SET status = 'entregue', entregue_em = now() "
                    "WHERE mensagem_id = :id"
                ),
                {"id": segunda},
            )

    broker_falso.ao_publicar.append(outra_replica_entrega_a_segunda)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: not _pendentes(engine))
    assert segunda not in broker_falso.publicadas
    assert len(broker_falso.publicadas) == 1


def test_replica_que_perdeu_o_lease_nao_sobrescreve_a_outra(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
    log_capturado: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Duas replicas de verdade, lease curto. A publica devagar (broker mudo): o
    # lease vence, B reivindica, publica e marca entregue. A volta depois e nao
    # grava nada por cima: a linha so muda pela replica com o lease vigente.
    monkeypatch.setattr(modulo_relay, "_LEASE", timedelta(milliseconds=300))
    mensagem = _gravar(engine)
    broker_falso.conexoes = 1
    a_publicando, b_entregou = threading.Event(), threading.Event()

    def a_espera_b() -> None:
        if not a_publicando.is_set():
            a_publicando.set()
            assert b_entregou.wait(15)

    broker_falso.ao_publicar.append(a_espera_b)
    with EmSegundoPlano(_relay_falso(engine, sinais, "a")):
        assert a_publicando.wait(15)
        with EmSegundoPlano(_relay_falso(engine, sinais, "b")):
            esperar_ate(lambda: outbox()[0]["status"] == "entregue")
            b_entregou.set()
        esperar_ate(
            lambda: (
                "message_published_after_losing_the_lease" in log_capturado.getvalue()
            )
        )

    assert broker_falso.publicadas == [mensagem, mensagem]  # pelo menos uma vez
    (linha,) = outbox()
    assert (linha["status"], linha["tentativas"]) == ("entregue", 0)


def test_linha_presa_no_claim_de_outra_replica_nao_segura_as_demais(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
    log_capturado: io.StringIO,
) -> None:
    # Outra replica no meio do claim trava a primeira linha (FOR UPDATE): o
    # claim desta pula a linha (SKIP LOCKED) em vez de esperar o lock_timeout.
    primeira = _gravar(engine)
    for _ in range(2):
        _gravar(engine)
    broker_falso.conexoes = 1
    with engine.connect() as outra:
        transacao = outra.begin()
        outra.execute(
            text("SELECT 1 FROM outbox WHERE mensagem_id = :id FOR UPDATE"),
            {"id": primeira},
        )
        with EmSegundoPlano(_relay_falso(engine, sinais)):
            esperar_ate(lambda: len(broker_falso.publicadas) == 2, prazo_s=4)
            assert outbox()[0]["status"] == "pendente"
            transacao.rollback()
            esperar_ate(lambda: len(broker_falso.publicadas) == 3)
    assert broker_falso.publicadas[-1] == primeira
    assert "relay_dependency_unavailable" not in log_capturado.getvalue()


def test_duas_replicas_publicam_cada_linha_uma_vez(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
) -> None:
    total = 40
    for _ in range(total):
        _gravar(engine)
    broker_falso.conexoes = 1
    with (
        EmSegundoPlano(_relay_falso(engine, sinais, "a")),
        EmSegundoPlano(_relay_falso(engine, sinais, "b")),
    ):
        esperar_ate(lambda: not _pendentes(engine))
    assert len(broker_falso.publicadas) == len(set(broker_falso.publicadas)) == total


def test_linha_nova_da_mesma_ordem_espera_a_anterior_em_backoff(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_falso.conexoes = 1
    ordem = uuid4()
    primeira = _gravar(engine, ordem_id=ordem, daqui_a=timedelta(hours=1))
    segunda = _gravar(engine, ordem_id=ordem)
    reivindicar = Outbox.reivindicar
    voltas: list[int] = []

    def contando(self: Outbox, lote: int, lease: timedelta) -> Any:
        voltas.append(1)
        return reivindicar(self, lote, lease)

    monkeypatch.setattr(Outbox, "reivindicar", contando)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        # Janela negativa medida em voltas do relay, nao em segundos: a segunda
        # linha nao passa a frente da primeira, que esta em backoff.
        esperar_ate(lambda: len(voltas) >= 5)
        assert broker_falso.publicadas == []
        with engine.begin() as conexao:
            conexao.execute(text("UPDATE outbox SET proxima_tentativa_em = now()"))
        esperar_ate(lambda: len(broker_falso.publicadas) == 2)
    assert broker_falso.publicadas == [primeira, segunda]


def test_linhas_de_ordens_diferentes_saem_em_ordem_de_gravacao(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
) -> None:
    broker_falso.conexoes = 1
    esperadas = [_gravar(engine) for _ in range(5)]
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: len(broker_falso.publicadas) == 5)
    assert broker_falso.publicadas == esperadas


def test_conexao_bloqueada_pelo_broker_para_os_claims_ate_o_desbloqueio(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_falso.conexoes = 1
    broker_falso.bloqueada = True  # Connection.Blocked: alarme de memoria
    _gravar(engine)
    reivindicar = Outbox.reivindicar
    voltas: list[int] = []
    esperas: list[int] = []

    def contando(self: Outbox, lote: int, lease: timedelta) -> Any:
        voltas.append(1)
        return reivindicar(self, lote, lease)

    def manter_viva(_self: _BrokerFalso) -> None:
        esperas.append(1)

    monkeypatch.setattr(Outbox, "reivindicar", contando)
    monkeypatch.setattr(_BrokerFalso, "manter_viva", manter_viva)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: len(esperas) >= 5)
        assert (voltas, broker_falso.publicadas) == ([], [])
        assert not (tmp_path / "relay-pronto").exists()
        broker_falso.bloqueada = False  # Connection.Unblocked
        esperar_ate(lambda: len(broker_falso.publicadas) == 1)
        esperar_ate((tmp_path / "relay-pronto").exists)


def test_envelope_fora_do_contrato_vira_dead_sem_derrubar_o_relay(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
) -> None:
    broker_falso.conexoes = 1
    _gravar(engine, envelope={})  # editado a mao ou defeito: nenhuma tentativa conserta
    valida = _gravar(engine)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: broker_falso.publicadas == [valida])
    venenosa = outbox()[0]
    assert (venenosa["status"], venenosa["ultimo_erro"]) == (
        "dead",
        "envelope fora do contrato",
    )


def test_falha_inesperada_na_publicacao_conta_tentativa_e_o_relay_segue(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_falso.conexoes = 1
    quebrada = _gravar(engine)
    valida = _gravar(engine)
    publicar = _BrokerFalso.publicar

    def falha_na_primeira(self: _BrokerFalso, linha: LinhaDaOutbox) -> str | None:
        if linha.envelope["id"] == quebrada:
            raise KeyError("propriedade")
        return publicar(self, linha)

    monkeypatch.setattr(_BrokerFalso, "publicar", falha_na_primeira)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: broker_falso.publicadas == [valida])
        esperar_ate(lambda: outbox()[0]["tentativas"] == 1)
    assert (outbox()[0]["status"], outbox()[0]["ultimo_erro"]) == (
        "pendente",
        "falha ao publicar (KeyError)",
    )


def test_metricas_do_relay_contam_publicadas_e_pendentes(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
) -> None:
    broker_falso.conexoes = 1
    chave = {"tipo": "ReservaLiberada"}
    antes = REGISTRY.get_sample_value("pytstop_mensagens_publicadas_total", chave) or 0
    for _ in range(2):
        _gravar(engine, daqui_a=timedelta(hours=1))  # seguem pendentes
    relay = _relay_falso(engine, sinais)
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
                "routing_key, envelope, status) VALUES (gen_random_uuid(), "
                "'ReservaLiberada', gen_random_uuid(), 'pytstop.eventos', "
                "'evento.execucao.reserva_liberada', '{}', 'dead')"
            )
        )
    assert REGISTRY.get_sample_value("outbox_pendentes") == 2
    assert REGISTRY.get_sample_value("outbox_dead") == 1
    for _ in range(3):
        _gravar(engine)
    with EmSegundoPlano(relay):
        esperar_ate(lambda: len(broker_falso.publicadas) == 3)
    depois = REGISTRY.get_sample_value("pytstop_mensagens_publicadas_total", chave)
    assert depois == antes + 3


def test_encerramento_gracioso_tira_a_prontidao_e_mantem_o_heartbeat(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
    tmp_path: Path,
) -> None:
    broker_falso.conexoes = 1
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate((tmp_path / "relay-pronto").exists)
    assert not (tmp_path / "relay-pronto").exists()
    assert (tmp_path / "relay-heartbeat").exists()


def test_heartbeat_bate_a_cada_lote_de_um_dreno_longo(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    broker_falso: type[_BrokerFalso],
    tmp_path: Path,
) -> None:
    for _ in range(25):  # 3 lotes de ate 10 linhas
        _gravar(engine)
    broker_falso.conexoes = 1
    batimento = tmp_path / "relay-heartbeat"
    existia: list[bool] = []

    def apaga_e_registra() -> None:
        existia.append(batimento.exists())
        batimento.unlink(missing_ok=True)

    broker_falso.ao_publicar.append(apaga_e_registra)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: len(broker_falso.publicadas) == 25)
    assert existia[0::10] == [True, True, True]  # 1a linha de cada lote


def test_bloqueio_no_meio_do_lote_devolve_as_linhas_que_sobraram(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
) -> None:
    for _ in range(3):
        _gravar(engine)
    broker_falso.conexoes = 1

    def bloqueia_depois_da_primeira() -> None:
        broker_falso.bloqueada = True

    broker_falso.ao_publicar.append(bloqueia_depois_da_primeira)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: len(broker_falso.publicadas) == 1)
        # As duas que sobraram voltam a valer ja, sem tentativa.
        esperar_ate(
            lambda: (
                [(r["status"], r["tentativas"]) for r in outbox()][1:]
                == [("pendente", 0), ("pendente", 0)]
            )
        )
        with engine.connect() as conexao:
            vencidas = conexao.execute(
                text(
                    "SELECT count(*) FROM outbox WHERE status = 'pendente' "
                    "AND proxima_tentativa_em <= now()"
                )
            ).scalar_one()
        assert vencidas == 2
        broker_falso.ao_publicar.clear()
        broker_falso.bloqueada = False
        esperar_ate(lambda: len(broker_falso.publicadas) == 3)


def test_falha_registrada_depois_de_perder_a_linha_nao_grava_nada(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
    log_capturado: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A publicacao falhou, mas o lease venceu nesse meio tempo e outra replica
    # reivindicou a linha: a tentativa nao e contada por cima dela.
    broker_falso.conexoes = 1
    _gravar(engine)

    def outra_replica_pega_e_falha(
        _self: _BrokerFalso, linha: LinhaDaOutbox
    ) -> str | None:
        with engine.begin() as conexao:
            conexao.execute(
                text(
                    "UPDATE outbox SET proxima_tentativa_em = now() + interval '1 hour'"
                )
            )
        return "UnroutableError"

    monkeypatch.setattr(_BrokerFalso, "publicar", outra_replica_pega_e_falha)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(
            lambda: (
                "message_publish_failed_after_losing_the_lease"
                in log_capturado.getvalue()
            )
        )
    (linha,) = outbox()
    assert (linha["status"], linha["tentativas"], linha["ultimo_erro"]) == (
        "pendente",
        0,
        None,
    )


def test_dead_de_envelope_invalido_tambem_respeita_o_fencing(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
    log_capturado: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_falso.conexoes = 1
    _gravar(engine, envelope={})

    def outra_replica_pega_no_meio(_envelope: object) -> None:
        with engine.begin() as conexao:
            conexao.execute(
                text(
                    "UPDATE outbox SET proxima_tentativa_em = now() + interval '1 hour'"
                )
            )
        msg = "envelope fora do contrato"
        raise MensagemInvalidaError(msg)

    monkeypatch.setattr(modulo_relay, "validar", outra_replica_pega_no_meio)
    voltas = _contar_claims(monkeypatch)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        esperar_ate(lambda: len(voltas) >= 3)
    assert outbox()[0]["status"] == "pendente"
    assert "message_dead" not in log_capturado.getvalue()


def _contar_claims(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    reivindicar = Outbox.reivindicar
    voltas: list[int] = []

    def contando(self: Outbox, lote: int, lease: timedelta) -> Any:
        voltas.append(1)
        return reivindicar(self, lote, lease)

    monkeypatch.setattr(Outbox, "reivindicar", contando)
    return voltas


def test_banco_fora_ao_devolver_e_ao_limpar_nao_derruba_o_relay(
    engine: Engine,
    sinais: Callable[[str], SinaisDoProcesso],
    outbox: Callable[[], list[dict[str, Any]]],
    broker_falso: type[_BrokerFalso],
    log_capturado: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sem conseguir devolver o lease, as linhas voltam quando ele vencer; sem
    # conseguir limpar, a limpeza fica para a proxima janela. O relay segue.
    for _ in range(2):
        _gravar(engine)

    def banco_fora(*_: object, **__: object) -> int:
        raise OperationalError("UPDATE", {}, Exception("banco fora"))

    monkeypatch.setattr(Outbox, "liberar", banco_fora)
    monkeypatch.setattr(Outbox, "limpar", banco_fora)
    with EmSegundoPlano(_relay_falso(engine, sinais)):
        # A primeira conexao cai no primeiro publish: a devolucao falha.
        esperar_ate(lambda: "outbox_lease_return_failed" in log_capturado.getvalue())
        esperar_ate(lambda: "outbox_cleanup_failed" in log_capturado.getvalue())
        with engine.begin() as conexao:
            conexao.execute(text("UPDATE outbox SET proxima_tentativa_em = now()"))
        esperar_ate(lambda: len(broker_falso.publicadas) == 2)
    assert [linha["status"] for linha in outbox()] == ["entregue", "entregue"]
