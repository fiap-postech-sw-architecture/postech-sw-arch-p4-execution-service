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

Tempo da raspagem. Cada raspagem abre uma conexao crua do driver
(``abrir_conexao_de_metricas``: sem pool nem engine, ate 1 s para conectar, com o
``statement_timeout`` de 2 s nas opcoes, que corta tambem a espera por lock) e
roda as tres consultas nela, em sequencia e em autocommit, cada uma independente
das outras. Elas correm numa thread a parte, e ``collect`` espera por elas no
maximo ``PRAZO_DA_RASPAGEM_S`` no total. O prazo existe porque o timeout do banco
nao cobre tudo: com o socket aberto e o banco calado (o outro lado da rede ou o
processo parado, sem fechar a conexao) nenhum timeout do driver dispara, e a
raspagem esperaria para sempre. Passado o prazo o que nao voltou fica de fora, e
o coletor derruba o socket da conexao que a thread ainda usa: a leitura que nao
voltava acorda com fim de conexao, a thread sai e o servidor ve a conexao
fechar, entao nada fica preso depois da raspagem. Fica de fora do alcance do
prazo so a resolucao do nome do banco, que o driver faz sem prazo. A gauge sem
resposta, qualquer que seja o motivo (banco fora, tabela travada, prazo
esgotado), sai ausente da raspagem, nunca em zero, e o resto do ``/metrics``
segue.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
from concurrent.futures import Future
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import structlog
from prometheus_client.core import GaugeMetricFamily

from src.diagnostico.dominio.diagnostico import TipoItem
from src.estoque.dominio.reserva import StatusReserva
from src.execucao.dominio.execucao import StatusExecucao

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

_log = structlog.get_logger(__name__)

JANELA_DE_NEGOCIO: Final = timedelta(days=7)
_DIAS: Final = JANELA_DE_NEGOCIO.days
# Todas as consultas de uma raspagem juntas. O Prometheus do platform espera 10 s
# (o `scrape_timeout` padrao): a folga fica para o resto do /metrics.
PRAZO_DA_RASPAGEM_S: Final = 5.0

_IDADE_DA_OUTBOX: Final = (
    "SELECT COALESCE(EXTRACT(EPOCH FROM clock_timestamp() - min(criado_em)), 0) "
    "FROM outbox WHERE status = 'pendente'"
)
# Uma linha por (execucao, servico): o DISTINCT evita contar duas vezes a
# execucao de um diagnostico que lista o mesmo servico em dois itens. As linhas
# sem o que a gauge precisa (execucao sem inicio, item sem codigo) ficam de fora:
# uma so nao pode derrubar a familia inteira.
_DURACAO_POR_SERVICO: Final = (
    "SELECT s.codigo, avg(EXTRACT(EPOCH FROM e.finalizada_em - e.iniciada_em)) "
    "FROM execucoes e JOIN diagnosticos d ON d.ordem_id = e.ordem_id "
    "CROSS JOIN LATERAL ("
    "SELECT DISTINCT item ->> 'codigo' AS codigo "
    "FROM jsonb_array_elements(d.itens) AS item "
    "WHERE item ->> 'tipo' = %(servico)s AND item ->> 'codigo' IS NOT NULL"
    ") s "
    "WHERE e.status = %(finalizada)s AND e.iniciada_em IS NOT NULL "
    "AND e.finalizada_em >= now() - %(janela)s "
    "GROUP BY s.codigo"
)
_FALTA_POR_SKU: Final = (
    "SELECT falta ->> 'sku', count(*) FROM reservas r "
    "CROSS JOIN LATERAL jsonb_array_elements(r.faltantes) AS falta "
    "WHERE r.status = %(recusada)s AND r.criada_em >= now() - %(janela)s "
    "AND falta ->> 'sku' IS NOT NULL "
    "GROUP BY falta ->> 'sku'"
)


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


def _montar_idade(linhas: Sequence[tuple[Any, ...]]) -> GaugeMetricFamily:
    familia = _idade_da_outbox()
    familia.add_metric([], float(linhas[0][0]))
    return familia


def _montar_duracao(linhas: Sequence[tuple[Any, ...]]) -> GaugeMetricFamily:
    familia = _duracao_media()
    for servico, media in linhas:
        familia.add_metric([servico], float(media))
    return familia


def _montar_falta(linhas: Sequence[tuple[Any, ...]]) -> GaugeMetricFamily:
    familia = _pecas_em_falta()
    for sku, reservas in linhas:
        familia.add_metric([sku], float(reservas))
    return familia


class _Gauge(NamedTuple):
    nome: str
    consulta: str
    parametros: Mapping[str, object]
    montar: Callable[[Sequence[tuple[Any, ...]]], GaugeMetricFamily]


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


