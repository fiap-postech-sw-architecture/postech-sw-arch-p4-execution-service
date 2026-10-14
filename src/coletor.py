"""Gauges por consulta da API: o que so o banco sabe, calculado na raspagem.

Registrado so na API (``src/main.py``): relay e consumidor nao leem estas
series, e a API segue exportando com os outros processos parados, que e o que
mantem de pe o alerta de outbox parada. Cada replica da API repete o mesmo
valor, e os paineis agregam com ``max``.

- ``pytstop_outbox_mais_antiga_segundos``: idade da linha pendente mais antiga
  da outbox (0 sem pendente);
- ``pytstop_execucao_duracao_media_segundos{servico}``: media de iniciada a
  finalizada das execucoes finalizadas nos ultimos 7 dias, uma vez por servico
  do diagnostico da ordem (a execucao com dois servicos entra na media dos dois,
  com a duracao inteira), RF-008;
- ``pytstop_pecas_em_falta{sku}``: reservas recusadas nos ultimos 7 dias em que o
  SKU faltou.

O coletor le as tabelas de tres contextos (diagnostico, execucao e estoque) e a
outbox de proposito: e leitura, sem repositorio. O formato que ele espera
(``itens[].tipo/codigo`` do diagnostico, ``faltantes[].sku`` da reserva) e o que
os mapeamentos desses contextos gravam, e os testes gravam as linhas por eles;
os status e o tipo do item vem dos enums dos contextos, como parametro.

Tempo da raspagem. As tres consultas rodam em sequencia, numa conexao so aberta
a cada raspagem (``criar_engine_de_metricas``: sem pool, ate 1 s para conectar),
cada uma na propria transacao e com ``statement_timeout`` de 2 s (que corta
tambem a espera por lock). Elas correm numa thread a parte, e ``collect`` espera
por elas no maximo ``PRAZO_DA_RASPAGEM_S`` no total. O prazo existe porque o
timeout do banco nao cobre tudo: com o socket aberto e o banco calado (o outro
lado da rede ou o processo parado, sem fechar a conexao) nenhum timeout do
driver dispara, e a raspagem esperaria para sempre. Passado o prazo o que nao
voltou fica de fora. A gauge sem resposta, qualquer que seja o motivo (banco
fora, tabela travada, prazo esgotado), sai ausente da raspagem, nunca em zero,
e o resto do ``/metrics`` segue.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import structlog
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from src.diagnostico.dominio.diagnostico import TipoItem
from src.estoque.dominio.reserva import StatusReserva
from src.execucao.dominio.execucao import StatusExecucao

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from sqlalchemy import Engine, Row, TextClause

_log = structlog.get_logger(__name__)

JANELA_DE_NEGOCIO: Final = timedelta(days=7)
_DIAS: Final = JANELA_DE_NEGOCIO.days
# Todas as consultas de uma raspagem juntas. O Prometheus do platform espera 10 s
# (o `scrape_timeout` padrao): a folga fica para o resto do /metrics.
PRAZO_DA_RASPAGEM_S: Final = 5.0

_IDADE_DA_OUTBOX: Final = text(
    "SELECT COALESCE(EXTRACT(EPOCH FROM clock_timestamp() - min(criado_em)), 0) "
    "FROM outbox WHERE status = 'pendente'"
)
# Uma linha por (execucao, servico): o DISTINCT evita contar duas vezes a
# execucao de um diagnostico que lista o mesmo servico em dois itens. As linhas
# sem o que a gauge precisa (execucao sem inicio, item sem codigo) ficam de fora:
# uma so nao pode derrubar a familia inteira.
_DURACAO_POR_SERVICO: Final = text(
    "SELECT s.codigo, avg(EXTRACT(EPOCH FROM e.finalizada_em - e.iniciada_em)) "
    "FROM execucoes e JOIN diagnosticos d ON d.ordem_id = e.ordem_id "
    "CROSS JOIN LATERAL ("
    "SELECT DISTINCT item ->> 'codigo' AS codigo "
    "FROM jsonb_array_elements(d.itens) AS item "
    "WHERE item ->> 'tipo' = :servico AND item ->> 'codigo' IS NOT NULL"
    ") s "
    "WHERE e.status = :finalizada AND e.iniciada_em IS NOT NULL "
    "AND e.finalizada_em >= now() - :janela "
    "GROUP BY s.codigo"
)
_FALTA_POR_SKU: Final = text(
    "SELECT falta ->> 'sku', count(*) FROM reservas r "
    "CROSS JOIN LATERAL jsonb_array_elements(r.faltantes) AS falta "
    "WHERE r.status = :recusada AND r.criada_em >= now() - :janela "
    "AND falta ->> 'sku' IS NOT NULL "
    "GROUP BY falta ->> 'sku'"
)
# O statement_timeout tambem corta a espera por um lock.
_PRAZO_DA_CONSULTA: Final = text("SET LOCAL statement_timeout = '2s'")


def _idade_da_outbox() -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_outbox_mais_antiga_segundos",
        "Idade da linha pendente mais antiga da outbox (0 sem pendente).",
    )


def _duracao_media() -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_execucao_duracao_media_segundos",
        f"Duracao media das execucoes finalizadas nos ultimos {_DIAS} dias, "
        "por servico.",
        labels=["servico"],
    )


def _pecas_em_falta() -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_pecas_em_falta",
        f"Reservas recusadas nos ultimos {_DIAS} dias em que o SKU faltou.",
        labels=["sku"],
    )


def _montar_idade(linhas: Sequence[Row[Any]]) -> GaugeMetricFamily:
    familia = _idade_da_outbox()
    familia.add_metric([], float(linhas[0][0]))
    return familia


def _montar_duracao(linhas: Sequence[Row[Any]]) -> GaugeMetricFamily:
    familia = _duracao_media()
    for servico, media in linhas:
        familia.add_metric([servico], float(media))
    return familia


def _montar_falta(linhas: Sequence[Row[Any]]) -> GaugeMetricFamily:
    familia = _pecas_em_falta()
    for sku, reservas in linhas:
        familia.add_metric([sku], float(reservas))
    return familia


class _Gauge(NamedTuple):
    nome: str
    consulta: TextClause
    parametros: Mapping[str, object]
    montar: Callable[[Sequence[Row[Any]]], GaugeMetricFamily]


_GAUGES: Final = (
    _Gauge("outbox", _IDADE_DA_OUTBOX, {}, _montar_idade),
    _Gauge(
        "duracao",
        _DURACAO_POR_SERVICO,
        {
            "janela": JANELA_DE_NEGOCIO,
            "finalizada": StatusExecucao.FINALIZADA.value,
            "servico": TipoItem.SERVICO.value,
        },
        _montar_duracao,
    ),
    _Gauge(
        "falta",
        _FALTA_POR_SKU,
        {"janela": JANELA_DE_NEGOCIO, "recusada": StatusReserva.RECUSADA.value},
        _montar_falta,
    ),
)


class ColetorDaApi:
    """Os tres gauges por consulta, lidos a cada raspagem do ``/metrics``."""

    def __init__(self, engine: Engine, *, prazo_s: float = PRAZO_DA_RASPAGEM_S) -> None:
        self._engine = engine
        self._prazo_s = prazo_s

    def describe(self) -> Iterator[GaugeMetricFamily]:
        """Os nomes, sem consultar o banco (o registro roda antes do boot terminar)."""
        yield _idade_da_outbox()
        yield _duracao_media()
        yield _pecas_em_falta()

    def collect(self) -> Iterator[GaugeMetricFamily]:
        """Uma familia por gauge cuja consulta voltou dentro do prazo da raspagem.

        As consultas correm numa thread a parte e este metodo espera por elas ate
        ``prazo_s`` no total, contado daqui. A gauge que falhou ou nao voltou a
        tempo fica de fora (e vai para o log com a classe do erro, que no prazo
        esgotado e ``TimeoutError``), nunca em zero. Nada sobe para quem
        raspa: uma falha do coletor, na consulta ou na montagem da familia, nao
        derruba o ``/metrics``. A thread que ficou presa num banco calado e
        daemon e morre com o socket (``tcp_user_timeout``) ou com o processo.
        """
        limite = time.monotonic() + self._prazo_s
        futuros: dict[str, Future[Sequence[Row[Any]]]] = {
            gauge.nome: Future() for gauge in _GAUGES
        }
        threading.Thread(
            target=self._consultar, args=(futuros,), name="coletor-da-api", daemon=True
        ).start()
        for gauge in _GAUGES:
            try:
                linhas = futuros[gauge.nome].result(
                    timeout=max(0.0, limite - time.monotonic())
                )
                familia = gauge.montar(linhas)
            except Exception as exc:  # noqa: BLE001 - a raspagem nunca cai pelo coletor
                _log.warning(
                    "api_gauge_unavailable",
                    gauge=gauge.nome,
                    error=type(exc).__name__,
                )
            else:
                yield familia

    def _consultar(self, futuros: Mapping[str, Future[Sequence[Row[Any]]]]) -> None:
        """Corpo da thread da raspagem: uma conexao, uma transacao por consulta."""
        try:
            conexao = self._engine.connect()
        except Exception as exc:  # noqa: BLE001 - o motivo vai para cada gauge
            for futuro in futuros.values():
                futuro.set_exception(exc)
            return
        with conexao:
            for gauge in _GAUGES:
                try:
                    with conexao.begin():
                        conexao.execute(_PRAZO_DA_CONSULTA)
                        linhas = conexao.execute(gauge.consulta, gauge.parametros).all()
                except Exception as exc:  # noqa: BLE001 - so esta gauge fica ausente
                    futuros[gauge.nome].set_exception(exc)
                else:
                    futuros[gauge.nome].set_result(linhas)
