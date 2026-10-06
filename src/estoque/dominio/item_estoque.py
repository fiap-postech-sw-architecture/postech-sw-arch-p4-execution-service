from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import (
    EstoqueInsuficienteException,
    ValorInvalidoError,
    ViolacaoRegraDeNegocioException,
)
from src.compartilhado.dominio.texto import texto_valido

if TYPE_CHECKING:
    from src.estoque.dominio.sku import Sku

TAMANHO_MAXIMO_NOME: Final = 255


def _nome_valido(nome: str) -> str:
    return texto_valido(nome, "Nome do item", maximo=TAMANHO_MAXIMO_NOME)


def _exigir_positiva(quantidade: int, operacao: str) -> None:
    if quantidade <= 0:
        msg = f"Quantidade para {operacao} deve ser positiva (recebido: {quantidade})"
        raise ValorInvalidoError(msg)


@dataclass(eq=False, kw_only=True)
class ItemEstoque(AggregateRoot):
    """Peca no estoque fisico, identificada pelo ``sku``.

    ``quantidade_disponivel`` e o saldo fisico (inclui as unidades reservadas);
    ``quantidade_reservada`` e a parte comprometida com reservas ativas e
    ``quantidade_livre`` o que ainda pode ser reservado. Invariante:
    ``0 <= quantidade_reservada <= quantidade_disponivel`` (tambem CHECK no banco).
    """

    _sku: Sku
    _nome: str
    _quantidade_disponivel: int
    _quantidade_reservada: int = 0
    _ativo: bool = True

    def __post_init__(self) -> None:
        self._nome = _nome_valido(self._nome)
        if self._quantidade_disponivel < 0:
            msg = "Quantidade disponivel nao pode ser negativa"
            raise ValorInvalidoError(msg)
        if not 0 <= self._quantidade_reservada <= self._quantidade_disponivel:
            msg = "Quantidade reservada deve estar entre 0 e a quantidade disponivel"
            raise ValorInvalidoError(msg)

    @classmethod
    def criar(cls, *, sku: Sku, nome: str, quantidade_disponivel: int) -> ItemEstoque:
        return cls(_sku=sku, _nome=nome, _quantidade_disponivel=quantidade_disponivel)

    @property
    def sku(self) -> Sku:
        return self._sku

    @property
    def nome(self) -> str:
        return self._nome

    @property
    def quantidade_disponivel(self) -> int:
        return self._quantidade_disponivel

    @property
    def quantidade_reservada(self) -> int:
        return self._quantidade_reservada

    @property
    def quantidade_livre(self) -> int:
        return self._quantidade_disponivel - self._quantidade_reservada

    @property
    def ativo(self) -> bool:
        return self._ativo

    def renomear(self, nome: str) -> None:
        self._nome = _nome_valido(nome)

    def ajustar_quantidade(self, quantidade_disponivel: int) -> None:
        """Define o saldo fisico (inventario); nunca abaixo do que esta reservado."""
        if quantidade_disponivel < 0:
            msg = "Quantidade disponivel nao pode ser negativa"
            raise ValorInvalidoError(msg)
        if quantidade_disponivel < self._quantidade_reservada:
            msg = (
                f"Quantidade disponivel ({quantidade_disponivel}) abaixo da reservada "
                f"({self._quantidade_reservada}) para ordens em andamento"
            )
            raise ViolacaoRegraDeNegocioException(msg)
        self._quantidade_disponivel = quantidade_disponivel

    def desativar(self) -> None:
        """Tira o item de novas reservas e diagnosticos. Idempotente."""
        if self._quantidade_reservada > 0:
            msg = (
                f"Item {self._sku} tem {self._quantidade_reservada} unidade(s) "
                "reservada(s) para ordens em andamento; libere ou consuma antes"
            )
            raise ViolacaoRegraDeNegocioException(msg)
        self._ativo = False

    def ativar(self) -> None:
        self._ativo = True

    def reservar(self, quantidade: int) -> None:
        _exigir_positiva(quantidade, "reserva")
        if not self._ativo:
            msg = f"Item {self._sku} esta inativo e nao pode ser reservado"
            raise ViolacaoRegraDeNegocioException(msg)
        if self.quantidade_livre < quantidade:
            msg = (
                f"Estoque insuficiente de {self._sku}: livre={self.quantidade_livre}, "
                f"solicitado={quantidade}"
            )
            raise EstoqueInsuficienteException(msg)
        self._quantidade_reservada += quantidade

    def liberar_reserva(self, quantidade: int) -> None:
        """Devolve ao saldo livre unidades antes reservadas."""
        _exigir_positiva(quantidade, "liberacao")
        self._exigir_reservado(quantidade)
        self._quantidade_reservada -= quantidade

    def consumir_reserva(self, quantidade: int) -> None:
        """Baixa: as unidades reservadas saem do estoque fisico."""
        _exigir_positiva(quantidade, "baixa")
        self._exigir_reservado(quantidade)
        self._quantidade_reservada -= quantidade
        self._quantidade_disponivel -= quantidade

    def _exigir_reservado(self, quantidade: int) -> None:
        if self._quantidade_reservada < quantidade:
            msg = (
                f"Item {self._sku} tem {self._quantidade_reservada} unidade(s) "
                f"reservada(s); operacao pediu {quantidade}"
            )
            raise ViolacaoRegraDeNegocioException(msg)
