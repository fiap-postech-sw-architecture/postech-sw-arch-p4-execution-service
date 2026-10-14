"""O coletor da API sem banco: a thread, o prazo e o socket derrubado.

A conexao e uma falsa que faz o que o driver faz quando o banco cala: bloqueia
numa leitura de socket que so volta quando alguem fecha o socket. O contra o banco
de verdade, com um proxy TCP, esta em ``tests/integracao/test_coletor.py``.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Self

import pytest

from src.coletor import ColetorDaApi, _derrubar, _Raspagem
from src.compartilhado.infraestrutura.database import parametros_de_metricas

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

IDADE = "pytstop_outbox_mais_antiga_segundos"
DURACAO = "pytstop_execucao_duracao_media_segundos"
FALTA = "pytstop_pecas_em_falta"
THREAD_DO_COLETOR = "coletor-da-api"
# O que as tres consultas devolvem: a idade da outbox, a duracao por servico e as
# reservas recusadas por SKU.
LINHAS = [[(120.0,)], [("SRV-A", 200.0)], [("PEC-A", 1)]]


def _threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == THREAD_DO_COLETOR]


def _esperar(condicao: Callable[[], bool], limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while not condicao():
        if time.monotonic() > fim:
            return False
        time.sleep(0.02)
    return True


@pytest.fixture(autouse=True)
def _nenhuma_thread_sobra() -> Iterator[None]:
    yield
    assert _esperar(lambda: not _threads(), 10), "a thread do coletor nao terminou"


class _Cursor:
    def __init__(self, conexao: _Conexao) -> None:
        self._conexao = conexao

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, consulta: str, parametros: dict[str, object]) -> None:
        self._conexao.executou.append((consulta, parametros))
        if self._conexao.ao_executar:
            self._conexao.ao_executar(len(self._conexao.executou))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._conexao.linhas[len(self._conexao.executou) - 1]


class _Conexao:
    """O que o coletor usa da conexao do driver."""

    def __init__(
        self,
        linhas: list[list[tuple[Any, ...]]] | None = None,
        *,
        fd: int = -1,
        ao_executar: Callable[[int], None] | None = None,
        falha_no_cursor: Exception | None = None,
    ) -> None:
        self.linhas = LINHAS if linhas is None else linhas
        self.autocommit = False
        self.fechada = False
        self.executou: list[tuple[str, dict[str, object]]] = []
        self.ao_executar = ao_executar
        self._fd = fd
        self._falha_no_cursor = falha_no_cursor

    def fileno(self) -> int:
        return self._fd

    def cursor(self) -> _Cursor:
        if self._falha_no_cursor is not None:
            raise self._falha_no_cursor
        return _Cursor(self)

    def close(self) -> None:
        self.fechada = True


def _nomes(conexao: _Conexao, *, prazo_s: float = 5.0) -> set[str]:
    return {f.name for f in ColetorDaApi(lambda: conexao, prazo_s=prazo_s).collect()}


def test_as_tres_consultas_rodam_na_mesma_conexao_em_autocommit_e_ela_fecha() -> None:
    lado, par = socket.socketpair()
    with lado, par:
        conexao = _Conexao(fd=lado.fileno())

        familias = {
            f.name: [s.value for s in f.samples]
            for f in ColetorDaApi(lambda: conexao).collect()
        }

        assert familias == {IDADE: [120.0], DURACAO: [200.0], FALTA: [1.0]}
        assert conexao.autocommit is True
        assert conexao.fechada is True
        consultas = [c for c, _ in conexao.executou]
        assert [
            ("outbox" in c, "execucoes" in c, "reservas" in c) for c in consultas
        ] == [
            (True, False, False),
            (False, True, False),
            (False, False, True),
        ]
        # A raspagem que terminou a tempo nao derruba socket nenhum.
        par.settimeout(0.3)
        with pytest.raises(TimeoutError):
            par.recv(1)


def test_consulta_que_falha_deixa_so_a_gauge_dela_ausente(
    log_capturado: Any,
) -> None:
    def falha_na_segunda(numero: int) -> None:
        if numero == 2:
            msg = "canceling statement due to statement timeout"
            raise RuntimeError(msg)

    conexao = _Conexao(ao_executar=falha_na_segunda)

    assert _nomes(conexao) == {IDADE, FALTA}
    erros = [
        (linha["gauge"], linha["error"])
        for linha in map(json.loads, log_capturado.getvalue().splitlines())
        if linha.get("event") == "api_gauge_unavailable"
    ]
    assert erros == [("duracao", "RuntimeError")]


def test_conexao_que_nao_abre_deixa_as_tres_ausentes_sem_esperar_o_prazo() -> None:
    def sem_rota() -> _Conexao:
        raise OSError

    inicio = time.monotonic()
    familias = list(ColetorDaApi(sem_rota, prazo_s=5.0).collect())

    assert familias == []
    assert time.monotonic() - inicio < 2


def test_cursor_que_nao_abre_deixa_as_tres_ausentes_e_a_conexao_fecha() -> None:
    conexao = _Conexao(falha_no_cursor=RuntimeError())

    assert _nomes(conexao) == set()
    assert conexao.fechada is True
    assert conexao.executou == []


def test_prazo_estourado_derruba_o_socket_da_conexao_presa() -> None:
    """A segunda consulta le de um socket que o outro lado nunca escreve, como o
    libpq com o banco calado; so o fim de conexao a acorda."""
    lado, par = socket.socketpair()
    with lado, par:

        def le_do_socket(numero: int) -> None:
            if numero == 2 and not lado.recv(1):
                msg = "server closed the connection unexpectedly"
                raise ConnectionError(msg)

        conexao = _Conexao(fd=lado.fileno(), ao_executar=le_do_socket)

        inicio = time.monotonic()
        familias = _nomes(conexao, prazo_s=0.4)

        # A primeira gauge ja tinha chegado; a segunda e a terceira ficam de fora.
        assert familias == {IDADE}
        assert time.monotonic() - inicio >= 0.4
        # E a thread sai logo: o coletor derrubou o socket, nao esperou o outro lado.
        assert _esperar(lambda: not _threads(), 2)
        assert conexao.fechada is True


def test_conexao_que_abre_depois_do_prazo_nao_e_usada_e_fecha() -> None:
    """O connect que nao volta no prazo (a resolucao do nome do banco, por exemplo)
    deixa a thread, daemon, esperando; quando volta, a conexao so se fecha."""
    liberar = threading.Event()
    conexao = _Conexao()

    def abre_tarde() -> _Conexao:
        liberar.wait(10)
        return conexao

    familias = list(ColetorDaApi(abre_tarde, prazo_s=0.3).collect())

    assert familias == []
    (presa,) = _threads()
    assert presa.daemon
    liberar.set()
    assert _esperar(lambda: not _threads(), 5)
    assert conexao.fechada is True
    assert conexao.executou == []
    assert conexao.autocommit is False


def test_derrubar_acorda_a_leitura_que_nao_volta() -> None:
    lado, par = socket.socketpair()
    with lado, par:
        lidos: list[bytes] = []
        leitor = threading.Thread(target=lambda: lidos.append(lado.recv(1)))
        leitor.start()

        _derrubar(lado.fileno())

        leitor.join(2)
        assert not leitor.is_alive()
        assert lidos == [b""]
        # O descritor do `lado` continua aberto e valido: so o socket foi encerrado.
        assert lado.fileno() >= 0


def test_derrubar_ignora_descritor_invalido_e_o_que_nao_e_socket() -> None:
    antes = len(os.listdir("/dev/fd"))
    _derrubar(-1)
    with open(os.devnull, "rb") as arquivo:
        _derrubar(arquivo.fileno())
    # Nenhum descritor duplicado ficou para tras.
    assert len(os.listdir("/dev/fd")) == antes


def test_derrubar_ignora_o_socket_que_nao_esta_conectado() -> None:
    with socket.socket() as solto:
        _derrubar(solto.fileno())


def test_depois_do_encerramento_a_conexao_nova_nao_serve_mais() -> None:
    raspagem = _Raspagem()
    raspagem.encerrar()

    assert raspagem.usar(_Conexao()) is False


def test_conexao_largada_nao_tem_o_descritor_derrubado() -> None:
    """A thread larga o descritor antes de fechar a conexao: depois disso o numero
    pode ser de outro socket, e o encerramento nao o toca."""
    lado, par = socket.socketpair()
    with lado, par:
        raspagem = _Raspagem()
        assert raspagem.usar(_Conexao(fd=lado.fileno())) is True
        raspagem.largar()

        raspagem.encerrar()

        par.settimeout(0.3)
        with pytest.raises(TimeoutError):
            par.recv(1)


def test_parametros_da_conexao_de_metricas() -> None:
    # Os valores ficam escritos aqui de proposito: mudar um deles e decisao.
    assert parametros_de_metricas() == {
        "connect_timeout": 1,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "tcp_user_timeout": 10_000,
        "application_name": "pytstop-execucao-metricas",
        "options": "-c statement_timeout=2000",
    }
