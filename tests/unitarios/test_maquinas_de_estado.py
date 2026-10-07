"""Toda transicao de cada agregado, com o esperado escrito aqui (nao lido do
``_TRANSICOES`` do codigo): remover ou acrescentar uma transicao quebra a tabela.

Cada linha parte de um estado montado pelo caminho legitimo e aplica uma acao
do mecanico responsavel: "muda" (vai ao estado-alvo), "nada" (repeticao
idempotente, sem mudar) ou "409" (transicao invalida).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException
from src.compartilhado.dominio.veiculo import Veiculo
from src.diagnostico.dominio.diagnostico import (
    Diagnostico,
    ItemDiagnostico,
    StatusDiagnostico,
    TipoItem,
)
from src.estoque.dominio.reserva import (
    Faltante,
    ItemReserva,
    Reserva,
    StatusReserva,
)
from src.estoque.dominio.sku import Sku
from src.execucao.dominio.execucao import Execucao, Prioridade, StatusExecucao

if TYPE_CHECKING:
    from collections.abc import Callable
    from enum import StrEnum

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
MECANICO = uuid4()
ITENS = [ItemDiagnostico(tipo=TipoItem.SERVICO, codigo="SRV-FREIOS", quantidade=1)]
VELA = Sku("PEC-VELA")


def _id(estado: StrEnum, acao: str) -> str:
    return f"{estado.value.lower()}-{acao}"


def _conferir(
    acao: Callable[[], object],
    status: Callable[[], StrEnum],
    esperado: str,
    alvo: StrEnum,
) -> None:
    antes = status()
    if esperado == "409":
        with pytest.raises(TransicaoStatusInvalidaException):
            acao()
        assert status() is antes
    else:
        acao()
        assert status() is (alvo if esperado == "muda" else antes)


# --- Diagnostico --------------------------------------------------------------

D = StatusDiagnostico


def _diagnostico(estado: StatusDiagnostico) -> Diagnostico:
    veiculo = Veiculo(
        veiculo_id=uuid4(), placa="ABC1D23", marca="Fiat", modelo="Uno", ano=2015
    )
    diagnostico = Diagnostico.solicitar(
        ordem_id=uuid4(),
        veiculo=veiculo,
        descricao_problema="x",
        agora=AGORA,
        solicitacao_id=uuid4(),
    )
    if estado in {D.EM_ANDAMENTO, D.CONCLUIDO}:
        diagnostico.iniciar(MECANICO, AGORA)
    if estado is D.CONCLUIDO:
        diagnostico.concluir(MECANICO, ITENS, "", AGORA)
    if estado is D.DESCARTADO:
        diagnostico.descartar(AGORA)
    return diagnostico


_ACOES_DIAGNOSTICO: dict[
    str, tuple[StatusDiagnostico, Callable[[Diagnostico], object]]
] = {
    "iniciar": (D.EM_ANDAMENTO, lambda d: d.iniciar(MECANICO, AGORA)),
    "concluir": (D.CONCLUIDO, lambda d: d.concluir(MECANICO, ITENS, "", AGORA)),
    "descartar": (D.DESCARTADO, lambda d: d.descartar(AGORA)),
}
_TABELA_DIAGNOSTICO = [
    (D.AGUARDANDO, "iniciar", "muda"),
    (D.AGUARDANDO, "concluir", "409"),
    (D.AGUARDANDO, "descartar", "muda"),
    (D.EM_ANDAMENTO, "iniciar", "nada"),
    (D.EM_ANDAMENTO, "concluir", "muda"),
    (D.EM_ANDAMENTO, "descartar", "muda"),
    (D.CONCLUIDO, "iniciar", "409"),
    (D.CONCLUIDO, "concluir", "nada"),
    (D.CONCLUIDO, "descartar", "muda"),
    (D.DESCARTADO, "iniciar", "409"),
    (D.DESCARTADO, "concluir", "409"),
    (D.DESCARTADO, "descartar", "nada"),
]


@pytest.mark.parametrize(
    ("estado", "acao", "esperado"),
    [pytest.param(*linha, id=_id(linha[0], linha[1])) for linha in _TABELA_DIAGNOSTICO],
)
def test_transicoes_do_diagnostico(
    estado: StatusDiagnostico, acao: str, esperado: str
) -> None:
    diagnostico = _diagnostico(estado)
    alvo, aplicar = _ACOES_DIAGNOSTICO[acao]
    _conferir(
        lambda: aplicar(diagnostico),
        lambda: diagnostico.status,
        esperado,
        alvo,
    )


# --- Execucao -----------------------------------------------------------------

E = StatusExecucao


def _execucao(estado: StatusExecucao) -> Execucao:
    execucao = Execucao.agendar(
        ordem_id=uuid4(),
        prioridade=Prioridade.NORMAL,
        veiculo=None,
        agora=AGORA,
        agendamento_id=uuid4(),
    )
    if estado in {E.EM_EXECUCAO, E.FINALIZADA}:
        execucao.iniciar(MECANICO, AGORA)
    if estado is E.FINALIZADA:
        execucao.finalizar(MECANICO, AGORA)
    if estado is E.CANCELADA:
        execucao.cancelar(AGORA)
    return execucao


_ACOES_EXECUCAO: dict[str, tuple[StatusExecucao, Callable[[Execucao], object]]] = {
    "iniciar": (E.EM_EXECUCAO, lambda e: e.iniciar(MECANICO, AGORA)),
    "finalizar": (E.FINALIZADA, lambda e: e.finalizar(MECANICO, AGORA)),
    "cancelar": (E.CANCELADA, lambda e: e.cancelar(AGORA)),
}
_TABELA_EXECUCAO = [
    (E.AGUARDANDO, "iniciar", "muda"),
    (E.AGUARDANDO, "finalizar", "409"),
    (E.AGUARDANDO, "cancelar", "muda"),
    (E.EM_EXECUCAO, "iniciar", "nada"),
    (E.EM_EXECUCAO, "finalizar", "muda"),
    (E.EM_EXECUCAO, "cancelar", "409"),  # pivot: depois de iniciada nao cancela
    (E.FINALIZADA, "iniciar", "409"),
    (E.FINALIZADA, "finalizar", "nada"),
    (E.FINALIZADA, "cancelar", "409"),
    (E.CANCELADA, "iniciar", "409"),
    (E.CANCELADA, "finalizar", "409"),
    (E.CANCELADA, "cancelar", "nada"),
]


@pytest.mark.parametrize(
    ("estado", "acao", "esperado"),
    [pytest.param(*linha, id=_id(linha[0], linha[1])) for linha in _TABELA_EXECUCAO],
)
def test_transicoes_da_execucao(
    estado: StatusExecucao, acao: str, esperado: str
) -> None:
    execucao = _execucao(estado)
    alvo, aplicar = _ACOES_EXECUCAO[acao]
    _conferir(lambda: aplicar(execucao), lambda: execucao.status, esperado, alvo)


# --- Reserva ------------------------------------------------------------------

R = StatusReserva


def _reserva(estado: StatusReserva) -> Reserva:
    if estado is R.RECUSADA:
        return Reserva.recusar(
            ordem_id=uuid4(),
            itens=[ItemReserva(VELA, 2)],
            faltantes=[Faltante(sku=str(VELA), solicitado=2, disponivel=0)],
            agora=AGORA,
        )
    reserva = Reserva.criar(ordem_id=uuid4(), itens=[ItemReserva(VELA, 2)], agora=AGORA)
    if estado is R.LIBERADA:
        reserva.liberar(AGORA)
    if estado is R.CONSUMIDA:
        reserva.consumir(AGORA)
    return reserva


_ACOES_RESERVA: dict[str, tuple[StatusReserva, Callable[[Reserva], object]]] = {
    "liberar": (R.LIBERADA, lambda r: r.liberar(AGORA)),
    "consumir": (R.CONSUMIDA, lambda r: r.consumir(AGORA)),
}
_TABELA_RESERVA = [
    (R.ATIVA, "liberar", "muda"),
    (R.ATIVA, "consumir", "muda"),
    (R.LIBERADA, "liberar", "nada"),
    (R.LIBERADA, "consumir", "409"),
    (R.CONSUMIDA, "liberar", "409"),  # baixa feita nao volta
    (R.CONSUMIDA, "consumir", "409"),
    (R.RECUSADA, "liberar", "nada"),  # nada foi separado
    (R.RECUSADA, "consumir", "409"),
]


@pytest.mark.parametrize(
    ("estado", "acao", "esperado"),
    [pytest.param(*linha, id=_id(linha[0], linha[1])) for linha in _TABELA_RESERVA],
)
def test_transicoes_da_reserva(estado: StatusReserva, acao: str, esperado: str) -> None:
    reserva = _reserva(estado)
    alvo, aplicar = _ACOES_RESERVA[acao]
    _conferir(lambda: aplicar(reserva), lambda: reserva.status, esperado, alvo)
