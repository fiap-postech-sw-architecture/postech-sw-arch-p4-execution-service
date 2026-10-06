from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from src.compartilhado.aplicacao.idempotencia import releitura_em_corrida
from src.compartilhado.dominio.exceptions import EntidadeDuplicadaException
from src.estoque.aplicacao.events import (
    FaltanteDTO,
    PecasReservadasEvent,
    ReservaDePecasFalhouEvent,
    ReservaLiberadaEvent,
)
from src.estoque.dominio.exceptions import ItemEstoqueNaoEncontradoException
from src.estoque.dominio.item_estoque import ItemEstoque
from src.estoque.dominio.reserva import Reserva, StatusReserva
from src.estoque.dominio.services import (
    calcular_faltantes,
    consumir,
    liberar,
    reservar,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.estoque.dominio.repository import ItemEstoqueRepository, ReservaRepository
    from src.estoque.dominio.reserva import ItemReserva
    from src.estoque.dominio.sku import Sku

_log = structlog.get_logger(__name__)


def _obter_item(
    repo: ItemEstoqueRepository, sku: Sku, *, com_lock: bool = False
) -> ItemEstoque:
    item = repo.obter_por_sku(sku, com_lock=com_lock)
    if item is None:
        raise ItemEstoqueNaoEncontradoException(sku)
    return item


class CriarItemEstoque:
    def __init__(self, repo: ItemEstoqueRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(
        self, *, sku: Sku, nome: str, quantidade_disponivel: int
    ) -> ItemEstoque:
        item = ItemEstoque.criar(
            sku=sku, nome=nome, quantidade_disponivel=quantidade_disponivel
        )
        with self._uow:
            if self._repo.obter_por_sku(sku) is not None:
                msg = f"Ja existe item de estoque com SKU {sku}"
                raise EntidadeDuplicadaException(msg)
            self._repo.salvar(item)
            self._uow.commit()
        return item


class ListarItensEstoque:
    def __init__(self, repo: ItemEstoqueRepository) -> None:
        self._repo = repo

    def executar(self, offset: int, limit: int) -> tuple[list[ItemEstoque], int]:
        """Pagina de itens (ordem de sku) e o total cadastrado."""
        return self._repo.listar(offset=offset, limit=limit), self._repo.contar()


class ConsultarItemEstoque:
    def __init__(self, repo: ItemEstoqueRepository) -> None:
        self._repo = repo

    def executar(self, sku: Sku) -> ItemEstoque:
        return _obter_item(self._repo, sku)


class AtualizarItemEstoque:
    def __init__(self, repo: ItemEstoqueRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(self, sku: Sku, *, nome: str, ativo: bool) -> ItemEstoque:
        with self._uow:
            item = _obter_item(self._repo, sku, com_lock=True)
            item.renomear(nome)
            if ativo:
                item.ativar()
            else:
                item.desativar()
            self._repo.salvar(item)
            self._uow.commit()
        return item


class AjustarQuantidade:
    def __init__(self, repo: ItemEstoqueRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(self, sku: Sku, quantidade_disponivel: int) -> ItemEstoque:
        with self._uow:
            # Lock: serializa com reservas concorrentes (sem lost update no saldo).
            item = _obter_item(self._repo, sku, com_lock=True)
            item.ajustar_quantidade(quantidade_disponivel)
            self._repo.salvar(item)
            self._uow.commit()
        return item


class DesativarItemEstoque:
    def __init__(self, repo: ItemEstoqueRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(self, sku: Sku) -> None:
        with self._uow:
            item = _obter_item(self._repo, sku, com_lock=True)
            item.desativar()
            self._repo.salvar(item)
            self._uow.commit()


class ReservarPecas:
    """Comando ``ReservarPecas`` (passo T5 da saga), idempotente por ordem.

    Responde ``PecasReservadas`` ou ``ReservaDePecasFalhou{faltantes}``; com
    falta de qualquer peca nada e separado e a recusa fica registrada. Regra de
    repeticao: enquanto o desfecho vale (reserva ATIVA ou RECUSADA), o comando
    repetido recebe a mesma resposta, sem decidir de novo (uma reposicao de
    estoque no meio do caminho nao vira reserva tardia); depois de compensado
    ou superado (LIBERADA, inclusive a lapide, ou CONSUMIDA), e descartado sem
    efeito e sem resposta.

    Copias simultaneas do mesmo comando: a existencia da reserva e conferida
    DEPOIS de travar os itens, entao a segunda copia ve a decisao da primeira;
    sem itens para travar (lista vazia), a UNIQUE(ordem_id) barra a segunda,
    que roda de novo e cai na regra de repeticao (``releitura_em_corrida``).
    """

    def __init__(
        self,
        itens: ItemEstoqueRepository,
        reservas: ReservaRepository,
        uow: UnitOfWork,
    ) -> None:
        self._itens = itens
        self._reservas = reservas
        self._uow = uow

    @releitura_em_corrida
    def executar(self, ordem_id: UUID, pecas: Sequence[ItemReserva]) -> Reserva:
        """Devolve a decisao da ordem: reserva ATIVA, RECUSADA ou ja existente."""
        agora = datetime.now(UTC)
        # Valida o comando (sku repetido, quantidade) antes de travar linhas.
        nova = Reserva.criar(ordem_id=ordem_id, itens=pecas, agora=agora)
        with self._uow:
            itens = self._itens.obter_com_lock([linha.sku for linha in nova.itens])
            existente = self._reservas.obter_por_ordem(ordem_id)
            if existente is not None:
                self._responder_de_novo(existente)
                return existente
            faltantes = calcular_faltantes(nova.itens, itens)
            if faltantes:
                recusada = Reserva.recusar(
                    ordem_id=ordem_id,
                    itens=nova.itens,
                    faltantes=faltantes,
                    agora=agora,
                )
                self._reservas.salvar(recusada)
                self._uow.registrar_evento(_falha(recusada, agora))
                self._uow.commit()
                _log.info(
                    "parts_reservation_refused",
                    correlation_id=str(ordem_id),
                    skus_em_falta=[str(f.sku) for f in faltantes],
                )
                return recusada
            reservar(nova, itens)
            self._reservas.salvar(nova)
            self._uow.registrar_evento(
                PecasReservadasEvent(
                    ordem_id=ordem_id, ocorrido_em=agora, reserva_id=nova.id
                )
            )
            self._uow.commit()
        return nova

    def _responder_de_novo(self, existente: Reserva) -> None:
        if existente.status in _RESERVA_ENCERRADA:
            _log.info(
                "late_command_discarded",
                comando="ReservarPecas",
                correlation_id=str(existente.ordem_id),
                status=existente.status,
            )
            return
        if existente.status is StatusReserva.RECUSADA:
            self._uow.registrar_evento(_falha(existente, datetime.now(UTC)))
        else:
            self._uow.registrar_evento(
                PecasReservadasEvent(
                    ordem_id=existente.ordem_id, reserva_id=existente.id
                )
            )
        self._uow.commit()


# Compensada (inclusive a lapide) ou superada pela baixa: o comando atrasado
# nao tem mais desfecho a republicar.
_RESERVA_ENCERRADA = frozenset({StatusReserva.LIBERADA, StatusReserva.CONSUMIDA})


def _falha(recusada: Reserva, agora: datetime) -> ReservaDePecasFalhouEvent:
    return ReservaDePecasFalhouEvent(
        ordem_id=recusada.ordem_id,
        ocorrido_em=agora,
        faltantes=tuple(
            FaltanteDTO(
                sku=str(f.sku), solicitado=f.solicitado, disponivel=f.disponivel
            )
            for f in recusada.faltantes
        ),
    )


class LiberarReserva:
    """Comando de compensacao ``LiberarReserva``, idempotente por ordem.

    Devolve as quantidades reservadas e responde ``ReservaLiberada``; repetido,
    ou para reserva recusada (nada separado), so responde. Reserva ja consumida
    (execucao finalizada) nao volta: 409. Ordem sem reserva (o ``ReservarPecas``
    ainda em voo): grava a lapide, uma reserva ja LIBERADA e sem pecas, e
    responde; o comando original, quando chegar, e descartado. O ``motivo`` do
    comando nao e usado aqui: o historico da saga fica no OS Service.
    """

    def __init__(
        self,
        itens: ItemEstoqueRepository,
        reservas: ReservaRepository,
        uow: UnitOfWork,
    ) -> None:
        self._itens = itens
        self._reservas = reservas
        self._uow = uow

    @releitura_em_corrida
    def executar(self, ordem_id: UUID) -> None:
        agora = datetime.now(UTC)
        with self._uow:
            reserva = self._reservas.obter_por_ordem(ordem_id, com_lock=True)
            if reserva is None:
                self._reservas.salvar(Reserva.lapide(ordem_id=ordem_id, agora=agora))
                _log.info(
                    "compensation_tombstone_recorded",
                    comando="LiberarReserva",
                    correlation_id=str(ordem_id),
                )
            else:
                itens = self._itens.obter_com_lock(
                    [linha.sku for linha in reserva.itens]
                )
                liberar(reserva, itens, agora)
            self._uow.registrar_evento(
                ReservaLiberadaEvent(ordem_id=ordem_id, ocorrido_em=agora)
            )
            self._uow.commit()
        _log.info("reservation_released", correlation_id=str(ordem_id))


def reserva_ativa(reservas: ReservaRepository, ordem_id: UUID) -> bool:
    """A ordem tem pecas separadas esperando a baixa.

    Trava a reserva (FOR UPDATE) ate o fim da transacao de quem chama: uma
    liberacao concorrente nao some no meio do agendamento ou do inicio.
    """
    reserva = reservas.obter_por_ordem(ordem_id, com_lock=True)
    return reserva is not None and reserva.status is StatusReserva.ATIVA


def baixar_reserva(
    itens: ItemEstoqueRepository,
    reservas: ReservaRepository,
    ordem_id: UUID,
    agora: datetime,
) -> Reserva | None:
    """Consome a reserva da ordem na transacao de quem chama (sem commit).

    Trava a reserva e depois os itens em ordem de sku, a mesma ordem de lock
    da liberacao. ``None`` quando a ordem nao tem reserva.

    Raises:
        TransicaoStatusInvalidaException: reserva liberada, recusada ou ja
            consumida.
    """
    reserva = reservas.obter_por_ordem(ordem_id, com_lock=True)
    if reserva is None:
        return None
    consumir(
        reserva, itens.obter_com_lock([linha.sku for linha in reserva.itens]), agora
    )
    return reserva
