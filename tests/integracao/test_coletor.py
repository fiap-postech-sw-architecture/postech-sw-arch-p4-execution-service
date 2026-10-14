"""Gauges por consulta da API contra o PostgreSQL das migracoes.

A fixture e conhecida: cada caso grava as linhas com a idade exata que quer, no
relogio do proprio banco (o que o coletor le), e confere o valor que o coletor
devolve na raspagem. As linhas de diagnostico e de reserva saem dos mapeamentos
dos contextos, como o servico as grava: renomear uma chave la reprova aqui.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import event, text

from src.coletor import JANELA_DE_NEGOCIO, PRAZO_DA_RASPAGEM_S, ColetorDaApi
from src.compartilhado.infraestrutura.database import criar_engine_de_metricas
from src.diagnostico.dominio.diagnostico import ItemDiagnostico, TipoItem
from src.diagnostico.infraestrutura.mapping import _itens_para_json
from src.estoque.dominio.reserva import Faltante, StatusReserva
from src.estoque.infraestrutura.mapping import _faltantes_para_json
from src.execucao.dominio.execucao import StatusExecucao
from src.main import criar_app
from tests.conftest import provedor_dos_testes
from tests.integracao.proxy_do_banco import ProxyDoBanco

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy import Connection, Engine

DIA = timedelta(days=1)
IDADE = "pytstop_outbox_mais_antiga_segundos"
DURACAO = "pytstop_execucao_duracao_media_segundos"
FALTA = "pytstop_pecas_em_falta"
TODAS = {IDADE, DURACAO, FALTA}
# Pode estar no meio de uma conexao ou de uma consulta quando o teste acaba.
THREAD_DO_COLETOR = "coletor-da-api"


def _esperar_a_thread_do_coletor() -> None:
    """A raspagem devolve assim que a ultima consulta volta; a thread ainda fecha a
    conexao. A que ficou presa num socket mudo morre quando o socket fecha."""
    limite = time.monotonic() + 10
    while any(t.name == THREAD_DO_COLETOR for t in threading.enumerate()):
        if time.monotonic() > limite:
            pytest.fail("a thread do coletor nao terminou")
        time.sleep(0.05)


@pytest.fixture(autouse=True)
def _nenhuma_thread_do_coletor_sobra() -> Iterator[None]:
    """A thread que a raspagem deixa para tras nao pode vazar para o teste seguinte."""
    yield
    _esperar_a_thread_do_coletor()


@pytest.fixture
def engine_de_metricas(database_url: str) -> Iterator[Engine]:
    """O engine que o app usa na raspagem."""
    metricas = criar_engine_de_metricas(database_url)
    yield metricas
    metricas.dispose()


@pytest.fixture
def proxy_do_banco(database_url: str) -> Iterator[ProxyDoBanco]:
    with ProxyDoBanco.para(database_url) as proxy:
        yield proxy


def _coletar(engine: Engine) -> dict[str, dict[tuple[str, ...], float]]:
    """O que o coletor devolve: familia -> valores dos rotulos -> valor."""
    return {
        familia.name: {
            tuple(amostra.labels.values()): amostra.value for amostra in familia.samples
        }
        for familia in ColetorDaApi(engine).collect()
    }


def _servico(codigo: str) -> ItemDiagnostico:
    return ItemDiagnostico(TipoItem.SERVICO, codigo, 1)


def _peca(codigo: str) -> ItemDiagnostico:
    return ItemDiagnostico(TipoItem.PECA, codigo, 1)


def _execucao(
    conexao: Connection,
    itens: Sequence[ItemDiagnostico] | None,
    *,
    finalizada_ha: timedelta | None,
    duracao: timedelta = timedelta(minutes=10),
    status: StatusExecucao = StatusExecucao.FINALIZADA,
    iniciada: bool = True,
    itens_json: list[dict[str, Any]] | None = None,
) -> None:
    """Diagnostico e execucao da mesma ordem; ``finalizada_ha`` nulo = em execucao.

    Os ``itens`` do dominio saem gravados pelo mapeamento do diagnostico. Para o
    dado que o dominio nao produz, ``itens_json`` leva o JSON cru.
    """
    ordem_id = uuid4()
    if itens_json is None:
        itens_json = _itens_para_json(tuple(itens or ()))
    conexao.execute(
        text(
            "INSERT INTO diagnosticos (ordem_id, status, itens, observacoes, "
            "solicitado_em) VALUES (:id, 'CONCLUIDO', CAST(:itens AS jsonb), '', now())"
        ),
        {"id": ordem_id, "itens": json.dumps(itens_json)},
    )
    finalizada_s = None if finalizada_ha is None else finalizada_ha.total_seconds()
    # Em execucao: iniciada ha 1 h. Finalizada: iniciada `duracao` antes do fim.
    iniciada_s = (
        3600.0 if finalizada_s is None else finalizada_s + duracao.total_seconds()
    )
    conexao.execute(
        text(
            "INSERT INTO execucoes (ordem_id, status, prioridade, enfileirada_em, "
            "mecanico_id, iniciada_em, finalizada_em) VALUES (:id, :status, "
            "'normal', now(), gen_random_uuid(), "
            "now() - make_interval(secs => :iniciada), "
            "now() - make_interval(secs => :finalizada))"
        ),
        {
            "id": ordem_id,
            "status": status.value,
            "iniciada": iniciada_s if iniciada else None,
            "finalizada": finalizada_s,
        },
    )


def _reserva(
    conexao: Connection,
    status: StatusReserva,
    faltantes: Sequence[str],
    criada_ha: timedelta,
    *,
    faltantes_json: list[dict[str, Any]] | None = None,
) -> None:
    if faltantes_json is None:
        faltantes_json = _faltantes_para_json(
            tuple(Faltante(sku, solicitado=2, disponivel=0) for sku in faltantes)
        )
    conexao.execute(
        text(
            "INSERT INTO reservas (id, ordem_id, status, itens, faltantes, "
            "criada_em) VALUES (gen_random_uuid(), :ordem, :status, '[]', "
            "CAST(:faltantes AS jsonb), "
            "now() - make_interval(secs => :criada))"
        ),
        {
            "ordem": uuid4(),
            "status": status.value,
            "faltantes": json.dumps(faltantes_json),
            "criada": criada_ha.total_seconds(),
        },
    )


def _linha_da_outbox(conexao: Connection, status: str, criada_ha: timedelta) -> None:
    conexao.execute(
        text(
            "INSERT INTO outbox (mensagem_id, tipo, correlation_id, exchange, "
            "routing_key, envelope, status, criado_em) VALUES (gen_random_uuid(), "
            "'ReservaLiberada', gen_random_uuid(), 'pytstop.eventos', "
            "'evento.execucao.reserva_liberada', '{}', :status, "
            "now() - make_interval(secs => :criado))"
        ),
        {"status": status, "criado": criada_ha.total_seconds()},
    )


def test_janela_de_negocio_e_de_sete_dias() -> None:
    assert JANELA_DE_NEGOCIO == 7 * DIA


def test_prazo_da_raspagem_cabe_no_scrape_timeout_do_prometheus() -> None:
    # O Prometheus do platform espera 10 s: a folga e do resto do /metrics.
    assert PRAZO_DA_RASPAGEM_S <= 5


def test_duracao_media_por_servico_conta_so_as_finalizadas_da_janela(
    engine: Engine, engine_de_metricas: Engine
) -> None:
    with engine.begin() as conexao:
        # SRV-A: 100 s e 300 s. SRV-B: 300 s e 600 s (a peca nao entra).
        _execucao(
            conexao,
            [_servico("SRV-A")],
            finalizada_ha=DIA,
            duracao=timedelta(seconds=100),
        )
        _execucao(
            conexao,
            [_servico("SRV-A"), _servico("SRV-B"), _peca("P")],
            finalizada_ha=JANELA_DE_NEGOCIO - timedelta(hours=1),
            duracao=timedelta(seconds=300),
        )
        # O mesmo servico em dois itens conta uma vez para a execucao.
        _execucao(
            conexao,
            [_servico("SRV-B"), _servico("SRV-B")],
            finalizada_ha=DIA,
            duracao=timedelta(seconds=600),
        )
        # Fora da janela, em execucao e cancelada nao contam.
        _execucao(
            conexao,
            [_servico("SRV-A")],
            finalizada_ha=JANELA_DE_NEGOCIO + timedelta(hours=1),
            duracao=timedelta(seconds=5000),
        )
        _execucao(
            conexao,
            [_servico("SRV-A")],
            finalizada_ha=None,
            status=StatusExecucao.EM_EXECUCAO,
        )
        _execucao(
            conexao,
            [_servico("SRV-A")],
            finalizada_ha=None,
            status=StatusExecucao.CANCELADA,
        )

    medias = _coletar(engine_de_metricas)[DURACAO]

    assert medias == {
        ("SRV-A",): pytest.approx(200.0),
        ("SRV-B",): pytest.approx(450.0),
    }


def test_pecas_em_falta_conta_as_reservas_recusadas_da_janela_por_sku(
    engine: Engine, engine_de_metricas: Engine
) -> None:
    recusada = StatusReserva.RECUSADA
    with engine.begin() as conexao:
        _reserva(conexao, recusada, ["PEC-A", "PEC-B"], DIA)
        _reserva(conexao, recusada, ["PEC-A"], 2 * DIA)
        _reserva(conexao, recusada, ["PEC-C"], JANELA_DE_NEGOCIO - timedelta(hours=1))
        # Fora da janela e as que nao foram recusadas nao contam.
        _reserva(conexao, recusada, ["PEC-A", "PEC-D"], JANELA_DE_NEGOCIO + DIA)
        _reserva(conexao, StatusReserva.ATIVA, [], DIA)
        _reserva(conexao, StatusReserva.LIBERADA, [], DIA)

    faltas = _coletar(engine_de_metricas)[FALTA]

    assert faltas == {("PEC-A",): 2.0, ("PEC-B",): 1.0, ("PEC-C",): 1.0}


def test_so_a_execucao_finalizada_e_so_a_reserva_recusada_entram(
    engine: Engine, engine_de_metricas: Engine
) -> None:
    """O banco aceita uma execucao em curso com ``finalizada_em`` e uma reserva
    ativa com ``faltantes`` (so o CHECK do status existe): o dominio nao produz as
    duas, e o filtro de status e a defesa se um dia produzir."""
    with engine.begin() as conexao:
        _execucao(
            conexao,
            [_servico("SRV-I")],
            finalizada_ha=DIA,
            duracao=timedelta(seconds=100),
            status=StatusExecucao.EM_EXECUCAO,
        )
        _reserva(conexao, StatusReserva.ATIVA, ["PEC-I"], DIA)

    coletado = _coletar(engine_de_metricas)

    assert coletado[DURACAO] == {}
    assert coletado[FALTA] == {}


def test_linhas_incoerentes_ficam_de_fora_e_nao_derrubam_a_raspagem(
    engine: Engine, engine_de_metricas: Engine
) -> None:
    """Dado que o dominio nao produz mas o banco aceita (nao ha CHECK nem NOT
    NULL): sem o filtro, ``float(None)`` e o rotulo nulo derrubavam o /metrics."""
    with engine.begin() as conexao:
        _execucao(
            conexao,
            [_servico("SRV-A")],
            finalizada_ha=DIA,
            duracao=timedelta(seconds=100),
        )
        # Finalizada sem inicio: a media do servico dela daria NULL.
        _execucao(conexao, [_servico("SRV-X")], finalizada_ha=DIA, iniciada=False)
        # Item de servico sem codigo: o grupo do rotulo seria NULL.
        _execucao(
            conexao,
            None,
            itens_json=[{"tipo": "servico", "quantidade": 1}],
            finalizada_ha=DIA,
            duracao=timedelta(seconds=900),
        )
        _reserva(conexao, StatusReserva.RECUSADA, ["PEC-A"], DIA)
        # Faltante sem sku.
        _reserva(
            conexao,
            StatusReserva.RECUSADA,
            [],
            DIA,
            faltantes_json=[{"solicitado": 2, "disponivel": 0}],
        )

    coletado = _coletar(engine_de_metricas)

    assert set(coletado) == TODAS
    assert coletado[DURACAO] == {("SRV-A",): pytest.approx(100.0)}
    assert coletado[FALTA] == {("PEC-A",): 1.0}


def test_sem_dado_na_janela_as_familias_saem_sem_serie(
    engine_de_metricas: Engine,
) -> None:
    coletado = _coletar(engine_de_metricas)

    assert coletado[DURACAO] == {}
    assert coletado[FALTA] == {}


def test_idade_da_outbox_e_a_da_linha_pendente_mais_antiga(
    engine: Engine, engine_de_metricas: Engine
) -> None:
    with engine.begin() as conexao:
        _linha_da_outbox(conexao, "pendente", timedelta(seconds=120))
        _linha_da_outbox(conexao, "pendente", timedelta(seconds=10))
        # Entregue e dead, mesmo mais velhas, nao contam.
        _linha_da_outbox(conexao, "entregue", 3 * DIA)
        _linha_da_outbox(conexao, "dead", 5 * DIA)

    (idade,) = _coletar(engine_de_metricas)[IDADE].values()

    # Gravada e lida no relogio do banco: a idade nunca e menor que a gravada.
    assert 120 <= idade < 130


def test_idade_da_outbox_sem_pendente_e_zero(
    engine: Engine, engine_de_metricas: Engine
) -> None:
    with engine.begin() as conexao:
        _linha_da_outbox(conexao, "entregue", 3 * DIA)

    assert _coletar(engine_de_metricas)[IDADE] == {(): 0.0}


@pytest.mark.parametrize(
    ("tabela", "ausente"),
    [
        pytest.param("outbox", IDADE, id="outbox"),
        pytest.param("execucoes", DURACAO, id="execucoes"),
        pytest.param("reservas", FALTA, id="reservas"),
    ],
)
def test_tabela_travada_omite_so_o_gauge_dela_dentro_do_prazo(
    engine: Engine, engine_de_metricas: Engine, tabela: str, ausente: str
) -> None:
    with engine.connect() as trava, trava.begin():
        trava.execute(text(f"LOCK TABLE {tabela} IN ACCESS EXCLUSIVE MODE"))
        inicio = time.monotonic()
        coletado = _coletar(engine_de_metricas)
        gasto = time.monotonic() - inicio

    # O statement_timeout de 2 s corta a espera pelo lock e as outras duas gauges
    # seguem na mesma conexao: uma trava custa 2 s, nao a raspagem.
    assert 1.5 < gasto < 3.5
    assert set(coletado) == TODAS - {ausente}


def test_as_tres_tabelas_travadas_devolvem_no_prazo_total_e_nao_em_6_s(
    engine: Engine, engine_de_metricas: Engine
) -> None:
    """Sozinhas, as tres esperas pelo lock somariam 3 x 2 s."""
    with engine.connect() as trava, trava.begin():
        trava.execute(
            text("LOCK TABLE outbox, execucoes, reservas IN ACCESS EXCLUSIVE MODE")
        )
        inicio = time.monotonic()
        familias = list(ColetorDaApi(engine_de_metricas, prazo_s=3.0).collect())
        gasto = time.monotonic() - inicio

    assert familias == []
    assert 2.8 < gasto < 4.0


def test_a_consulta_que_nao_responde_no_prazo_fica_ausente_com_o_banco_vivo(
    engine: Engine,
    engine_de_metricas: Engine,
    monkeypatch: pytest.MonkeyPatch,
    log_capturado: Any,
) -> None:
    """Sem o ``statement_timeout`` (30 s aqui) so o prazo da raspagem a corta. A
    consulta presa na espera do lock segue no banco ate a trava soltar, e a
    raspagem nao espera por ela."""
    monkeypatch.setattr(
        "src.coletor._PRAZO_DA_CONSULTA",
        text("SET LOCAL statement_timeout = '30s'"),
    )
    with engine.connect() as trava, trava.begin():
        trava.execute(text("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE"))
        inicio = time.monotonic()
        coletado = _coletar_com_prazo(engine_de_metricas, prazo_s=1.5)
        gasto = time.monotonic() - inicio

    assert 1.4 < gasto < 2.5
    # A primeira consulta prendeu a sequencia: as outras duas nem rodaram.
    assert coletado == set()
    erros = {
        linha["gauge"]: linha["error"]
        for linha in map(json.loads, log_capturado.getvalue().splitlines())
        if linha.get("event") == "api_gauge_unavailable"
    }
    assert erros == {
        "outbox": "TimeoutError",
        "duracao": "TimeoutError",
        "falta": "TimeoutError",
    }


def _coletar_com_prazo(engine: Engine, *, prazo_s: float) -> set[str]:
    return {f.name for f in ColetorDaApi(engine, prazo_s=prazo_s).collect()}


def test_banco_fora_deixa_os_gauges_ausentes_e_a_raspagem_de_pe() -> None:
    morto = criar_engine_de_metricas(
        "postgresql://x:y@127.0.0.1:1/nada"
    )  # gitleaks:allow
    coletor = ColetorDaApi(morto)

    assert list(coletor.collect()) == []
    assert {f.name for f in coletor.describe()} == TODAS


@pytest.mark.parametrize("defeito", ["calar", "atrasar"])
def test_banco_mudo_ou_lento_na_conexao_responde_pelo_connect_timeout(
    database_url: str, proxy_do_banco: ProxyDoBanco, defeito: str
) -> None:
    """O proxy aceita o TCP e nao entrega nada (ou entrega a 0,5 s o trecho):
    a conexao nao se completa. Sem ``connect_timeout`` o libpq esperaria para
    sempre; com o do pool da API (3 s) a raspagem levaria 3 s."""
    if defeito == "calar":
        proxy_do_banco.calar()
    else:
        proxy_do_banco.atrasar(0.5)
    metricas = criar_engine_de_metricas(proxy_do_banco.url_para(database_url))
    inicio = time.monotonic()

    familias = list(ColetorDaApi(metricas).collect())
    gasto = time.monotonic() - inicio

    assert familias == []
    assert 0.9 < gasto < 2.5


def test_conexao_que_cala_no_meio_da_raspagem_devolve_no_prazo_total(
    database_url: str, proxy_do_banco: ProxyDoBanco
) -> None:
    """A conexao abre e a primeira consulta sai; o banco nunca responde, o socket
    segue aberto (o kernel do outro lado ainda confirma o que recebe): nem o
    ``statement_timeout`` (a consulta nem chega ao servidor) nem o keepalive
    acodem. So o prazo da raspagem devolve."""
    metricas = criar_engine_de_metricas(proxy_do_banco.url_para(database_url))

    @event.listens_for(metricas, "before_cursor_execute")
    def _calar_na_primeira_consulta(
        _conexao: object,
        _cursor: object,
        comando: str,
        *_resto: object,
    ) -> None:
        if "outbox" in comando:
            proxy_do_banco.calar()

    inicio = time.monotonic()
    familias = list(ColetorDaApi(metricas, prazo_s=1.5).collect())
    gasto = time.monotonic() - inicio

    assert familias == []
    assert 1.4 < gasto < 2.5
    # A thread presa no socket mudo nao segura o processo na saida (SIGTERM).
    (presa,) = [t for t in threading.enumerate() if t.name == THREAD_DO_COLETOR]
    assert presa.daemon


def test_depois_de_calar_a_raspagem_seguinte_conecta_do_zero(
    database_url: str, proxy_do_banco: ProxyDoBanco
) -> None:
    """A conexao presa no socket mudo nao prende a raspagem seguinte: sem pool, ela
    abre outra, e o banco que voltou (o proxy so calou a que existia) devolve as
    gauges, com a thread da anterior ainda presa."""
    metricas = criar_engine_de_metricas(proxy_do_banco.url_para(database_url))
    ligado = threading.Event()
    ligado.set()

    @event.listens_for(metricas, "before_cursor_execute")
    def _calar_a_primeira(
        _conexao: object, _cursor: object, comando: str, *_resto: object
    ) -> None:
        if "outbox" in comando and ligado.is_set():
            ligado.clear()
            proxy_do_banco.calar_abertas()

    assert list(ColetorDaApi(metricas, prazo_s=1.0).collect()) == []

    assert {f.name for f in ColetorDaApi(metricas).collect()} == TODAS


def test_raspagem_nao_deixa_conexao_aberta(
    engine: Engine, engine_de_metricas: Engine
) -> None:
    """Sem pool a conexao fecha com a raspagem: nada fica aberto entre duas, para
    calar e prender a seguinte."""
    contar = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE application_name = 'pytstop-execucao-metricas'"
    )

    def abertas() -> int:
        # Uma transacao por leitura: o pg_stat_activity e um retrato por transacao.
        with engine.connect() as conexao:
            return conexao.execute(contar).scalar_one()

    antes = abertas()
    _coletar(engine_de_metricas)
    _esperar_a_thread_do_coletor()
    depois = abertas()

    assert (antes, depois) == (0, 0)


def test_raspagens_simultaneas_devolvem_as_tres_gauges(
    engine_de_metricas: Engine,
) -> None:
    coletor = ColetorDaApi(engine_de_metricas)

    with ThreadPoolExecutor(8) as raspadores:
        nomes = list(
            raspadores.map(lambda _: {f.name for f in coletor.collect()}, range(24))
        )

    assert nomes == [TODAS] * 24


@pytest.fixture
def ambiente_da_api(
    database_url: str, jwks_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("JWKS_URL", jwks_url)
    monkeypatch.setenv("BILLING_URL", "http://billing.test")


@pytest.fixture
def api_com_dados_do_coletor(
    ambiente_da_api: None, engine: Engine
) -> Iterator[TestClient]:
    with engine.begin() as conexao:
        _linha_da_outbox(conexao, "pendente", timedelta(seconds=60))
        _execucao(
            conexao,
            [_servico("SRV-A")],
            finalizada_ha=DIA,
            duracao=timedelta(seconds=600),
        )
        _reserva(conexao, StatusReserva.RECUSADA, ["PEC-A"], DIA)
    with TestClient(criar_app(provedor_dos_testes())) as cliente:
        yield cliente


def test_metrics_da_api_traz_os_gauges_por_consulta(
    api_com_dados_do_coletor: TestClient,
) -> None:
    resposta = api_com_dados_do_coletor.get("/metrics")

    assert resposta.status_code == 200
    for familia in (IDADE, DURACAO, FALTA):
        assert f"# TYPE {familia} gauge" in resposta.text
    # Lidas no registro, como o Prometheus as le: depois do caso que as dispara.
    idade = REGISTRY.get_sample_value(IDADE)
    assert idade is not None
    assert 60 <= idade < 70
    assert REGISTRY.get_sample_value(DURACAO, {"servico": "SRV-A"}) == pytest.approx(
        600.0
    )
    assert REGISTRY.get_sample_value(FALTA, {"sku": "PEC-A"}) == 1.0


def test_metrics_da_api_com_o_banco_travado_responde_no_prazo_com_o_resto(
    api_com_dados_do_coletor: TestClient, engine: Engine
) -> None:
    """Tres tabelas travadas: as tres esperas somariam 6 s, e o /metrics responde no
    prazo da raspagem, sem as tres series e com todo o resto."""
    with engine.connect() as trava, trava.begin():
        trava.execute(
            text("LOCK TABLE outbox, execucoes, reservas IN ACCESS EXCLUSIVE MODE")
        )
        inicio = time.monotonic()
        resposta = api_com_dados_do_coletor.get("/metrics")
        gasto = time.monotonic() - inicio

    assert resposta.status_code == 200
    assert PRAZO_DA_RASPAGEM_S - 0.5 < gasto < PRAZO_DA_RASPAGEM_S + 1.5
    for familia in TODAS:
        assert familia not in resposta.text
    for resto in (
        "# TYPE http_request_duration_seconds histogram",
        "# TYPE pytstop_circuit_breaker_aberto gauge",
        "# TYPE pytstop_reconexoes_ao_broker_total counter",
    ):
        assert resto in resposta.text


def test_raspagem_nao_disputa_o_pool_das_requisicoes(
    api_com_dados_do_coletor: TestClient,
) -> None:
    # Pool de 5 mais 10 de excesso (os padroes): todo ocupado, como sob carga.
    engine_da_api = api_com_dados_do_coletor.app.state.session_factory.kw["bind"]
    conexoes = [engine_da_api.connect() for _ in range(15)]
    try:
        assert engine_da_api.pool.checkedout() == 15
        inicio = time.monotonic()
        resposta = api_com_dados_do_coletor.get("/metrics")
        gasto = time.monotonic() - inicio
    finally:
        for conexao in conexoes:
            conexao.close()

    assert f"\n{IDADE} " in resposta.text
    assert gasto < 1.5


def test_coletor_sai_do_registro_com_o_app_e_o_proximo_app_registra_de_novo(
    ambiente_da_api: None,
) -> None:
    for _ in range(2):
        with TestClient(criar_app(provedor_dos_testes())):
            assert REGISTRY.get_sample_value(IDADE) == 0.0
        assert REGISTRY.get_sample_value(IDADE) is None
