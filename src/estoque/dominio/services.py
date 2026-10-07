"""Servico de dominio da reserva: coordena ``Reserva`` com os ``ItemEstoque``.

A regra tudo-ou-nada atravessa varios agregados (a reserva e cada peca), por
isso mora aqui e nao num deles. Os itens chegam travados (``SELECT ... FOR
UPDATE`` em ordem de sku) pelo repositorio; este modulo so decide e aplica.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.dominio.exceptions import EstoqueInsuficienteException
from src.estoque.dominio.exceptions import ItemEstoqueNaoEncontradoException
from src.estoque.dominio.reserva import Faltante

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from src.estoque.dominio.item_estoque import ItemEstoque
    from src.estoque.dominio.reserva import ItemReserva, Reserva
    from src.estoque.dominio.sku import Sku


def calcular_faltantes(
    pecas: Iterable[ItemReserva], itens: Mapping[Sku, ItemEstoque]
) -> list[Faltante]:
    """Pecas sem saldo livre suficiente; SKU ausente ou inativo conta como 0."""
    faltantes = []
    for peca in pecas:
        item = itens.get(peca.sku)
        livre = item.quantidade_livre if item is not None and item.ativo else 0
        if livre < peca.quantidade:
            faltantes.append(
                Faltante(
                    sku=str(peca.sku), solicitado=peca.quantidade, disponivel=livre
                )
            )
    return faltantes


def reservar(reserva: Reserva, itens: Mapping[Sku, ItemEstoque]) -> None:
    """Separa as pecas da reserva; com qualquer faltante, nada muda.

    Raises:
        EstoqueInsuficienteException: alguma peca sem saldo livre suficiente.
    """
    faltantes = calcular_faltantes(reserva.itens, itens)
    if faltantes:
        resumo = ", ".join(
            f"{f.sku} (solicitado {f.solicitado}, livre {f.disponivel})"
            for f in faltantes
        )
        msg = f"Estoque insuficiente: {resumo}"
        raise EstoqueInsuficienteException(msg)
    for linha in reserva.itens:
        itens[linha.sku].reservar(linha.quantidade)


def liberar(
    reserva: Reserva, itens: Mapping[Sku, ItemEstoque], agora: datetime
) -> None:
    """Devolve as quantidades reservadas; reserva ja liberada e no-op."""
    if reserva.liberar(agora):
        for linha in reserva.itens:
            _item(itens, linha.sku).liberar_reserva(linha.quantidade)


def consumir(
    reserva: Reserva, itens: Mapping[Sku, ItemEstoque], agora: datetime
) -> None:
    """Baixa: debita reservado e disponivel de cada peca da reserva."""
    reserva.consumir(agora)
    for linha in reserva.itens:
        _item(itens, linha.sku).consumir_reserva(linha.quantidade)


def _item(itens: Mapping[Sku, ItemEstoque], sku: Sku) -> ItemEstoque:
    item = itens.get(sku)
    if item is None:
        raise ItemEstoqueNaoEncontradoException(sku)
    return item
