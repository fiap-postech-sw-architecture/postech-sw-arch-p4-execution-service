from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from src.compartilhado.dominio.exceptions import (
    OperacaoNaoPermitidaException,
    TransicaoStatusInvalidaException,
)
from src.execucao.dominio.execucao import Execucao, Prioridade, StatusExecucao

if TYPE_CHECKING:
    from collections.abc import Callable

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
MECANICO, OUTRO = uuid4(), uuid4()


def _agendada(prioridade: Prioridade = Prioridade.NORMAL) -> Execucao:
    return Execucao.agendar(
        ordem_id=uuid4(), prioridade=prioridade, veiculo=None, agora=AGORA
    )


def _iniciada() -> Execucao:
    execucao = _agendada()
    execucao.iniciar(MECANICO, AGORA)
    return execucao


def test_agendada_entra_na_fila() -> None:
    execucao = _agendada(prioridade=Prioridade.ALTA)
    assert execucao.status is StatusExecucao.AGUARDANDO
    assert (execucao.ordem_id, execucao.prioridade) == (execucao.id, Prioridade.ALTA)
    assert execucao.enfileirada_em == AGORA
    assert (execucao.mecanico_id, execucao.iniciada_em) == (None, None)


@pytest.mark.parametrize(
    "prioridade",
    [pytest.param("urgente", id="fora-do-enum"), pytest.param(1, id="numero-antigo")],
)
def test_prioridade_fora_do_contrato(prioridade: object) -> None:
    with pytest.raises(ValueError, match="Prioridade"):
        _agendada(prioridade)


def test_iniciar_registra_mecanico() -> None:
    execucao = _agendada()
    assert execucao.iniciar(MECANICO, AGORA) is True
    assert execucao.status is StatusExecucao.EM_EXECUCAO
    assert (execucao.mecanico_id, execucao.iniciada_em) == (MECANICO, AGORA)


def test_iniciar_de_novo_pelo_mesmo_mecanico_e_no_op() -> None:
    execucao = _iniciada()
    assert execucao.iniciar(MECANICO, datetime.now(UTC)) is False
    assert execucao.iniciada_em == AGORA


def test_iniciar_por_outro_mecanico_e_409() -> None:
    execucao = _iniciada()
    with pytest.raises(TransicaoStatusInvalidaException, match="outro mecanico"):
        execucao.iniciar(OUTRO, AGORA)


def test_finalizar_pelo_responsavel() -> None:
    execucao = _iniciada()
    assert execucao.finalizar(MECANICO, AGORA) is True
    assert execucao.status is StatusExecucao.FINALIZADA
    assert execucao.finalizada_em == AGORA
    assert execucao.finalizada_por(MECANICO)
    assert execucao.finalizar(MECANICO, datetime.now(UTC)) is False


def test_finalizar_sem_iniciar_e_409() -> None:
    execucao = _agendada()
    with pytest.raises(TransicaoStatusInvalidaException, match="AGUARDANDO"):
        execucao.finalizar(MECANICO, AGORA)


def test_so_o_responsavel_finaliza() -> None:
    execucao = _iniciada()
    with pytest.raises(OperacaoNaoPermitidaException):
        execucao.finalizar(OUTRO, AGORA)
    assert execucao.status is StatusExecucao.EM_EXECUCAO


def test_finalizada_por_um_nao_e_finalizavel_por_outro() -> None:
    execucao = _iniciada()
    execucao.finalizar(MECANICO, AGORA)
    assert not execucao.finalizada_por(OUTRO)
    with pytest.raises(TransicaoStatusInvalidaException):
        execucao.finalizar(OUTRO, AGORA)


def test_cancelar_na_fila_e_idempotente() -> None:
    execucao = _agendada()
    execucao.cancelar(AGORA)
    execucao.cancelar(datetime.now(UTC))
    assert execucao.status is StatusExecucao.CANCELADA
    assert execucao.cancelada_em == AGORA


def test_depois_de_iniciada_nao_cancela() -> None:
    execucao = _iniciada()
    with pytest.raises(TransicaoStatusInvalidaException, match="EM_EXECUCAO"):
        execucao.cancelar(AGORA)


def test_lapide_nasce_cancelada_fora_da_fila() -> None:
    ordem_id = uuid4()
    lapide = Execucao.lapide(ordem_id=ordem_id, agora=AGORA)
    assert (lapide.ordem_id, lapide.status) == (ordem_id, StatusExecucao.CANCELADA)
    assert lapide.cancelada_em == AGORA
    assert (lapide.mecanico_id, lapide.iniciada_em) == (None, None)


def test_cancelada_nao_inicia() -> None:
    execucao = _agendada()
    execucao.cancelar(AGORA)
    with pytest.raises(TransicaoStatusInvalidaException, match="estado final"):
        execucao.iniciar(MECANICO, AGORA)


def test_validar_inicio_nao_muda_nada() -> None:
    execucao = _agendada()
    execucao.validar_inicio(MECANICO)
    assert execucao.status is StatusExecucao.AGUARDANDO
    assert (execucao.mecanico_id, execucao.iniciada_em) == (None, None)


@pytest.mark.parametrize(
    ("preparar", "mensagem"),
    [
        pytest.param(lambda e: e.iniciar(OUTRO, AGORA), "outro mecanico", id="outro"),
        pytest.param(lambda e: e.cancelar(AGORA), "estado final", id="cancelada"),
    ],
)
def test_validar_inicio_recusa_sem_mudar_o_agregado(
    preparar: Callable[[Execucao], object], mensagem: str
) -> None:
    execucao = _agendada()
    preparar(execucao)
    status, mecanico = execucao.status, execucao.mecanico_id
    with pytest.raises(TransicaoStatusInvalidaException, match=mensagem):
        execucao.validar_inicio(MECANICO)
    assert (execucao.status, execucao.mecanico_id) == (status, mecanico)


def test_validar_finalizacao_nao_muda_nada() -> None:
    execucao = _iniciada()
    execucao.validar_finalizacao(MECANICO)
    assert execucao.status is StatusExecucao.EM_EXECUCAO
    assert execucao.finalizada_em is None
    with pytest.raises(OperacaoNaoPermitidaException):
        execucao.validar_finalizacao(OUTRO)


@pytest.mark.parametrize(
    ("status", "mecanico"),
    [
        pytest.param(StatusExecucao.EM_EXECUCAO, None, id="iniciada-sem-mecanico"),
        pytest.param(StatusExecucao.AGUARDANDO, MECANICO, id="na-fila-com-mecanico"),
    ],
)
def test_estado_incoerente_e_recusado_na_construcao(
    status: StatusExecucao, mecanico: UUID | None
) -> None:
    ordem_id = uuid4()
    with pytest.raises(ValueError, match="mecanico"):
        Execucao(
            id=ordem_id,
            _status=status,
            _prioridade=Prioridade.NORMAL,
            _enfileirada_em=AGORA,
            _mecanico_id=mecanico,
        )
