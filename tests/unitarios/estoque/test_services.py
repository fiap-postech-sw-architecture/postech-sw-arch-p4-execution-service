from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.compartilhado.dominio.exceptions import EstoqueInsuficienteException
from src.estoque.dominio.exceptions import ItemEstoqueNaoEncontradoException
from src.estoque.dominio.item_estoque import ItemEstoque
from src.estoque.dominio.reserva import Faltante, ItemReserva, Reserva
from src.estoque.dominio.services import calcular_faltantes, consumir, liberar, reservar
from src.estoque.dominio.sku import Sku

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _itens(**saldos: int) -> dict[Sku, ItemEstoque]:
    itens = {}
    for nome, saldo in saldos.items():
        sku = Sku(nome.replace("_", "-"))
        itens[sku] = ItemEstoque.criar(sku=sku, nome=nome, quantidade_disponivel=saldo)
    return itens


def _reserva(**pecas: int) -> Reserva:
    linhas = [ItemReserva(Sku(nome.replace("_", "-")), q) for nome, q in pecas.items()]
    return Reserva.criar(ordem_id=uuid4(), itens=linhas, agora=AGORA)


def test_faltantes_considera_ausente_e_inativo_como_zero() -> None:
    itens = _itens(PEC_A=5, PEC_INATIVA=9)
    itens[Sku("PEC-INATIVA")].desativar()
    pecas = _reserva(PEC_A=2, PEC_INATIVA=1, PEC_SUMIDA=3).itens

    assert calcular_faltantes(pecas, itens) == [
        Faltante(sku=Sku("PEC-INATIVA"), solicitado=1, disponivel=0),
        Faltante(sku=Sku("PEC-SUMIDA"), solicitado=3, disponivel=0),
    ]


def test_faltante_informa_o_saldo_livre_e_nao_o_fisico() -> None:
    itens = _itens(PEC_A=5)
    itens[Sku("PEC-A")].reservar(4)
    assert calcular_faltantes(_reserva(PEC_A=2).itens, itens) == [
        Faltante(sku=Sku("PEC-A"), solicitado=2, disponivel=1)
    ]


def test_reservar_separa_todas_as_pecas() -> None:
    itens = _itens(PEC_A=5, PEC_B=1)
    reservar(_reserva(PEC_A=2, PEC_B=1), itens)
    assert itens[Sku("PEC-A")].quantidade_reservada == 2
    assert itens[Sku("PEC-B")].quantidade_reservada == 1


def test_reservar_e_tudo_ou_nada() -> None:
    itens, reserva = _itens(PEC_A=5, PEC_B=0), _reserva(PEC_A=2, PEC_B=1)
    with pytest.raises(EstoqueInsuficienteException, match="PEC-B"):
        reservar(reserva, itens)
    assert itens[Sku("PEC-A")].quantidade_reservada == 0


def test_liberar_devolve_uma_vez_so() -> None:
    itens = _itens(PEC_A=5)
    reserva = _reserva(PEC_A=2)
    reservar(reserva, itens)
    liberar(reserva, itens, AGORA)
    liberar(reserva, itens, AGORA)
    assert itens[Sku("PEC-A")].quantidade_reservada == 0


def test_consumir_baixa_o_estoque_fisico() -> None:
    itens = _itens(PEC_A=5)
    reserva = _reserva(PEC_A=2)
    reservar(reserva, itens)
    consumir(reserva, itens, AGORA)
    item = itens[Sku("PEC-A")]
    assert (item.quantidade_disponivel, item.quantidade_reservada) == (3, 0)


def test_item_da_reserva_sumido_e_erro_explicito() -> None:
    reserva = _reserva(PEC_A=1)
    with pytest.raises(ItemEstoqueNaoEncontradoException, match="PEC-A"):
        consumir(reserva, {}, AGORA)
