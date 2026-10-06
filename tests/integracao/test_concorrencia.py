"""Concorrencia contra o Postgres real: cada lock pessimista tem um teste.

Padrao: a primeira transacao trava as linhas e segura (``_ItensQueSeguramOLock``);
a segunda roda ate bloquear no lock (``pg_stat_activity``) ou terminar; so entao
a primeira comita. Sem o lock, a segunda leria o valor antigo e o saldo final
sairia errado (ou viraria erro de CHECK no banco em vez do 409 do dominio).
"""

from __future__ import annotations

import io
import json
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
from src.compartilhado.dominio.veiculo import Veiculo
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.diagnostico.aplicacao.use_cases import (
    DescartarDiagnostico,
    RegistrarSolicitacaoDeDiagnostico,
)
from src.diagnostico.infraestrutura.repository import DiagnosticoSQLAlchemyRepository
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
from src.estoque.infraestrutura.seed import ITENS_DEMO, semear
from src.execucao.aplicacao.use_cases import (
    AgendarExecucao,
    CancelarExecucao,
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
    from src.execucao.dominio.execucao import Execucao

VELA = Sku("PEC-VELA")
# Teto das esperas do teste: so estoura em regressao (num CI lento a transacao
# pode levar segundos para chegar ao lock).
_ESPERA_MAXIMA_S = 30


def _pausar(travou: threading.Event, seguir: threading.Event) -> None:
    """A transacao ja travou a linha: avisa o teste e espera ele mandar seguir."""
    travou.set()
    assert seguir.wait(_ESPERA_MAXIMA_S)


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
        _pausar(self._travou, self._seguir)
        return itens


class _ExecucoesQueSeguramOLock(ExecucaoSQLAlchemyRepository):
    """Trava a execucao (``FOR UPDATE``) e so devolve quando o teste mandar."""

    def __init__(
        self, session: Session, travou: threading.Event, seguir: threading.Event
    ) -> None:
        super().__init__(session)
        self._travou = travou
        self._seguir = seguir

    def obter(self, ordem_id: UUID, *, com_lock: bool = False) -> Execucao | None:
        execucao = super().obter(ordem_id, com_lock=com_lock)
        if com_lock:
            _pausar(self._travou, self._seguir)
        return execucao


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


def _esperar_bloqueio_ou_fim(engine: Engine, disputa: threading.Thread) -> None:
    """A segunda transacao parou no lock ou (sem lock, num mutante) ja terminou.

    Cada leitura do ``pg_stat_activity`` numa transacao propria: dentro da mesma
    transacao o Postgres devolve o retrato da primeira leitura, e uma espera que
    comecasse depois dela nunca apareceria (o teste falhava em CI lento).
    """
    limite = time.monotonic() + _ESPERA_MAXIMA_S
    with engine.connect() as conexao:
        while disputa.is_alive() and time.monotonic() < limite:
            esperando = conexao.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            ).scalar_one()
            conexao.rollback()  # o proximo SELECT ve um retrato novo
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
    assert travou.wait(_ESPERA_MAXIMA_S)
    segunda = threading.Thread(target=rodar, args=("disputa", disputa))
    segunda.start()
    _esperar_bloqueio_ou_fim(engine, segunda)
    seguir.set()
    primeira.join(_ESPERA_MAXIMA_S)
    segunda.join(_ESPERA_MAXIMA_S)
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
    assert travou_a.wait(
        _ESPERA_MAXIMA_S
    )  # A tem o lock da ultima unidade e ainda nao comitou
    b = threading.Thread(
        target=reservar, args=("b", ordem_b, ItemEstoqueSQLAlchemyRepository)
    )
    b.start()
    # B parado no SELECT ... FOR UPDATE (ou ja terminado, se o lock sumir num
    # mutante: entao os asserts de saldo e de erros abaixo apontam o defeito).
    _esperar_bloqueio_ou_fim(engine, b)
    seguir_a.set()
    a.join(_ESPERA_MAXIMA_S)
    b.join(_ESPERA_MAXIMA_S)
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
            largada.wait(_ESPERA_MAXIMA_S)
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
        thread.join(_ESPERA_MAXIMA_S)

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
            EstoqueSQLAlchemyAdapter(session),
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


