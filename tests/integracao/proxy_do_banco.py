"""Proxy TCP na frente do banco, para os testes do que a rede faz com ele.

O PostgreSQL de teste responde sempre; o que um incidente faz e o banco calar com
a conexao aberta (o outro lado da rede some, o processo para) ou atender devagar.
O proxy repassa os bytes entre o cliente e o banco e, a pedido, os descarta
(``calar`` para todas as conexoes, ``calar_abertas`` so para as que ja existem,
``calar_no_comando`` a partir do n-esimo comando SQL de cada conexao) ou os atrasa
(``atrasar``) sem fechar nenhum socket: o cliente fica esperando uma resposta que
nao vem, como no incidente. Quem fecha o socket e o cliente, e ``abertas`` conta as
conexoes que o proxy ainda segura.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from typing import TYPE_CHECKING

from sqlalchemy import make_url

if TYPE_CHECKING:
    from types import TracebackType
    from typing import Self


class ProxyDoBanco:
    def __init__(self, destino: tuple[str, int]) -> None:
        self._destino = destino
        self._calado = threading.Event()
        self._atraso_s = 0.0
        self._calar_no_comando: int | None = None
        self._vivas = 0
        self._sockets: list[socket.socket] = []
        # Um sinal por conexao: ``calar_abertas`` cala as que existem e deixa as novas.
        self._calados_por_conexao: list[threading.Event] = []
        self._trava = threading.Lock()
        self._servidor = socket.create_server(("127.0.0.1", 0))
        self.porta: int = self._servidor.getsockname()[1]
        threading.Thread(target=self._aceitar, daemon=True).start()

    @classmethod
    def para(cls, url_do_banco: str) -> ProxyDoBanco:
        banco = make_url(url_do_banco)
        return cls((banco.host or "127.0.0.1", banco.port or 5432))

    def url_para(self, url_do_banco: str) -> str:
        """A mesma URL do banco, passando pelo proxy."""
        return (
            make_url(url_do_banco)
            .set(host="127.0.0.1", port=self.porta)
            .render_as_string(hide_password=False)
        )

    def calar(self) -> None:
        """Daqui em diante nenhum byte passa, e nenhum socket fecha."""
        self._calado.set()

    def calar_abertas(self) -> None:
        """So as conexoes que ja existem calam; as novas passam (o banco voltou)."""
        with self._trava:
            conexoes = list(self._calados_por_conexao)
        for calada in conexoes:
            calada.set()

    def calar_no_comando(self, numero: int | None) -> None:
        """Cada conexao passa a calar no ``numero``-esimo comando SQL do cliente.

        O handshake e os comandos anteriores passam inteiros; o comando que calou
        e os seguintes nao chegam ao banco. ``None`` desfaz: as conexoes novas
        passam (o banco voltou). Os comandos sao as mensagens ``Query`` do
        protocolo (o primeiro byte e ``Q``); o handshake nao comeca assim.
        """
        self._calar_no_comando = numero

    @property
    def abertas(self) -> int:
        """Quantas conexoes o proxy ainda segura (o cliente nao fechou o socket)."""
        with self._trava:
            return self._vivas

    def atrasar(self, segundos: float) -> None:
        """Cada trecho repassado espera ``segundos`` antes de seguir."""
        self._atraso_s = segundos

    def fechar(self) -> None:
        """Fecha tudo: quem esperava resposta recebe o fim da conexao."""
        self._servidor.close()
        with self._trava:
            sockets, self._sockets = self._sockets, []
        for aberto in sockets:
            aberto.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        tipo: type[BaseException] | None,
        erro: BaseException | None,
        pilha: TracebackType | None,
    ) -> None:
        self.fechar()

    def _aceitar(self) -> None:
        while True:
            try:
                cliente, _ = self._servidor.accept()
            except OSError:
                return
            threading.Thread(target=self._ligar, args=(cliente,), daemon=True).start()

    def _ligar(self, cliente: socket.socket) -> None:
        try:
            banco = socket.create_connection(self._destino, timeout=5)
        except OSError:
            cliente.close()
            return
        calada = threading.Event()
        with self._trava:
            self._sockets += [cliente, banco]
            self._calados_por_conexao.append(calada)
            self._vivas += 1
        destinos = {cliente: banco, banco: cliente}
        comandos = 0
        try:
            while True:
                prontos, _, _ = select.select(list(destinos), [], [], 0.2)
                for origem in prontos:
                    dados = origem.recv(65536)
                    if not dados:
                        return
                    if origem is cliente and dados[:1] == b"Q":
                        comandos += 1
                        limite = self._calar_no_comando
                        if limite is not None and comandos >= limite:
                            calada.set()
                    if self._calado.is_set() or calada.is_set():
                        continue
                    if self._atraso_s:
                        time.sleep(self._atraso_s)
                    destinos[origem].sendall(dados)
        except (OSError, ValueError):
            return
        finally:
            with self._trava:
                self._vivas -= 1
            cliente.close()
            banco.close()
