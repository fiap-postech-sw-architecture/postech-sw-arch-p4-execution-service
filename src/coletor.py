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

Cada gauge tem a propria consulta, numa conexao curta e com prazo de 2 s (o da
espera por um lock inclusive): a que falha (banco fora, tabela travada, prazo
esgotado) fica ausente da raspagem, nunca em zero, e nao leva as outras junto.
Os rotulos ``servico`` e ``sku`` vem do que o diagnostico e a reserva guardam,
codigos que o Billing e o estoque ja validaram: a cardinalidade e a do catalogo.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from sqlalchemy import Connection, Row, TextClause

_log = structlog.get_logger(__name__)

JANELA_DE_NEGOCIO: Final = timedelta(days=7)
_DIAS: Final = JANELA_DE_NEGOCIO.days

_IDADE_DA_OUTBOX: Final = text(
    "SELECT COALESCE(EXTRACT(EPOCH FROM clock_timestamp() - min(criado_em)), 0) "
    "FROM outbox WHERE status = 'pendente'"
)
# Uma linha por (execucao, servico): o DISTINCT evita contar duas vezes a
# execucao de um diagnostico que lista o mesmo servico em dois itens.
_DURACAO_POR_SERVICO: Final = text(
    "SELECT s.codigo, avg(EXTRACT(EPOCH FROM e.finalizada_em - e.iniciada_em)) "
    "FROM execucoes e JOIN diagnosticos d ON d.ordem_id = e.ordem_id "
    "CROSS JOIN LATERAL ("
    "SELECT DISTINCT item ->> 'codigo' AS codigo "
    "FROM jsonb_array_elements(d.itens) AS item WHERE item ->> 'tipo' = 'servico'"
    ") s "
    "WHERE e.status = 'FINALIZADA' AND e.finalizada_em >= now() - :janela "
    "GROUP BY s.codigo"
)
_FALTA_POR_SKU: Final = text(
    "SELECT falta ->> 'sku', count(*) FROM reservas r "
    "CROSS JOIN LATERAL jsonb_array_elements(r.faltantes) AS falta "
    "WHERE r.status = 'RECUSADA' AND r.criada_em >= now() - :janela "
    "GROUP BY falta ->> 'sku'"
)
# O statement_timeout tambem corta a espera por um lock.
_PRAZO: Final = text("SET LOCAL statement_timeout = '2s'")


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


class ColetorDaApi:
    """Os tres gauges por consulta, lidos a cada raspagem do ``/metrics``."""

    def __init__(self, abrir_conexao: Callable[[], Connection]) -> None:
        self._abrir_conexao = abrir_conexao

    def describe(self) -> Iterator[GaugeMetricFamily]:
        """Os nomes, sem consultar o banco (o registro roda antes do boot terminar)."""
        yield _idade_da_outbox()
        yield _duracao_media()
        yield _pecas_em_falta()

    def collect(self) -> Iterator[GaugeMetricFamily]:
        idade = self._consultar("outbox", _IDADE_DA_OUTBOX)
        if idade is not None:
            familia = _idade_da_outbox()
            familia.add_metric([], float(idade[0][0]))
            yield familia
        duracoes = self._consultar("duracao", _DURACAO_POR_SERVICO)
        if duracoes is not None:
            familia = _duracao_media()
            for servico, media in duracoes:
                familia.add_metric([servico], float(media))
            yield familia
        faltas = self._consultar("falta", _FALTA_POR_SKU)
        if faltas is not None:
            familia = _pecas_em_falta()
            for sku, reservas in faltas:
                familia.add_metric([sku], float(reservas))
            yield familia

    def _consultar(self, nome: str, consulta: TextClause) -> Sequence[Row[Any]] | None:
        """As linhas da consulta, ou ``None`` se ela falhou (e o gauge sai ausente)."""
        try:
            with self._abrir_conexao() as conexao, conexao.begin():
                conexao.execute(_PRAZO)
                return conexao.execute(consulta, {"janela": JANELA_DE_NEGOCIO}).all()
        except Exception as exc:  # noqa: BLE001 - a raspagem nunca cai pelo coletor
            _log.warning("api_gauge_unavailable", gauge=nome, error=type(exc).__name__)
            return None
