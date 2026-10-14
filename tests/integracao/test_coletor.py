"""Gauges do coletor da API contra o PostgreSQL das migracoes.

A fixture e conhecida: cada caso grava as linhas com a idade exata que quer, no
relogio do proprio banco (o que o coletor le), e confere o valor que o coletor
devolve na raspagem. As linhas de diagnostico e de reserva saem dos mapeamentos
dos contextos, como o servico as grava: renomear uma chave la reprova aqui.

O banco que falha (cala com o socket aberto, atende devagar, volta) e um proxy TCP
na frente dele, ``proxy_do_banco.py``.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import text

from src.coletor import JANELA_DE_NEGOCIO, PRAZO_DA_RASPAGEM_S, ColetorDaApi
from src.compartilhado.infraestrutura.database import (
    abrir_conexao_de_metricas,
    criar_engine,
)
from src.diagnostico.dominio.diagnostico import ItemDiagnostico, TipoItem
from src.diagnostico.infraestrutura.mapping import _itens_para_json
from src.estoque.dominio.reserva import Faltante, StatusReserva
from src.estoque.infraestrutura.mapping import _faltantes_para_json
from src.execucao.dominio.execucao import StatusExecucao
from src.main import criar_app
from tests.conftest import provedor_dos_testes
from tests.integracao.broker import esperar_ate
from tests.integracao.proxy_do_banco import ProxyDoBanco

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from sqlalchemy import Connection, Engine

DIA = timedelta(days=1)
IDADE = "pytstop_outbox_mais_antiga_segundos"
DURACAO = "pytstop_execucao_duracao_media_segundos"
FALTA = "pytstop_pecas_em_falta"
TODAS = {IDADE, DURACAO, FALTA}
THREAD_DO_COLETOR = "coletor-da-api"
NOME_NO_BANCO = "pytstop-execucao-metricas"


def _threads_do_coletor() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == THREAD_DO_COLETOR]


def _esperar_a_thread_do_coletor() -> None:
    """A raspagem devolve assim que a ultima consulta volta; a thread ainda fecha a
    conexao."""
    try:
        esperar_ate(lambda: not _threads_do_coletor(), prazo_s=10)
    except AssertionError:
        pytest.fail("a thread do coletor nao terminou")


@pytest.fixture(autouse=True)
def _nenhuma_thread_do_coletor_sobra() -> Iterator[None]:
    """A thread que a raspagem deixa para tras nao pode vazar para o teste seguinte."""
    yield
    _esperar_a_thread_do_coletor()


@pytest.fixture
def abridor(engine: Engine) -> Callable[[], Any]:
    """Como o app abre a conexao da raspagem: o engine so empresta a URL e o dialeto."""
    return partial(abrir_conexao_de_metricas, engine)


@pytest.fixture
def proxy_do_banco(database_url: str) -> Iterator[ProxyDoBanco]:
    with ProxyDoBanco.para(database_url) as proxy:
        yield proxy


@pytest.fixture
def via_proxy(
    database_url: str, proxy_do_banco: ProxyDoBanco
) -> Iterator[Callable[[], Any]]:
    """O mesmo, com o proxy no caminho do banco."""
    atras_do_proxy = criar_engine(proxy_do_banco.url_para(database_url))
    yield partial(abrir_conexao_de_metricas, atras_do_proxy)
    atras_do_proxy.dispose()


def _coletar(
    abrir_conexao: Callable[[], Any],
) -> dict[str, dict[tuple[str, ...], float]]:
    """O que o coletor devolve: familia -> valores dos rotulos -> valor."""
    return {
        familia.name: {
            tuple(amostra.labels.values()): amostra.value for amostra in familia.samples
        }
        for familia in ColetorDaApi(abrir_conexao).collect()
    }


def _nomes(abrir_conexao: Callable[[], Any], *, prazo_s: float) -> set[str]:
    return {f.name for f in ColetorDaApi(abrir_conexao, prazo_s=prazo_s).collect()}


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
    engine: Engine, abridor: Callable[[], Any]
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

    medias = _coletar(abridor)[DURACAO]

    assert medias == {
        ("SRV-A",): pytest.approx(200.0),
        ("SRV-B",): pytest.approx(450.0),
    }


def test_pecas_em_falta_conta_as_reservas_recusadas_da_janela_por_sku(
    engine: Engine, abridor: Callable[[], Any]
) -> None:
    recusada = StatusReserva.RECUSADA
    with engine.begin() as conexao:
        _reserva(conexao, recusada, ["PEC-A", "PEC-B"], DIA)
        _reserva(conexao, recusada, ["PEC-A"], 2 * DIA)
        _reserva(conexao, recusada, ["PEC-C"], JANELA_DE_NEGOCIO - timedelta(hours=1))
        # Fora da janela e as que nao foram recusadas nao contam.
        _reserva(
            conexao,
            recusada,
            ["PEC-A", "PEC-D"],
            JANELA_DE_NEGOCIO + timedelta(hours=1),
        )
        _reserva(conexao, StatusReserva.ATIVA, [], DIA)
        _reserva(conexao, StatusReserva.LIBERADA, [], DIA)

    faltas = _coletar(abridor)[FALTA]

    assert faltas == {("PEC-A",): 2.0, ("PEC-B",): 1.0, ("PEC-C",): 1.0}


def test_so_a_execucao_finalizada_e_so_a_reserva_recusada_entram(
    engine: Engine, abridor: Callable[[], Any]
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

    coletado = _coletar(abridor)

    assert coletado[DURACAO] == {}
    assert coletado[FALTA] == {}


def test_linhas_incoerentes_ficam_de_fora_e_nao_derrubam_a_raspagem(
    engine: Engine, abridor: Callable[[], Any]
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

    coletado = _coletar(abridor)

    assert set(coletado) == TODAS
    assert coletado[DURACAO] == {("SRV-A",): pytest.approx(100.0)}
    assert coletado[FALTA] == {("PEC-A",): 1.0}


def test_sem_dado_na_janela_as_familias_saem_sem_serie(
    abridor: Callable[[], Any],
) -> None:
    coletado = _coletar(abridor)

    assert coletado[DURACAO] == {}
    assert coletado[FALTA] == {}


def test_idade_da_outbox_e_a_da_linha_pendente_mais_antiga(
    engine: Engine, abridor: Callable[[], Any]
) -> None:
    with engine.begin() as conexao:
        _linha_da_outbox(conexao, "pendente", timedelta(seconds=120))
        _linha_da_outbox(conexao, "pendente", timedelta(seconds=10))
        # Entregue e dead, mesmo mais velhas, nao contam.
        _linha_da_outbox(conexao, "entregue", 3 * DIA)
        _linha_da_outbox(conexao, "dead", 5 * DIA)

    (idade,) = _coletar(abridor)[IDADE].values()

    # Gravada e lida no relogio do banco: a idade nunca e menor que a gravada.
    assert 120 <= idade < 130


def test_idade_da_outbox_sem_pendente_e_zero(
    engine: Engine, abridor: Callable[[], Any]
) -> None:
    with engine.begin() as conexao:
        _linha_da_outbox(conexao, "entregue", 3 * DIA)

    assert _coletar(abridor)[IDADE] == {(): 0.0}


@pytest.mark.parametrize(
    ("tabela", "ausente"),
    [
        pytest.param("outbox", IDADE, id="outbox"),
        pytest.param("execucoes", DURACAO, id="execucoes"),
        pytest.param("reservas", FALTA, id="reservas"),
    ],
)
def test_tabela_travada_omite_so_o_gauge_dela(
    engine: Engine, abridor: Callable[[], Any], tabela: str, ausente: str
) -> None:
    with engine.connect() as trava, trava.begin():
        trava.execute(text(f"LOCK TABLE {tabela} IN ACCESS EXCLUSIVE MODE"))
        inicio = time.monotonic()
        coletado = _coletar(abridor)
        gasto = time.monotonic() - inicio

    # O statement_timeout de 2 s corta a espera pelo lock e as outras duas gauges
    # seguem na mesma conexao, em autocommit: se ele nao cortasse, a primeira
    # consulta seguraria a raspagem ate o prazo total e as outras duas nao viriam.
    assert gasto > 1.5
    assert set(coletado) == TODAS - {ausente}


def test_as_tres_tabelas_travadas_devolvem_no_prazo_total_e_nao_em_6_s(
    engine: Engine, abridor: Callable[[], Any]
) -> None:
    """Sozinhas, as tres esperas pelo lock somariam 3 x 2 s."""
    with engine.connect() as trava, trava.begin():
        trava.execute(
            text("LOCK TABLE outbox, execucoes, reservas IN ACCESS EXCLUSIVE MODE")
        )
        inicio = time.monotonic()
        familias = _nomes(abridor, prazo_s=3.0)
        gasto = time.monotonic() - inicio

    assert familias == set()
    assert 2.8 < gasto < 5


def test_a_consulta_que_nao_responde_no_prazo_fica_ausente_com_o_banco_vivo(
    engine: Engine,
    abridor: Callable[[], Any],
    monkeypatch: pytest.MonkeyPatch,
    log_capturado: Any,
) -> None:
    """Sem o ``statement_timeout`` (30 s aqui) so o prazo da raspagem a corta. A
    consulta presa na espera do lock segue no servidor ate a trava soltar, e a
    raspagem nao espera por ela: devolve no prazo, derruba o socket, e o servidor
    desfaz a espera."""
    monkeypatch.setattr(
        "src.compartilhado.infraestrutura.database._STATEMENT_TIMEOUT_DAS_METRICAS_MS",
        30_000,
    )
    with engine.connect() as trava, trava.begin():
        trava.execute(text("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE"))
        inicio = time.monotonic()
        coletado = _nomes(abridor, prazo_s=1.5)
        gasto = time.monotonic() - inicio

    assert gasto > 1.4
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


def test_banco_fora_deixa_os_gauges_ausentes_e_a_raspagem_de_pe() -> None:
    morto = criar_engine("postgresql://x:y@127.0.0.1:1/nada")  # gitleaks:allow
    coletor = ColetorDaApi(partial(abrir_conexao_de_metricas, morto))

    assert list(coletor.collect()) == []
    assert {f.name for f in coletor.describe()} == TODAS
    morto.dispose()


def test_a_conexao_da_raspagem_leva_o_nome_e_o_prazo_das_consultas(
    abridor: Callable[[], Any],
) -> None:
    """O que as opcoes da conexao fazem no servidor: o nome que a acha no
    ``pg_stat_activity`` e o ``statement_timeout`` de 2 s."""
    conexao = abridor()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SHOW application_name")
            nome = cursor.fetchone()[0]
            cursor.execute("SHOW statement_timeout")
            prazo = cursor.fetchone()[0]
    finally:
        conexao.close()

    assert (nome, prazo) == (NOME_NO_BANCO, "2s")


@pytest.mark.parametrize("defeito", ["calar", "atrasar"])
def test_banco_mudo_ou_lento_na_conexao_responde_pelo_connect_timeout(
    proxy_do_banco: ProxyDoBanco, via_proxy: Callable[[], Any], defeito: str
) -> None:
    """O proxy aceita o TCP e nao entrega nada (ou entrega a 0,5 s o trecho): a
    conexao nao se completa. Sem ``connect_timeout`` o libpq esperaria para sempre,
    e com o do pool da API (3 s) a raspagem levaria 3 s."""
    if defeito == "calar":
        proxy_do_banco.calar()
    else:
        proxy_do_banco.atrasar(0.5)
    inicio = time.monotonic()

    familias = list(ColetorDaApi(via_proxy).collect())
    gasto = time.monotonic() - inicio

    assert familias == []
    assert 0.9 < gasto < 1.5


def test_primeira_conexao_cortada_e_banco_de_volta_a_seguinte_devolve_as_tres(
    proxy_do_banco: ProxyDoBanco, via_proxy: Callable[[], Any]
) -> None:
    """A primeira conexao do processo cala no primeiro comando (o pod sobe com o
    banco pendurado). Com um pool, os hooks de primeira conexao do engine
    consultam o banco sob um mutex dele, e toda raspagem seguinte esperava ali
    mesmo com o banco de volta; a conexao crua nao divide nada com a anterior."""
    proxy_do_banco.calar_no_comando(1)
    assert _nomes(via_proxy, prazo_s=1.0) == set()

    proxy_do_banco.calar_no_comando(None)  # o banco voltou
    inicio = time.monotonic()
    assert _nomes(via_proxy, prazo_s=PRAZO_DA_RASPAGEM_S) == TODAS
    assert time.monotonic() - inicio < PRAZO_DA_RASPAGEM_S


def test_conexao_que_cala_no_meio_da_raspagem_devolve_o_que_ja_veio_no_prazo(
    proxy_do_banco: ProxyDoBanco, via_proxy: Callable[[], Any]
) -> None:
    """A primeira consulta responde e a segunda nunca: o socket segue aberto (o
    kernel do outro lado ainda confirma o que recebe), entao nem o
    ``statement_timeout`` (a consulta nem chega ao servidor) nem o keepalive
    acodem. So o prazo da raspagem devolve, com a gauge que ja tinha chegado."""
    proxy_do_banco.calar_no_comando(2)

    inicio = time.monotonic()
    familias = _nomes(via_proxy, prazo_s=1.5)
    gasto = time.monotonic() - inicio

    assert familias == {IDADE}
    assert gasto > 1.4


def test_depois_do_prazo_o_socket_e_a_thread_da_raspagem_sao_soltos(
    proxy_do_banco: ProxyDoBanco, via_proxy: Callable[[], Any]
) -> None:
    """O proxy segue segurando a conexao calada (o caso em que nem keepalive nem
    ``tcp_user_timeout`` disparam); quem a fecha e o coletor, no prazo."""
    proxy_do_banco.calar_no_comando(1)

    assert _nomes(via_proxy, prazo_s=1.0) == set()

    esperar_ate(lambda: proxy_do_banco.abertas == 0 and not _threads_do_coletor(), 5)


def test_raspagens_cortadas_ao_mesmo_tempo_nao_acumulam_thread_nem_conexao(
    proxy_do_banco: ProxyDoBanco, via_proxy: Callable[[], Any]
) -> None:
    proxy_do_banco.calar_no_comando(1)

    with ThreadPoolExecutor(12) as raspadores:
        nomes = list(
            raspadores.map(lambda _: _nomes(via_proxy, prazo_s=1.0), range(12))
        )

    assert nomes == [set()] * 12
    esperar_ate(lambda: proxy_do_banco.abertas == 0 and not _threads_do_coletor(), 5)


def test_a_conexao_da_raspagem_aparece_no_pg_stat_activity_e_some_com_ela(
    engine: Engine, abridor: Callable[[], Any]
) -> None:
    """Sem pool a conexao fecha com a raspagem: nada fica aberto entre duas, para
    calar e prender a seguinte. E ela aparece, com o nome dela, enquanto roda."""
    contar = text(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name = :nome"
    )

    def abertas() -> int:
        # Uma transacao por leitura: o pg_stat_activity e um retrato por transacao.
        with engine.connect() as conexao:
            return int(conexao.execute(contar, {"nome": NOME_NO_BANCO}).scalar_one())

    assert abertas() == 0
    raspador = threading.Thread(target=_coletar, args=(abridor,))
    with engine.connect() as trava, trava.begin():
        trava.execute(text("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE"))
        raspador.start()
        # Presa na espera do lock da outbox, a conexao dela esta aberta.
        esperar_ate(lambda: abertas() == 1, 5)
    raspador.join(10)

    _esperar_a_thread_do_coletor()
    assert abertas() == 0


def test_raspagens_simultaneas_devolvem_as_tres_gauges(
    abridor: Callable[[], Any],
) -> None:
    coletor = ColetorDaApi(abridor)

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
    # O Prometheus do platform desiste aos 10 s (o scrape_timeout padrao).
    assert PRAZO_DA_RASPAGEM_S - 0.5 < gasto < 8
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
    # Sem o pool dela, a raspagem esperaria os 5 s do pool_timeout da API.
    assert gasto < 3


def test_coletor_sai_do_registro_com_o_app_e_o_proximo_app_registra_de_novo(
    ambiente_da_api: None,
) -> None:
    for _ in range(2):
        with TestClient(criar_app(provedor_dos_testes())):
            assert REGISTRY.get_sample_value(IDADE) == 0.0
        assert REGISTRY.get_sample_value(IDADE) is None
