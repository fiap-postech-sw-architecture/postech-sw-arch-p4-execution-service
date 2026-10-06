from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from src.compartilhado.aplicacao.outbox import dados_do_evento
from src.compartilhado.dominio.exceptions import (
    EntidadeDuplicadaException,
    TransicaoStatusInvalidaException,
    ViolacaoRegraDeNegocioException,
)
from src.estoque.aplicacao.use_cases import (
    AjustarQuantidade,
    AtualizarItemEstoque,
    CriarItemEstoque,
    DesativarItemEstoque,
    LiberarReserva,
    ListarItensEstoque,
    ObterItemEstoque,
    ReservarPecas,
)
from src.estoque.dominio.exceptions import ItemEstoqueNaoEncontradoException
from src.estoque.dominio.item_estoque import ItemEstoque
from src.estoque.dominio.reserva import ItemReserva, Reserva, StatusReserva
from src.estoque.dominio.sku import Sku
from tests.fakes import FakeUnitOfWork, ItensEmMemoria, ReservasEmMemoria

if TYPE_CHECKING:
    from collections.abc import Callable

OLEO, VELA = Sku("PEC-OLEO-5W30"), Sku("PEC-VELA")


def _item(sku: Sku, quantidade: int) -> ItemEstoque:
    return ItemEstoque.criar(sku=sku, nome=str(sku), quantidade_disponivel=quantidade)


def _eventos(uow: FakeUnitOfWork) -> list[tuple[str, dict[str, object]]]:
    return [(e.tipo, dados_do_evento(e)) for e in uow.eventos]


class TestCadastro:
    def test_criar_item(self) -> None:
        repo, uow = ItensEmMemoria(), FakeUnitOfWork()
        item = CriarItemEstoque(repo, uow).executar(
            sku=VELA, nome="Vela de ignicao", quantidade_disponivel=0
        )
        assert repo.itens[VELA] is item
        assert uow.commits == 1
        assert uow.eventos == []

    def test_sku_duplicado(self) -> None:
        uow = FakeUnitOfWork()
        uc = CriarItemEstoque(ItensEmMemoria(_item(VELA, 1)), uow)
        with pytest.raises(EntidadeDuplicadaException, match="PEC-VELA"):
            uc.executar(sku=VELA, nome="Outra", quantidade_disponivel=5)
        assert uow.commits == 0

    def test_listar_e_obter(self) -> None:
        repo = ItensEmMemoria(_item(VELA, 1), _item(OLEO, 2))
        itens, total = ListarItensEstoque(repo).executar(offset=0, limit=1)
        assert ([i.sku for i in itens], total) == ([OLEO], 2)
        assert ObterItemEstoque(repo).executar(VELA).quantidade_disponivel == 1
        uc, inexistente = ObterItemEstoque(repo), Sku("PEC-NADA")
        with pytest.raises(ItemEstoqueNaoEncontradoException):
            uc.executar(inexistente)

    def test_atualizar_renomeia_e_alterna_ativo(self) -> None:
        repo = ItensEmMemoria(_item(VELA, 1))
        uc = AtualizarItemEstoque(repo, FakeUnitOfWork())
        item = uc.executar(VELA, nome="Vela NGK", ativo=False)
        assert (item.nome, item.ativo) == ("Vela NGK", False)
        assert uc.executar(VELA, nome="Vela NGK", ativo=True).ativo

    def test_ajustar_e_desativar(self) -> None:
        repo = ItensEmMemoria(_item(VELA, 1))
        assert (
            AjustarQuantidade(repo, FakeUnitOfWork()).executar(VELA, 7).quantidade_livre
            == 7
        )
        DesativarItemEstoque(repo, FakeUnitOfWork()).executar(VELA)
        assert not repo.itens[VELA].ativo

    def test_desativar_peca_reservada_e_409(self) -> None:
        item = _item(VELA, 3)
        item.reservar(1)
        uc = DesativarItemEstoque(ItensEmMemoria(item), FakeUnitOfWork())
        with pytest.raises(ViolacaoRegraDeNegocioException):
            uc.executar(VELA)


