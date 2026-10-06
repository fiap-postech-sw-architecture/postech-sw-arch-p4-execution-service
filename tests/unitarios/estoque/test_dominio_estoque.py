from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.compartilhado.dominio.exceptions import (
    EstoqueInsuficienteException,
    TransicaoStatusInvalidaException,
    ViolacaoRegraDeNegocioException,
)
from src.estoque.dominio.item_estoque import ItemEstoque
from src.estoque.dominio.reserva import Faltante, ItemReserva, Reserva, StatusReserva
from src.estoque.dominio.sku import Sku

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _item(
    quantidade: int = 10, reservada: int = 0, sku: str = "PEC-VELA"
) -> ItemEstoque:
    item = ItemEstoque.criar(
        sku=Sku(sku), nome="Vela", quantidade_disponivel=quantidade
    )
    if reservada:
        item.reservar(reservada)
    return item


class TestSku:
    @pytest.mark.parametrize("valor", ["PEC-OLEO-5W30", "PEC-VELA", "A", "X1-2-3"])
    def test_formatos_validos(self, valor: str) -> None:
        assert str(Sku(valor)) == valor

    @pytest.mark.parametrize(
        "valor",
        [
            "",
            "pec-vela",
            "PEC VELA",
            "PEC--VELA",
            "-PEC",
            "PEC-",
            "PEC-VELA\n",
            "A" * 65,
        ],
    )
    def test_formatos_invalidos(self, valor: str) -> None:
        with pytest.raises(ValueError, match="SKU invalido"):
            Sku(valor)

    def test_igualdade_estrutural_e_imutavel(self) -> None:
        um, outro = Sku("PEC-VELA"), Sku("PEC-VELA")
        assert um == outro
        assert hash(um) == hash(outro)
        with pytest.raises(FrozenInstanceError):
            um.valor = "OUTRO"  # type: ignore[misc]


class TestItemEstoque:
    def test_criar_normaliza_nome_e_comeca_sem_reserva(self) -> None:
        item = ItemEstoque.criar(
            sku=Sku("PEC-X"), nome="  Oleo  ", quantidade_disponivel=3
        )
        assert (item.nome, item.quantidade_reservada, item.quantidade_livre) == (
            "Oleo",
            0,
            3,
        )
        assert item.ativo

    @pytest.mark.parametrize("nome", ["", "   ", "x" * 256])
    def test_nome_invalido(self, nome: str) -> None:
        sku = Sku("PEC-X")
        with pytest.raises(ValueError, match="Nome do item"):
            ItemEstoque.criar(sku=sku, nome=nome, quantidade_disponivel=1)

    def test_disponivel_negativo(self) -> None:
        sku = Sku("PEC-X")
        with pytest.raises(ValueError, match="negativa"):
            ItemEstoque.criar(sku=sku, nome="x", quantidade_disponivel=-1)

    @pytest.mark.parametrize("reservada", [-1, 4])
    def test_reservada_fora_do_disponivel(self, reservada: int) -> None:
        sku = Sku("PEC-X")
        with pytest.raises(ValueError, match="reservada"):
            ItemEstoque(
                _sku=sku,
                _nome="x",
                _quantidade_disponivel=3,
                _quantidade_reservada=reservada,
            )

    def test_reservar_compromete_sem_tirar_do_fisico(self) -> None:
        item = _item(10)
        item.reservar(4)
        assert (item.quantidade_disponivel, item.quantidade_reservada) == (10, 4)
        assert item.quantidade_livre == 6

    def test_reservar_alem_do_livre(self) -> None:
        item = _item(10, reservada=8)
        with pytest.raises(EstoqueInsuficienteException, match="livre=2"):
            item.reservar(3)
        assert item.quantidade_reservada == 8

    def test_item_inativo_nao_reserva(self) -> None:
        item = _item(10)
        item.desativar()
        with pytest.raises(ViolacaoRegraDeNegocioException, match="inativo"):
            item.reservar(1)

    @pytest.mark.parametrize(
        "operacao", ["reservar", "liberar_reserva", "consumir_reserva"]
    )
    def test_quantidade_nao_positiva(self, operacao: str) -> None:
        metodo = getattr(_item(10, reservada=2), operacao)
        with pytest.raises(ValueError, match="positiva"):
            metodo(0)

    def test_liberar_devolve_ao_livre(self) -> None:
        item = _item(10, reservada=4)
        item.liberar_reserva(3)
        assert (item.quantidade_disponivel, item.quantidade_reservada) == (10, 1)

    def test_consumir_debita_reservado_e_disponivel(self) -> None:
        item = _item(10, reservada=4)
        item.consumir_reserva(4)
        assert (item.quantidade_disponivel, item.quantidade_reservada) == (6, 0)

    @pytest.mark.parametrize("operacao", ["liberar_reserva", "consumir_reserva"])
    def test_nao_libera_nem_consome_alem_do_reservado(self, operacao: str) -> None:
        item = _item(10, reservada=2)
        metodo = getattr(item, operacao)
        with pytest.raises(ViolacaoRegraDeNegocioException, match="reservada"):
            metodo(3)
        assert item.quantidade_reservada == 2

    def test_ajustar_quantidade(self) -> None:
        item = _item(10, reservada=4)
        item.ajustar_quantidade(4)
        assert item.quantidade_disponivel == 4

    def test_ajuste_abaixo_do_reservado_e_409(self) -> None:
        item = _item(10, reservada=4)
        with pytest.raises(
            ViolacaoRegraDeNegocioException, match="abaixo da reservada"
        ):
            item.ajustar_quantidade(3)
        assert item.quantidade_disponivel == 10

    def test_ajuste_negativo(self) -> None:
        item = _item(10)
        with pytest.raises(ValueError, match="negativa"):
            item.ajustar_quantidade(-1)

    def test_desativar_com_reserva_e_409(self) -> None:
        item = _item(10, reservada=1)
        with pytest.raises(ViolacaoRegraDeNegocioException, match="reservada"):
            item.desativar()
        assert item.ativo

    def test_desativar_e_ativar_sao_idempotentes(self) -> None:
        item = _item(10)
        item.desativar()
        item.desativar()
        assert not item.ativo
        item.ativar()
        item.ativar()
        assert item.ativo

    def test_renomear(self) -> None:
        item = _item()
        item.renomear(" Vela nova ")
        assert item.nome == "Vela nova"