def _derrubar(fd: int) -> None:
    """Fecha o socket de uma conexao que outra thread usa, sem soltar o descritor.

    ``shutdown`` num descritor duplicado acorda a leitura que nao volta com fim de
    conexao: a thread da raspagem sai com erro, o driver fecha o resto e o
    servidor ve a conexao terminar.
    """
    try:
        duplicado = os.dup(fd)
    except OSError:
        return
    try:
        irmao = socket.socket(fileno=duplicado)
    except OSError:  # nao e socket: o duplicado ainda e nosso
        os.close(duplicado)
        return
    with irmao, contextlib.suppress(OSError):
        irmao.shutdown(socket.SHUT_RDWR)


class _Raspagem:
    """O descritor da conexao em uso numa raspagem, que a thread dela e quem espera
    por ela dividem.

    O trinco garante que ``encerrar`` so derruba um descritor que a thread ainda
    nao largou (``largar`` roda antes de ela fechar a conexao): sem isso o numero
    poderia ja ser de outro socket.
    """

    def __init__(self) -> None:
        self._trinco = threading.Lock()
        self._fd: int | None = None
        self._encerrada = False

    def usar(self, conexao: Any) -> bool:  # noqa: ANN401 - conexao crua do driver
        """Registra a conexao recem-aberta; ``False`` se o prazo ja passou."""
        with self._trinco:
            if self._encerrada:
                return False
            self._fd = conexao.fileno()
            return True

    def largar(self) -> None:
        """A thread vai fechar a conexao: o descritor deixa de ser dela."""
        with self._trinco:
            self._fd = None

    def encerrar(self) -> None:
        """O prazo passou: derruba o socket da conexao que a thread ainda usa."""
        with self._trinco:
            self._encerrada = True
            if self._fd is not None:
                _derrubar(self._fd)


def _falhar(
    futuros: Mapping[str, Future[list[tuple[Any, ...]]]], exc: Exception
) -> None:
    for futuro in futuros.values():
        futuro.set_exception(exc)


class ColetorDaApi:
    """As tres gauges do coletor, calculadas a cada raspagem do ``/metrics``."""

    def __init__(
        self,
        abrir_conexao: Callable[[], Any],
        *,
        prazo_s: float = PRAZO_DA_RASPAGEM_S,
    ) -> None:
        self._abrir_conexao = abrir_conexao
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
        derruba o ``/metrics``. Se alguma consulta ficou sem resposta, o socket
        da conexao dela e derrubado ao fim (ver o docstring do modulo), e a
        thread, daemon, sai logo depois.
        """
        limite = time.monotonic() + self._prazo_s
        futuros: dict[str, Future[list[tuple[Any, ...]]]] = {
            gauge.nome: Future() for gauge in _GAUGES
        }
        raspagem = _Raspagem()
        threading.Thread(
            target=self._consultar,
            args=(raspagem, futuros),
            name="coletor-da-api",
            daemon=True,
        ).start()
        try:
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
        finally:
            if not all(futuro.done() for futuro in futuros.values()):
                raspagem.encerrar()

    def _consultar(
        self,
        raspagem: _Raspagem,
        futuros: Mapping[str, Future[list[tuple[Any, ...]]]],
    ) -> None:
        """Corpo da thread da raspagem: uma conexao, as tres consultas."""
        try:
            conexao = self._abrir_conexao()
        except Exception as exc:  # noqa: BLE001 - o motivo vai para cada gauge
            _falhar(futuros, exc)
            return
        try:
            # Prazo ja passado durante o connect: ninguem espera mais por isto.
            if raspagem.usar(conexao):
                self._rodar(conexao, futuros)
        finally:
            raspagem.largar()
            conexao.close()

    @staticmethod
    def _rodar(
        conexao: Any,  # noqa: ANN401 - conexao crua do driver
        futuros: Mapping[str, Future[list[tuple[Any, ...]]]],
    ) -> None:
        # Autocommit: cada consulta e a sua transacao, e a que falha nao estraga
        # a seguinte; o statement_timeout ja veio nas opcoes da conexao.
        try:
            conexao.autocommit = True
            cursor = conexao.cursor()
        except Exception as exc:  # noqa: BLE001 - o motivo vai para cada gauge
            _falhar(futuros, exc)
            return
        with cursor:
            for gauge in _GAUGES:
                try:
                    cursor.execute(gauge.consulta, gauge.parametros)
                    linhas = cursor.fetchall()
                except Exception as exc:  # noqa: BLE001 - so esta gauge fica ausente
                    futuros[gauge.nome].set_exception(exc)
                else:
                    futuros[gauge.nome].set_result(linhas)