class TestReservarPecas:
    def _cenario(
        self, *itens: ItemEstoque
    ) -> tuple[ReservarPecas, ItensEmMemoria, ReservasEmMemoria, FakeUnitOfWork]:
        repo_itens, repo_reservas, uow = (
            ItensEmMemoria(*itens),
            ReservasEmMemoria(),
            FakeUnitOfWork(),
        )
        return (
            ReservarPecas(repo_itens, repo_reservas, uow),
            repo_itens,
            repo_reservas,
            uow,
        )

    def test_sucesso_reserva_e_responde_pecas_reservadas(self) -> None:
        uc, itens, reservas, uow = self._cenario(_item(OLEO, 40), _item(VELA, 2))
        ordem_id = uuid4()
        reserva = uc.executar(ordem_id, [ItemReserva(VELA, 2), ItemReserva(OLEO, 4)])

        assert reserva is not None
        assert reservas.reservas[ordem_id] is reserva
        assert itens.itens[OLEO].quantidade_reservada == 4
        assert itens.itens[VELA].quantidade_livre == 0
        # Trava em ordem de sku, qualquer que seja a ordem do comando.
        assert itens.locks == [[OLEO, VELA]]
        assert _eventos(uow) == [
            (
                "PecasReservadas",
                {"ordem_id": str(ordem_id), "reserva_id": str(reserva.id)},
            )
        ]

    def test_falta_de_peca_nao_reserva_nada_e_lista_faltantes(self) -> None:
        uc, itens, reservas, uow = self._cenario(_item(OLEO, 40), _item(VELA, 0))
        ordem_id = uuid4()
        resultado = uc.executar(
            ordem_id,
            [ItemReserva(OLEO, 4), ItemReserva(VELA, 4), ItemReserva(Sku("PEC-X"), 1)],
        )

        assert resultado.status is StatusReserva.RECUSADA
        assert reservas.reservas == {ordem_id: resultado}
        assert itens.itens[OLEO].quantidade_reservada == 0
        assert _eventos(uow) == [
            (
                "ReservaDePecasFalhou",
                {
                    "ordem_id": str(ordem_id),
                    "faltantes": [
                        {"sku": "PEC-VELA", "solicitado": 4, "disponivel": 0},
                        {"sku": "PEC-X", "solicitado": 1, "disponivel": 0},
                    ],
                },
            )
        ]

    def test_reenvio_apos_reposicao_repete_a_recusa_sem_reservar(self) -> None:
        # O orquestrador ja compensou ao receber a recusa: uma reserva tardia
        # (estoque reposto no meio) ficaria presa sem ninguem para libera-la.
        uc, itens, _, uow = self._cenario(_item(VELA, 0))
        ordem_id = uuid4()
        uc.executar(ordem_id, [ItemReserva(VELA, 2)])
        itens.itens[VELA].ajustar_quantidade(10)

        assert uc.executar(ordem_id, [ItemReserva(VELA, 2)]).status is (
            StatusReserva.RECUSADA
        )
        assert itens.itens[VELA].quantidade_reservada == 0
        falha = (
            "ReservaDePecasFalhou",
            {
                "ordem_id": str(ordem_id),
                "faltantes": [{"sku": "PEC-VELA", "solicitado": 2, "disponivel": 0}],
            },
        )
        assert _eventos(uow) == [falha, falha]

    def test_lista_vazia_e_reserva_valida(self) -> None:
        uc, _, _, uow = self._cenario()
        ordem_id = uuid4()
        reserva = uc.executar(ordem_id, [])
        assert reserva is not None
        assert reserva.itens == ()
        assert [e.tipo for e in uow.eventos] == ["PecasReservadas"]

    def test_comando_repetido_reemite_resposta_sem_reservar_de_novo(self) -> None:
        uc, itens, _, uow = self._cenario(_item(VELA, 5))
        ordem_id = uuid4()
        primeira = uc.executar(ordem_id, [ItemReserva(VELA, 2)])
        segunda = uc.executar(ordem_id, [ItemReserva(VELA, 2)])

        assert primeira is segunda
        assert itens.itens[VELA].quantidade_reservada == 2
        assert _eventos(uow) == 2 * [
            (
                "PecasReservadas",
                {"ordem_id": str(ordem_id), "reserva_id": str(primeira.id)},
            )
        ]

    @pytest.mark.parametrize(
        "encerrar",
        [
            pytest.param(Reserva.liberar, id="liberada"),
            pytest.param(Reserva.consumir, id="consumida"),
        ],
    )
    def test_comando_atrasado_apos_compensacao_ou_baixa_e_descartado(
        self, encerrar: Callable[[Reserva, datetime], object]
    ) -> None:
        uc, itens, reservas, uow = self._cenario(_item(VELA, 5))
        ordem_id = uuid4()
        reserva = Reserva.criar(ordem_id=ordem_id, itens=[], agora=datetime.now(UTC))
        encerrar(reserva, datetime.now(UTC))
        reservas.salvar(reserva)

        assert uc.executar(ordem_id, [ItemReserva(VELA, 2)]) is reserva
        assert itens.itens[VELA].quantidade_reservada == 0
        assert (uow.eventos, uow.commits) == ([], 0)

    def test_comando_invalido_falha_antes_de_travar(self) -> None:
        uc, itens, _, uow = self._cenario(_item(VELA, 5))
        ordem_id, repetidas = uuid4(), [ItemReserva(VELA, 1), ItemReserva(VELA, 1)]
        with pytest.raises(ValueError, match="unica vez"):
            uc.executar(ordem_id, repetidas)
        assert itens.locks == []
        assert uow.eventos == []


