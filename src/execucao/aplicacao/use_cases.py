from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from src.compartilhado.aplicacao.idempotencia import releitura_em_corrida
from src.compartilhado.aplicacao.responsavel import (
    registrar_auditoria,
    responsavel_efetivo,
)
from src.compartilhado.dominio.exceptions import ViolacaoRegraDeNegocioException
from src.execucao.aplicacao.events import (
    ExecucaoAgendadaEvent,
    ExecucaoCanceladaEvent,
    ExecucaoFinalizadaEvent,
    ExecucaoIniciadaEvent,
)
from src.execucao.dominio.exceptions import ExecucaoNaoEncontradaException
from src.execucao.dominio.execucao import Execucao, Prioridade, StatusExecucao

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.aplicacao.unit_of_work import (
        UnitOfWork,
        UnitOfWorkDoComando,
    )
    from src.execucao.aplicacao.ports import (
        EstoquePort,
        FilaDeExecucaoPort,
        ItemDaFila,
        VeiculosPort,
    )
    from src.execucao.dominio.repository import ExecucaoRepository

_log = structlog.get_logger(__name__)


def _obter(
    repo: ExecucaoRepository, ordem_id: UUID, *, com_lock: bool = False
) -> Execucao:
    execucao = repo.obter(ordem_id, com_lock=com_lock)
    if execucao is None:
        raise ExecucaoNaoEncontradaException(ordem_id)
    return execucao


def _sem_reserva() -> ViolacaoRegraDeNegocioException:
    msg = (
        "A ordem nao tem reserva de pecas ativa: sem ela nao ha baixa de estoque "
        "e a execucao nao pode seguir"
    )
    return ViolacaoRegraDeNegocioException(msg)


class AgendarExecucao:
    """Comando ``AgendarExecucao`` (T7): poe a ordem na fila de execucao.

    Responde ``ExecucaoAgendada{posicao_na_fila}``. So entra na fila a ordem
    com as pecas reservadas (reserva ATIVA, RN-027): sem ela a execucao nunca
    poderia ser finalizada. Idempotente por ordem: com a execucao ainda
    AGUARDANDO, o reenvio so reemite a resposta com a posicao atual (a
    prioridade original fica); ja iniciada, finalizada ou cancelada (inclusive
    a lapide de um cancelamento adiantado), o comando atrasado e descartado sem
    efeito e sem resposta. A execucao nova copia o retrato do veiculo do
    diagnostico, que a fila mostra ao mecanico. Quem comita e o consumidor.
    """

    def __init__(
        self,
        repo: ExecucaoRepository,
        fila: FilaDeExecucaoPort,
        veiculos: VeiculosPort,
        estoque: EstoquePort,
        uow: UnitOfWorkDoComando,
    ) -> None:
        self._repo = repo
        self._fila = fila
        self._veiculos = veiculos
        self._estoque = estoque
        self._uow = uow

    @releitura_em_corrida
    def executar(
        self,
        ordem_id: UUID,
        prioridade: Prioridade,
        agendamento_id: UUID | None = None,
    ) -> Execucao:
        """Devolve a execucao da ordem (nova, na fila ou ja encerrada).

        ``agendamento_id``: id do comando, causa dos fatos que o mecanico gera.

        Raises:
            ViolacaoRegraDeNegocioException: ordem nova sem reserva ATIVA.
        """
        agora = datetime.now(UTC)
        with self._uow:
            execucao = self._repo.obter(ordem_id)
            if execucao is None:
                execucao = Execucao.agendar(
                    ordem_id=ordem_id,
                    prioridade=prioridade,
                    veiculo=self._veiculos.da_ordem(ordem_id),
                    agora=agora,
                    agendamento_id=agendamento_id,
                )
                if not self._estoque.tem_reserva_ativa(ordem_id):
                    raise _sem_reserva()
                self._repo.salvar(execucao)
            elif execucao.status is not StatusExecucao.AGUARDANDO:
                _log.info(
                    "late_command_discarded",
                    comando="AgendarExecucao",
                    correlation_id=str(ordem_id),
                    status=execucao.status,
                )
                self._uow.descartar()
                return execucao
            self._uow.registrar_evento(
                ExecucaoAgendadaEvent(
                    ordem_id=ordem_id,
                    ocorrido_em=agora,
                    posicao_na_fila=self._fila.posicao(execucao),
                )
            )
        return execucao


class ListarFila:
    """Fila de execucao: ``alta`` antes de ``normal``, depois por chegada."""

    def __init__(self, fila: FilaDeExecucaoPort) -> None:
        self._fila = fila

    def executar(self, offset: int, limit: int) -> tuple[list[ItemDaFila], int]:
        """Pagina da fila (com a posicao absoluta de cada ordem) e o tamanho dela."""
        return self._fila.listar(offset=offset, limit=limit), self._fila.contar()