def _com_insercao_pendente(
    engine: Engine,
    insercao: str,
    parametros: dict[str, object],
    disputa: Callable[[], object],
) -> dict[str, BaseException]:
    """Outra transacao ja inseriu a mesma chave e nao comitou.

    ``disputa`` le (nao ve a linha), insere e bloqueia no indice unico; so entao
    a primeira comita, e a disputa recebe a violacao de unicidade.
    """
    erros: dict[str, BaseException] = {}

    def rodar() -> None:
        try:
            disputa()
        except BaseException as exc:  # o teste confere o tipo
            erros["disputa"] = exc

    with engine.connect() as primeira:
        primeira.execute(text(insercao), parametros)
        segunda = threading.Thread(target=rodar)
        segunda.start()
        _esperar_bloqueio_ou_fim(engine, segunda)
        primeira.commit()
        segunda.join(_ESPERA_MAXIMA_S)
    return erros


def test_seed_de_duas_replicas_ao_mesmo_tempo_nao_derruba_o_boot(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    criados: list[str] = []
    erros = _com_insercao_pendente(
        engine,
        "INSERT INTO itens_estoque (id, sku, nome, quantidade_disponivel, "
        "quantidade_reservada, ativo) VALUES "
        "(gen_random_uuid(), 'PEC-OLEO-5W30', 'Oleo', 99, 0, true)",
        {},
        lambda: criados.extend(semear(session_factory)),
    )

    assert erros == {}
    assert criados == [sku for sku, _, _ in ITENS_DEMO if sku != "PEC-OLEO-5W30"]
    with session_factory() as session:
        oleo = ItemEstoqueSQLAlchemyRepository(session).obter_por_sku(
            Sku("PEC-OLEO-5W30")
        )
    assert oleo is not None
    assert oleo.quantidade_disponivel == 99  # o da outra replica, intocado


_DIAGNOSTICO_PENDENTE = (
    "INSERT INTO diagnosticos (ordem_id, status, veiculo, descricao_problema, "
    "itens, observacoes, solicitado_em) VALUES (:ordem_id, 'AGUARDANDO', "
    """'{"veiculo_id": "4a7c7a6e-1b6f-4bb0-9a49-5d1b3f0c2e11", "placa": "ABC1D23", """
    """"marca": "Fiat", "modelo": "Uno", "ano": 2015}', 'Nao liga', '[]', '', now())"""
)
_EXECUCAO_NA_FILA = (
    "INSERT INTO execucoes (ordem_id, status, prioridade, enfileirada_em) "
    "VALUES (:ordem_id, 'AGUARDANDO', 'normal', now())"
)
_RESERVA_SEM_PECAS = (
    "INSERT INTO reservas (id, ordem_id, status, itens, faltantes, criada_em) "
    "VALUES (gen_random_uuid(), :ordem_id, 'ATIVA', '[]', '[]', now())"
)


def _solicitar_diagnostico(session: Session, ordem_id: UUID) -> object:
    veiculo = Veiculo(
        veiculo_id=uuid4(), placa="ABC1D23", marca="Fiat", modelo="Uno", ano=2015
    )
    return RegistrarSolicitacaoDeDiagnostico(
        DiagnosticoSQLAlchemyRepository(session), _uow(session)
    ).executar(ordem_id, veiculo, "Nao liga")


def _descartar_diagnostico(session: Session, ordem_id: UUID) -> object:
    return DescartarDiagnostico(
        DiagnosticoSQLAlchemyRepository(session), _uow(session)
    ).executar(ordem_id)


def _agendar_execucao(session: Session, ordem_id: UUID) -> object:
    return AgendarExecucao(
        ExecucaoSQLAlchemyRepository(session),
        FilaDeExecucaoSQLAlchemy(session),
        VeiculosSQLAlchemy(session),
        EstoqueSQLAlchemyAdapter(session),
        _uow(session),
    ).executar(ordem_id, Prioridade.NORMAL)


def _cancelar_execucao(session: Session, ordem_id: UUID) -> object:
    return CancelarExecucao(
        ExecucaoSQLAlchemyRepository(session), _uow(session)
    ).executar(ordem_id)


def _reservar_sem_pecas(session: Session, ordem_id: UUID) -> object:
    return ReservarPecas(
        ItemEstoqueSQLAlchemyRepository(session),
        ReservaSQLAlchemyRepository(session),
        _uow(session),
    ).executar(ordem_id, [])


def _liberar_reserva(session: Session, ordem_id: UUID) -> object:
    return LiberarReserva(
        ItemEstoqueSQLAlchemyRepository(session),
        ReservaSQLAlchemyRepository(session),
        _uow(session),
    ).executar(ordem_id)


# Log de cada compensacao (depois do commit): a lapide que perdeu a corrida nao
# pode aparecer como gravada.
_LOG_DA_COMPENSACAO = {
    "DiagnosticoDescartado": "diagnosis_discarded",
    "ExecucaoCancelada": "execution_cancelled",
    "ReservaLiberada": "reservation_released",
}


@pytest.mark.parametrize(
    ("insercao", "comando", "tabela", "status", "respostas"),
    [
        pytest.param(
            _DIAGNOSTICO_PENDENTE,
            _solicitar_diagnostico,
            "diagnosticos",
            "AGUARDANDO",
            [],
            id="solicitar-diagnostico",
        ),
        pytest.param(
            _DIAGNOSTICO_PENDENTE,
            _descartar_diagnostico,
            "diagnosticos",
            "DESCARTADO",
            ["DiagnosticoDescartado"],
            id="descarte-contra-a-solicitacao",
        ),
        pytest.param(
            _EXECUCAO_NA_FILA,
            _agendar_execucao,
            "execucoes",
            "AGUARDANDO",
            ["ExecucaoAgendada"],
            id="agendar-execucao",
        ),
        pytest.param(
            _EXECUCAO_NA_FILA,
            _cancelar_execucao,
            "execucoes",
            "CANCELADA",
            ["ExecucaoCancelada"],
            id="cancelamento-contra-o-agendamento",
        ),
        pytest.param(
            _RESERVA_SEM_PECAS,
            _reservar_sem_pecas,
            "reservas",
            "ATIVA",
            ["PecasReservadas"],
            id="reservar-sem-pecas",
        ),
        pytest.param(
            _RESERVA_SEM_PECAS,
            _liberar_reserva,
            "reservas",
            "LIBERADA",
            ["ReservaLiberada"],
            id="liberacao-contra-a-reserva",
        ),
    ],
)
def test_copia_simultanea_que_perde_a_corrida_le_a_vencedora(
    engine: Engine,
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
    insercao: str,
    comando: Callable[[Session, UUID], object],
    tabela: str,
    status: str,
    respostas: list[str],
    log_capturado: io.StringIO,
) -> None:
    # A outra copia (ou o comando original, contra a lapide) ja inseriu a linha
    # da ordem e nao comitou: esta le "nada", insere e bate na UNIQUE. Antes
    # isso era IntegrityError (500 ou mensagem na DLQ); agora roda de novo e
    # segue a regra de repeticao sobre a linha da vencedora.
    ordem_id = uuid4()
    if comando is _agendar_execucao:
        with session_factory() as session:  # RN-027: so agenda com reserva ativa
            _reservar_sem_pecas(session, ordem_id)

    def disputar() -> None:
        with session_factory() as session:
            comando(session, ordem_id)

    antes = len(outbox())
    erros = _com_insercao_pendente(engine, insercao, {"ordem_id": ordem_id}, disputar)

    assert erros == {}
    with engine.connect() as conexao:
        linhas = conexao.execute(
            text(f"SELECT status FROM {tabela} WHERE ordem_id = :id"),  # noqa: S608 - tabela fixa do teste
            {"id": ordem_id},
        ).all()
    assert [linha.status for linha in linhas] == [status]
    assert [linha["tipo"] for linha in outbox()[antes:]] == respostas
    registros = [json.loads(linha) for linha in log_capturado.getvalue().splitlines()]
    for resposta in respostas:
        if resposta in _LOG_DA_COMPENSACAO:
            [compensacao] = [
                r for r in registros if r["event"] == _LOG_DA_COMPENSACAO[resposta]
            ]
            assert compensacao["tombstone"] is False  # leu a linha da vencedora


def _execucoes_que_seguram(
    travou: threading.Event, seguir: threading.Event
) -> Callable[[Session], ExecucaoSQLAlchemyRepository]:
    return lambda session: _ExecucoesQueSeguramOLock(session, travou, seguir)


def _execucao_na_fila(session_factory: sessionmaker[Session]) -> UUID:
    """Execucao AGUARDANDO de uma ordem com a reserva de pecas ATIVA (RN-027)."""
    _criar_item(session_factory, 1)
    ordem_id = uuid4()
    _reservar(session_factory, ordem_id, 1)
    with session_factory() as session:
        _agendar_execucao(session, ordem_id)
    return ordem_id


def _iniciar_execucao(
    session_factory: sessionmaker[Session],
    ordem_id: UUID,
    mecanico_id: UUID,
    execucoes: Callable[[Session], ExecucaoSQLAlchemyRepository] = (
        ExecucaoSQLAlchemyRepository
    ),
) -> None:
    with session_factory() as session:
        IniciarExecucao(
            execucoes(session), EstoqueSQLAlchemyAdapter(session), _uow(session)
        ).executar(ordem_id, mecanico_id)


def _cancelar(
    session_factory: sessionmaker[Session],
    ordem_id: UUID,
    execucoes: Callable[[Session], ExecucaoSQLAlchemyRepository] = (
        ExecucaoSQLAlchemyRepository
    ),
) -> None:
    with session_factory() as session:
        CancelarExecucao(execucoes(session), _uow(session)).executar(ordem_id)


def _execucao(session_factory: sessionmaker[Session], ordem_id: UUID) -> Execucao:
    with session_factory() as session:
        execucao = ExecucaoSQLAlchemyRepository(session).obter(ordem_id)
    assert execucao is not None
    return execucao


@pytest.mark.parametrize(
    ("segura", "status"),
    [
        pytest.param("inicio", StatusExecucao.EM_EXECUCAO, id="inicio-primeiro"),
        pytest.param(
            "cancelamento", StatusExecucao.CANCELADA, id="cancelamento-primeiro"
        ),
    ],
)
def test_inicio_e_cancelamento_simultaneos_deixam_so_o_que_chegou_primeiro(
    engine: Engine,
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
    segura: str,
    status: StatusExecucao,
) -> None:
    # Pivot da saga: quem trava a execucao primeiro decide e o outro, depois de
    # esperar, rele o estado e recebe 409. Sem o FOR UPDATE os dois leriam
    # AGUARDANDO e os dois comitariam: a execucao sairia iniciada com um
    # ExecucaoCancelada na outbox, ou cancelada com mecanico.
    ordem_id, mecanico = _execucao_na_fila(session_factory), uuid4()
    antes = len(outbox())

    def inicio(execucoes: Callable[[Session], ExecucaoSQLAlchemyRepository]) -> None:
        _iniciar_execucao(session_factory, ordem_id, mecanico, execucoes)

    def cancelamento(
        execucoes: Callable[[Session], ExecucaoSQLAlchemyRepository],
    ) -> None:
        _cancelar(session_factory, ordem_id, execucoes)

    primeira, segunda = (
        (inicio, cancelamento) if segura == "inicio" else (cancelamento, inicio)
    )
    erros = _disputar(
        engine,
        lambda travou, seguir: primeira(_execucoes_que_seguram(travou, seguir)),
        lambda: segunda(ExecucaoSQLAlchemyRepository),
    )

    assert list(erros) == ["disputa"]
    assert isinstance(erros["disputa"], TransicaoStatusInvalidaException)
    execucao = _execucao(session_factory, ordem_id)
    assert execucao.status is status
    iniciada = status is StatusExecucao.EM_EXECUCAO
    assert execucao.mecanico_id == (mecanico if iniciada else None)
    assert [linha["tipo"] for linha in outbox()[antes:]] == [
        "ExecucaoIniciada" if iniciada else "ExecucaoCancelada"
    ]


def test_inicios_simultaneos_de_dois_mecanicos_deixam_so_o_primeiro(
    engine: Engine,
    session_factory: sessionmaker[Session],
    outbox: Callable[[], list[dict[str, Any]]],
) -> None:
    # O segundo, depois de esperar o lock, rele EM_EXECUCAO do primeiro e recebe
    # 409. Sem o FOR UPDATE os dois iniciariam: dois ExecucaoIniciada com
    # mecanicos diferentes, e o ultimo a gravar ficaria como responsavel.
    ordem_id = _execucao_na_fila(session_factory)
    primeiro, segundo = uuid4(), uuid4()
    antes = len(outbox())

    erros = _disputar(
        engine,
        lambda travou, seguir: _iniciar_execucao(
            session_factory,
            ordem_id,
            primeiro,
            _execucoes_que_seguram(travou, seguir),
        ),
        lambda: _iniciar_execucao(session_factory, ordem_id, segundo),
    )

    assert list(erros) == ["disputa"]
    assert isinstance(erros["disputa"], TransicaoStatusInvalidaException)
    assert _execucao(session_factory, ordem_id).mecanico_id == primeiro
    assert [
        (linha["tipo"], linha["dados"]["mecanico_id"]) for linha in outbox()[antes:]
    ] == [("ExecucaoIniciada", str(primeiro))]