class TestReserva:
    def test_nasce_ativa(self) -> None:
        ordem_id = uuid4()
        itens = [ItemReserva(Sku("PEC-A"), 1), ItemReserva(Sku("PEC-B"), 2)]
        reserva = Reserva.criar(ordem_id=ordem_id, itens=itens, agora=AGORA)
        assert reserva.status is StatusReserva.ATIVA
        assert (reserva.ordem_id, reserva.itens) == (ordem_id, tuple(itens))
        assert (reserva.criada_em, reserva.encerrada_em) == (AGORA, None)

    def test_recusa_registra_os_faltantes_e_ja_nasce_encerrada(self) -> None:
        faltante = Faltante(sku=Sku("PEC-A"), solicitado=2, disponivel=0)
        reserva = Reserva.recusar(
            ordem_id=uuid4(),
            itens=[ItemReserva(Sku("PEC-A"), 2)],
            faltantes=[faltante],
            agora=AGORA,
        )
        assert (reserva.status, reserva.faltantes) == (
            StatusReserva.RECUSADA,
            (faltante,),
        )
        assert reserva.encerrada_em == AGORA
        assert reserva.liberar(AGORA) is False
        with pytest.raises(TransicaoStatusInvalidaException):
            reserva.consumir(AGORA)

    @pytest.mark.parametrize("status", [StatusReserva.ATIVA, StatusReserva.RECUSADA])
    def test_faltantes_so_na_recusa(self, status: StatusReserva) -> None:
        faltantes = (
            () if status is StatusReserva.RECUSADA else (Faltante(Sku("PEC-A"), 1, 0),)
        )
        with pytest.raises(ValueError, match="faltantes"):
            Reserva(
                _ordem_id=uuid4(),
                _itens=(),
                _status=status,
                _faltantes=faltantes,
                _criada_em=AGORA,
            )

    def test_lista_vazia_e_reserva_valida(self) -> None:
        reserva = Reserva.criar(ordem_id=uuid4(), itens=[], agora=AGORA)
        assert reserva.itens == ()

    def test_sku_repetido(self) -> None:
        itens = [ItemReserva(Sku("PEC-A"), 1), ItemReserva(Sku("PEC-A"), 2)]
        with pytest.raises(ValueError, match="unica vez"):
            Reserva.criar(ordem_id=uuid4(), itens=itens, agora=AGORA)

    def test_item_com_quantidade_nao_positiva(self) -> None:
        sku = Sku("PEC-A")
        with pytest.raises(ValueError, match="positiva"):
            ItemReserva(sku, 0)

    def test_liberar_e_idempotente(self) -> None:
        reserva = Reserva.criar(ordem_id=uuid4(), itens=[], agora=AGORA)
        assert reserva.liberar(AGORA) is True
        assert reserva.liberar(AGORA) is False
        assert (reserva.status, reserva.encerrada_em) == (StatusReserva.LIBERADA, AGORA)

    def test_consumida_nao_libera(self) -> None:
        reserva = Reserva.criar(ordem_id=uuid4(), itens=[], agora=AGORA)
        reserva.consumir(AGORA)
        assert reserva.status is StatusReserva.CONSUMIDA
        with pytest.raises(TransicaoStatusInvalidaException, match="CONSUMIDA"):
            reserva.liberar(AGORA)

    @pytest.mark.parametrize("encerrar", ["liberar", "consumir"])
    def test_encerrada_nao_consome(self, encerrar: str) -> None:
        reserva = Reserva.criar(ordem_id=uuid4(), itens=[], agora=AGORA)
        getattr(reserva, encerrar)(AGORA)
        with pytest.raises(TransicaoStatusInvalidaException):
            reserva.consumir(AGORA)
