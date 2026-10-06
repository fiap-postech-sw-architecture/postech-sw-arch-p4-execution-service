"""Reserva concorrente contra o Postgres real: lock pessimista, sem saldo negativo."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.estoque.aplicacao.use_cases import CriarItemEstoque, ReservarPecas
from src.estoque.dominio.reserva import ItemReserva, Reserva, StatusReserva
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.estoque.dominio.item_estoque import ItemEstoque

VELA = Sku("PEC-VELA")


class _ItensQueSeguramOLock(ItemEstoqueSQLAlchemyRepository):
    """Trava as linhas e so devolve quando o teste mandar (transacao fica aberta)."""

    def __init__(
        self, session: Session, travou: threading.Event, seguir: threading.Event
    ) -> None:
        super().__init__(session)
        self._travou = travou
        self._seguir = seguir

    def obter_com_lock(self, skus: Collection[Sku]) -> dict[Sku, ItemEstoque]:
        itens = super().obter_com_lock(skus)
        self._travou.set()
        assert self._seguir.wait(10)
        return itens


def _criar_item(session_factory: sessionmaker[Session], quantidade: int) -> None:
    with session_factory() as session:
        CriarItemEstoque(
            ItemEstoqueSQLAlchemyRepository(session),
            SQLAlchemyUnitOfWork(lambda: session),
        ).executar(sku=VELA, nome="Vela", quantidade_disponivel=quantidade)


def _saldo(session_factory: sessionmaker[Session]) -> tuple[int, int]:
    with session_factory() as session:
        item = ItemEstoqueSQLAlchemyRepository(session).obter_por_sku(VELA)
        assert item is not None
        return item.quantidade_disponivel, item.quantidade_reservada


def _esperar_alguem_bloqueado(engine: Engine, timeout: float = 10) -> None:
    limite = time.monotonic() + timeout
    with engine.connect() as conexao:
        while time.monotonic() < limite:
            esperando = conexao.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            ).scalar_one()
            if esperando:
                return
            time.sleep(0.02)
    msg = "nenhuma transacao ficou esperando o lock"
    raise AssertionError(msg)


def _disputar_ultima_unidade(
    engine: Engine,
    session_factory: sessionmaker[Session],
    ordem_a: UUID,
    ordem_b: UUID,
) -> tuple[Reserva, Reserva]:
    """A trava a ultima unidade e segura; B bloqueia no FOR UPDATE; A comita."""
    _criar_item(session_factory, 1)
    travou_a, seguir_a = threading.Event(), threading.Event()
    resultados: dict[str, Reserva] = {}
    erros: list[BaseException] = []

    def reservar(
        nome: str,
        ordem_id: UUID,
        repo_itens: Callable[[Session], ItemEstoqueSQLAlchemyRepository],
    ) -> None:
        try:
            with session_factory() as session:
                uc = ReservarPecas(
                    repo_itens(session),
                    ReservaSQLAlchemyRepository(session),
                    SQLAlchemyUnitOfWork(lambda: session),
                )
                resultados[nome] = uc.executar(ordem_id, [ItemReserva(VELA, 1)])
        except BaseException as exc:  # pragma: no cover - so aparece em regressao
            erros.append(exc)

    a = threading.Thread(
        target=reservar,
        args=("a", ordem_a, lambda s: _ItensQueSeguramOLock(s, travou_a, seguir_a)),
    )
    a.start()
    assert travou_a.wait(10)  # A tem o lock da ultima unidade e ainda nao comitou
    b = threading.Thread(
        target=reservar, args=("b", ordem_b, ItemEstoqueSQLAlchemyRepository)
    )
    b.start()
    _esperar_alguem_bloqueado(engine)  # B esta parado no SELECT ... FOR UPDATE
    seguir_a.set()
    a.join(30)
    b.join(30)
    assert erros == []
    return resultados["a"], resultados["b"]


def test_duas_reservas_disputando_a_ultima_unidade(
    engine: Engine,
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    ordem_a, ordem_b = uuid4(), uuid4()
    reserva_a, reserva_b = _disputar_ultima_unidade(
        engine, session_factory, ordem_a, ordem_b
    )

    assert reserva_a.status is StatusReserva.ATIVA
    # B so leu a linha depois do commit de A e viu o saldo atualizado.
    assert reserva_b.status is StatusReserva.RECUSADA
    assert _saldo(session_factory) == (1, 1)
    respostas = {linha["correlation_id"]: linha for linha in outbox()}
    assert respostas[ordem_a]["tipo"] == "PecasReservadas"
    assert respostas[ordem_b]["tipo"] == "ReservaDePecasFalhou"
    assert respostas[ordem_b]["dados"]["faltantes"] == [
        {"sku": "PEC-VELA", "solicitado": 1, "disponivel": 0}
    ]


def test_copias_simultaneas_do_mesmo_comando_dao_a_mesma_resposta(
    engine: Engine,
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # Reenvio do orquestrador processado em paralelo com o original: depois do
    # lock, a segunda copia ve a reserva da primeira em vez de uma falta de peca.
    ordem_id = uuid4()
    primeira, segunda = _disputar_ultima_unidade(
        engine, session_factory, ordem_id, ordem_id
    )

    assert primeira.status is StatusReserva.ATIVA
    assert segunda.id == primeira.id
    assert _saldo(session_factory) == (1, 1)
    assert [(linha["tipo"], linha["dados"]) for linha in outbox()] == 2 * [
        (
            "PecasReservadas",
            {"ordem_id": str(ordem_id), "reserva_id": str(primeira.id)},
        )
    ]


def test_muitas_reservas_simultaneas_nao_vendem_alem_do_estoque(
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    _criar_item(session_factory, 3)
    largada = threading.Barrier(10)
    erros: list[BaseException] = []

    def reservar() -> None:
        try:
            largada.wait(10)
            with session_factory() as session:
                ReservarPecas(
                    ItemEstoqueSQLAlchemyRepository(session),
                    ReservaSQLAlchemyRepository(session),
                    SQLAlchemyUnitOfWork(lambda: session),
                ).executar(uuid4(), [ItemReserva(VELA, 1)])
        except BaseException as exc:  # pragma: no cover - so aparece em regressao
            erros.append(exc)

    threads = [threading.Thread(target=reservar) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert erros == []
    assert _saldo(session_factory) == (3, 3)
    tipos = sorted(linha["tipo"] for linha in outbox())
    assert tipos == 3 * ["PecasReservadas"] + 7 * ["ReservaDePecasFalhou"]