class TestLiberarReserva:
    def test_libera_quantidades_e_responde(self) -> None:
        itens, reservas, uow = (
            ItensEmMemoria(_item(VELA, 5)),
            ReservasEmMemoria(),
            FakeUnitOfWork(),
        )
        ordem_id = uuid4()
        ReservarPecas(itens, reservas, FakeUnitOfWork()).executar(
            ordem_id, [ItemReserva(VELA, 3)]
        )

        LiberarReserva(itens, reservas, uow).executar(ordem_id)

        assert reservas.reservas[ordem_id].status is StatusReserva.LIBERADA
        assert itens.itens[VELA].quantidade_reservada == 0
        assert _eventos(uow) == [("ReservaLiberada", {"ordem_id": str(ordem_id)})]

    def test_repetida_reemite_resposta_sem_devolver_de_novo(self) -> None:
        itens, reservas = ItensEmMemoria(_item(VELA, 5)), ReservasEmMemoria()
        ordem_id = uuid4()
        ReservarPecas(itens, reservas, FakeUnitOfWork()).executar(
            ordem_id, [ItemReserva(VELA, 3)]
        )
        # Outra ordem segura 2 unidades: a segunda liberacao nao pode mexer nelas.
        ReservarPecas(itens, reservas, FakeUnitOfWork()).executar(
            uuid4(), [ItemReserva(VELA, 2)]
        )
        uow = FakeUnitOfWork()
        uc = LiberarReserva(itens, reservas, uow)

        uc.executar(ordem_id)
        uc.executar(ordem_id)

        assert itens.itens[VELA].quantidade_reservada == 2
        assert [e.tipo for e in uow.eventos] == ["ReservaLiberada", "ReservaLiberada"]

    def test_reserva_recusada_so_responde(self) -> None:
        itens, reservas = ItensEmMemoria(_item(VELA, 0)), ReservasEmMemoria()
        ordem_id = uuid4()
        ReservarPecas(itens, reservas, FakeUnitOfWork()).executar(
            ordem_id, [ItemReserva(VELA, 1)]
        )
        uow = FakeUnitOfWork()
        LiberarReserva(itens, reservas, uow).executar(ordem_id)
        assert reservas.reservas[ordem_id].status is StatusReserva.RECUSADA
        assert [e.tipo for e in uow.eventos] == ["ReservaLiberada"]

    def test_reserva_consumida_nao_volta(self) -> None:
        ordem_id = uuid4()
        reserva = Reserva.criar(ordem_id=ordem_id, itens=[], agora=datetime.now(UTC))
        reserva.consumir(datetime.now(UTC))
        uow = FakeUnitOfWork()
        uc = LiberarReserva(ItensEmMemoria(), ReservasEmMemoria(reserva), uow)
        with pytest.raises(TransicaoStatusInvalidaException):
            uc.executar(ordem_id)
        assert uow.eventos == []

    def test_compensacao_antes_do_original_grava_lapide_e_responde(self) -> None:
        reservas, uow = ReservasEmMemoria(), FakeUnitOfWork()
        ordem_id = uuid4()

        LiberarReserva(ItensEmMemoria(), reservas, uow).executar(ordem_id)

        lapide = reservas.reservas[ordem_id]
        assert (lapide.status, lapide.itens) == (StatusReserva.LIBERADA, ())
        assert _eventos(uow) == [("ReservaLiberada", {"ordem_id": str(ordem_id)})]

    def test_original_atrasado_encontra_a_lapide_e_e_descartado(self) -> None:
        itens, reservas = ItensEmMemoria(_item(VELA, 5)), ReservasEmMemoria()
        ordem_id = uuid4()
        LiberarReserva(itens, reservas, FakeUnitOfWork()).executar(ordem_id)
        lapide = reservas.reservas[ordem_id]
        uow = FakeUnitOfWork()

        resultado = ReservarPecas(itens, reservas, uow).executar(
            ordem_id, [ItemReserva(VELA, 2)]
        )

        assert resultado is lapide
        assert resultado.status is StatusReserva.LIBERADA
        assert itens.itens[VELA].quantidade_reservada == 0
        assert (uow.eventos, uow.commits) == ([], 0)

    def test_compensacao_repetida_sobre_a_lapide_so_responde(self) -> None:
        reservas, uow = ReservasEmMemoria(), FakeUnitOfWork()
        ordem_id = uuid4()
        uc = LiberarReserva(ItensEmMemoria(), reservas, uow)
        uc.executar(ordem_id)
        lapide = reservas.reservas[ordem_id]
        uc.executar(ordem_id)
        assert reservas.reservas[ordem_id] is lapide
        assert [e.tipo for e in uow.eventos] == 2 * ["ReservaLiberada"]