class CancelarExecucao:
    """Comando de compensacao ``CancelarExecucao``, idempotente por ordem.

    Tira a ordem da fila e responde ``ExecucaoCancelada``; repetido, so reemite
    a resposta. Depois de iniciada (pivot da saga) nao cancela: 409. Ordem sem
    execucao (``AgendarExecucao`` ainda em voo): grava a lapide e responde. O
    ``motivo`` do comando nao e usado aqui: o historico fica no OS Service.
    """

    def __init__(self, repo: ExecucaoRepository, uow: UnitOfWorkDoComando) -> None:
        self._repo = repo
        self._uow = uow

    @releitura_em_corrida
    def executar(self, ordem_id: UUID) -> None:
        agora = datetime.now(UTC)
        with self._uow:
            execucao = self._repo.obter(ordem_id, com_lock=True)
            lapide = execucao is None
            if execucao is None:
                execucao = Execucao.lapide(ordem_id=ordem_id, agora=agora)
            else:
                execucao.cancelar(agora)
            self._repo.salvar(execucao)
            self._uow.registrar_evento(
                ExecucaoCanceladaEvent(ordem_id=ordem_id, ocorrido_em=agora)
            )
        # Depois do bloco (ver DescartarDiagnostico).
        _log.info("execution_cancelled", correlation_id=str(ordem_id), tombstone=lapide)


class IniciarExecucao:
    def __init__(
        self, repo: ExecucaoRepository, estoque: EstoquePort, uow: UnitOfWork
    ) -> None:
        self._repo = repo
        self._estoque = estoque
        self._uow = uow

    def executar(self, ordem_id: UUID, mecanico_id: UUID) -> Execucao:
        """Mecanico tira a ordem da fila; emite ``ExecucaoIniciada`` (pivot).

        Exige a reserva de pecas ativa: depois do pivot a OS nao cancela, e uma
        execucao sem reserva nunca conseguiria finalizar (baixa impossivel).
        Repetir pelo mesmo mecanico devolve o estado atual sem novo evento.
        """
        agora = datetime.now(UTC)
        with self._uow:
            execucao = _obter(self._repo, ordem_id, com_lock=True)
            if execucao.iniciada_por(mecanico_id):
                return execucao
            # Guardas antes de qualquer mudanca: transicao, responsavel e a
            # reserva ativa (travada ate o commit).
            execucao.validar_inicio(mecanico_id)
            if not self._estoque.tem_reserva_ativa(ordem_id):
                raise _sem_reserva()
            execucao.iniciar(mecanico_id, agora)
            self._repo.salvar(execucao)
            self._uow.registrar_evento(
                ExecucaoIniciadaEvent(
                    ordem_id=ordem_id,
                    ocorrido_em=agora,
                    causation_id=execucao.agendamento_id,
                    mecanico_id=mecanico_id,
                    iniciada_em=agora,
                )
            )
            self._uow.commit()
        return execucao


class FinalizarExecucao:
    """Mecanico encerra o reparo; consome a reserva e emite ``ExecucaoFinalizada``.

    Finalizacao e baixa de estoque saem na mesma transacao local (mesmo banco):
    ou as duas acontecem, ou nenhuma. So o mecanico que iniciou finaliza; o
    admin (pode tudo) finaliza em nome dele, sem trocar o responsavel.
    """

    def __init__(
        self, repo: ExecucaoRepository, estoque: EstoquePort, uow: UnitOfWork
    ) -> None:
        self._repo = repo
        self._estoque = estoque
        self._uow = uow

    def executar(
        self, ordem_id: UUID, mecanico_id: UUID, *, pelo_admin: bool = False
    ) -> Execucao:
        """Repetir pelo mesmo mecanico devolve o estado atual sem nova baixa."""
        agora = datetime.now(UTC)
        with self._uow:
            execucao = _obter(self._repo, ordem_id, com_lock=True)
            responsavel = responsavel_efetivo(
                execucao.mecanico_id, mecanico_id, pelo_admin=pelo_admin
            )
            if execucao.finalizada_por(responsavel):
                return execucao
            execucao.validar_finalizacao(responsavel)
            # A baixa vem antes de mexer no agregado: reserva fora de ATIVA (ou
            # inexistente) recusa a finalizacao sem mudar nada.
            pecas = self._estoque.consumir_reserva(ordem_id, agora)
            if pecas is None:
                raise _sem_reserva()
            execucao.finalizar(responsavel, agora)
            self._repo.salvar(execucao)
            self._uow.registrar_evento(
                ExecucaoFinalizadaEvent(
                    ordem_id=ordem_id,
                    ocorrido_em=agora,
                    causation_id=execucao.agendamento_id,
                    finalizada_em=agora,
                    pecas_consumidas=tuple(pecas),
                )
            )
            self._uow.commit()
        if pelo_admin:
            registrar_auditoria(
                "finalizar_execucao",
                ator_id=mecanico_id,
                alvo=str(ordem_id),
                mecanico_id=str(responsavel),
            )
        return execucao
