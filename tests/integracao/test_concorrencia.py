"""Concorrencia contra o Postgres real: cada lock pessimista tem um teste.

Padrao: a primeira transacao trava as linhas e segura (``_ItensQueSeguramOLock``);
a segunda roda ate bloquear no lock (``pg_stat_activity``) ou terminar; so entao
a primeira comita. Sem o lock, a segunda leria o valor antigo e o saldo final
sairia errado (ou viraria erro de CHECK no banco em vez do 409 do dominio).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from src.compartilhado.dominio.exceptions import (
    TransicaoStatusInvalidaException,
    ViolacaoRegraDeNegocioException,
)
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.estoque.aplicacao.use_cases import (
    AjustarQuantidade,
    CriarItemEstoque,
    DesativarItemEstoque,
    LiberarReserva,
    ReservarPecas,
)
from src.estoque.dominio.reserva import ItemReserva, Reserva, StatusReserva
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)
from src.execucao.aplicacao.use_cases import (
    AgendarExecucao,
    FinalizarExecucao,
    IniciarExecucao,
)
from src.execucao.dominio.execucao import Prioridade, StatusExecucao
from src.execucao.infraestrutura.adapters import (
    EstoqueSQLAlchemyAdapter,
    VeiculosSQLAlchemy,
)
from src.execucao.infraestrutura.repository import (
    ExecucaoSQLAlchemyRepository,
    FilaDeExecucaoSQLAlchemy,
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


def _uow(session: Session) -> SQLAlchemyUnitOfWork:
    return SQLAlchemyUnitOfWork(lambda: session)


def _criar_item(session_factory: sessionmaker[Session], quantidade: int) -> None:
    with session_factory() as session:
        CriarItemEstoque(
            ItemEstoqueSQLAlchemyRepository(session),
            SQLAlchemyUnitOfWork(lambda: session),
        ).executar(sku=VELA, nome="Vela", quantidade_disponivel=quantidade)


def _reservar(
    session_factory: sessionmaker[Session],
    ordem_id: UUID,
    quantidade: int,
    itens: Callable[[Session], ItemEstoqueSQLAlchemyRepository] = (
        ItemEstoqueSQLAlchemyRepository
    ),
) -> Reserva:
    with session_factory() as session:
        return ReservarPecas(
            itens(session), ReservaSQLAlchemyRepository(session), _uow(session)
        ).executar(ordem_id, [ItemReserva(VELA, quantidade)])


def _liberar(
    session_factory: sessionmaker[Session],
    ordem_id: UUID,
    itens: Callable[[Session], ItemEstoqueSQLAlchemyRepository] = (
        ItemEstoqueSQLAlchemyRepository
    ),
) -> None:
    with session_factory() as session:
        LiberarReserva(
            itens(session), ReservaSQLAlchemyRepository(session), _uow(session)
        ).executar(ordem_id)


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


def _esperar_bloqueio_ou_fim(engine: Engine, disputa: threading.Thread) -> None:
    """A segunda transacao parou no lock ou (sem lock, num mutante) ja terminou."""
    limite = time.monotonic() + 10
    with engine.connect() as conexao:
        while disputa.is_alive() and time.monotonic() < limite:
            esperando = conexao.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            ).scalar_one()
            if esperando:
                return
            time.sleep(0.02)
    assert not disputa.is_alive(), "a segunda transacao nem bloqueou nem terminou"


def _disputar(
    engine: Engine,
    segura: Callable[[threading.Event, threading.Event], object],
    disputa: Callable[[], object],
) -> dict[str, BaseException]:
    """``segura`` trava e espera; ``disputa`` corre ate bloquear; os dois terminam.

    Devolve as excecoes de cada lado (``segura``/``disputa``), para o teste
    conferir quem perdeu e com que erro.
    """
    travou, seguir = threading.Event(), threading.Event()
    erros: dict[str, BaseException] = {}

    def rodar(nome: str, alvo: Callable[..., object], *args: object) -> None:
        try:
            alvo(*args)
        except BaseException as exc:  # o teste confere o tipo de cada lado
            erros[nome] = exc

    primeira = threading.Thread(target=rodar, args=("segura", segura, travou, seguir))
    primeira.start()
    assert travou.wait(10)
    segunda = threading.Thread(target=rodar, args=("disputa", disputa))
    segunda.start()
    _esperar_bloqueio_ou_fim(engine, segunda)
    seguir.set()
    primeira.join(30)
    segunda.join(30)
    return erros


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


def _segurando(
    travou: threading.Event, seguir: threading.Event
) -> Callable[[Session], ItemEstoqueSQLAlchemyRepository]:
    return lambda session: _ItensQueSeguramOLock(session, travou, seguir)


def test_liberacoes_simultaneas_da_mesma_ordem_nao_devolvem_unidades_de_outra(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Sem o FOR UPDATE da reserva, a segunda liberacao leria a reserva ainda
    # ATIVA e devolveria de novo as 2 unidades, que entao sairiam da ordem B.
    _criar_item(session_factory, 4)
    ordem_a, ordem_b = uuid4(), uuid4()
    _reservar(session_factory, ordem_a, 2)
    _reservar(session_factory, ordem_b, 2)

    erros = _disputar(
        engine,
        lambda travou, seguir: _liberar(
            session_factory, ordem_a, _segurando(travou, seguir)
        ),
        lambda: _liberar(session_factory, ordem_a),
    )

    assert erros == {}
    assert _saldo(session_factory) == (4, 2)


def test_liberacao_e_reserva_simultaneas_nao_perdem_a_reserva_nova(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Sem o FOR UPDATE dos itens na liberacao, a reserva de C comitaria no meio
    # e a liberacao gravaria o reservado antigo menos 3, apagando as 2 de C.
    _criar_item(session_factory, 10)
    ordem_a, ordem_b, ordem_c = uuid4(), uuid4(), uuid4()
    _reservar(session_factory, ordem_a, 3)
    _reservar(session_factory, ordem_b, 2)

    erros = _disputar(
        engine,
        lambda travou, seguir: _liberar(
            session_factory, ordem_a, _segurando(travou, seguir)
        ),
        lambda: _reservar(session_factory, ordem_c, 2),
    )

    assert erros == {}
    assert _saldo(session_factory) == (10, 4)  # B (2) + C (2)


def test_finalizacao_e_liberacao_simultaneas_nao_baixam_reserva_liberada(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # A liberacao vence: a finalizacao espera no FOR UPDATE da reserva, rele
    # LIBERADA e recusa (409). Sem o lock ela baixaria a reserva ja devolvida,
    # comendo as unidades da ordem B.
    _criar_item(session_factory, 4)
    ordem_a, ordem_b, mecanico = uuid4(), uuid4(), uuid4()
    _reservar(session_factory, ordem_a, 2)
    _reservar(session_factory, ordem_b, 2)
    with session_factory() as session:
        AgendarExecucao(
            ExecucaoSQLAlchemyRepository(session),
            FilaDeExecucaoSQLAlchemy(session),
            VeiculosSQLAlchemy(session),
            _uow(session),
        ).executar(ordem_a, Prioridade.NORMAL)
    with session_factory() as session:
        IniciarExecucao(
            ExecucaoSQLAlchemyRepository(session),
            EstoqueSQLAlchemyAdapter(session),
            _uow(session),
        ).executar(ordem_a, mecanico)

    def finalizar() -> None:
        with session_factory() as session:
            FinalizarExecucao(
                ExecucaoSQLAlchemyRepository(session),
                EstoqueSQLAlchemyAdapter(session),
                _uow(session),
            ).executar(ordem_a, mecanico)

    erros = _disputar(
        engine,
        lambda travou, seguir: _liberar(
            session_factory, ordem_a, _segurando(travou, seguir)
        ),
        finalizar,
    )

    assert list(erros) == ["disputa"]
    assert isinstance(erros["disputa"], TransicaoStatusInvalidaException)
    assert _saldo(session_factory) == (4, 2)
    with session_factory() as session:
        execucao = ExecucaoSQLAlchemyRepository(session).obter(ordem_a)
    assert execucao is not None
    assert execucao.status is StatusExecucao.EM_EXECUCAO


@pytest.mark.parametrize(
    "comando",
    [
        pytest.param(
            lambda repo, uow: AjustarQuantidade(repo, uow).executar(VELA, 0),
            id="ajuste-abaixo-do-reservado",
        ),
        pytest.param(
            lambda repo, uow: DesativarItemEstoque(repo, uow).executar(VELA),
            id="desativacao-com-reserva",
        ),
    ],
)
def test_escrita_do_admin_espera_a_reserva_e_ve_o_reservado(
    engine: Engine,
    session_factory: sessionmaker[Session],
    comando: Callable[[ItemEstoqueSQLAlchemyRepository, SQLAlchemyUnitOfWork], object],
) -> None:
    # O FOR UPDATE de obter_por_sku faz o admin esperar a reserva em curso e
    # reler o reservado: 409 do dominio. Sem ele, o ajuste viraria erro de CHECK
    # no banco (500) e a desativacao deixaria peca inativa com reserva ativa.
    _criar_item(session_factory, 1)

    def admin() -> None:
        with session_factory() as session:
            comando(ItemEstoqueSQLAlchemyRepository(session), _uow(session))

    erros = _disputar(
        engine,
        lambda travou, seguir: _reservar(
            session_factory, uuid4(), 1, _segurando(travou, seguir)
        ),
        admin,
    )

    assert list(erros) == ["disputa"]
    assert isinstance(erros["disputa"], ViolacaoRegraDeNegocioException)
    assert _saldo(session_factory) == (1, 1)
    with session_factory() as session:
        item = ItemEstoqueSQLAlchemyRepository(session).obter_por_sku(VELA)
    assert item is not None
    assert item.ativo
