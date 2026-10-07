from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from src.compartilhado.aplicacao.idempotencia import releitura_em_corrida
from src.compartilhado.aplicacao.responsavel import (
    registrar_auditoria,
    responsavel_efetivo,
)
from src.diagnostico.aplicacao.events import (
    DiagnosticoConcluidoEvent,
    DiagnosticoDescartadoEvent,
    DiagnosticoIniciadoEvent,
    ItemDTO,
)
from src.diagnostico.dominio.diagnostico import (
    Diagnostico,
    StatusDiagnostico,
    TipoItem,
)
from src.diagnostico.dominio.exceptions import (
    DiagnosticoNaoEncontradoException,
    ItensInvalidosException,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from src.compartilhado.aplicacao.unit_of_work import (
        UnitOfWork,
        UnitOfWorkDoComando,
    )
    from src.compartilhado.dominio.veiculo import Veiculo
    from src.diagnostico.aplicacao.ports import (
        CatalogoDePecasPort,
        ValidadorDeItensPort,
    )
    from src.diagnostico.dominio.diagnostico import ItemDiagnostico
    from src.diagnostico.dominio.repository import DiagnosticoRepository

_log = structlog.get_logger(__name__)


def _obter(
    repo: DiagnosticoRepository, ordem_id: UUID, *, com_lock: bool = False
) -> Diagnostico:
    diagnostico = repo.obter(ordem_id, com_lock=com_lock)
    if diagnostico is None:
        raise DiagnosticoNaoEncontradoException(ordem_id)
    return diagnostico


class RegistrarSolicitacaoDeDiagnostico:
    """Comando ``SolicitarDiagnostico`` (T2): poe a ordem na fila do mecanico.

    Idempotente por ordem: reenvio do orquestrador devolve o diagnostico
    existente sem mudar nada. Nao ha resposta no catalogo; o fato seguinte e
    ``DiagnosticoIniciado``, quando o mecanico comeca. Com o diagnostico ja
    concluido ou descartado (inclusive a lapide de um descarte adiantado), o
    comando atrasado e descartado com log. Quem comita e o consumidor.
    """

    def __init__(self, repo: DiagnosticoRepository, uow: UnitOfWorkDoComando) -> None:
        self._repo = repo
        self._uow = uow

    @releitura_em_corrida
    def executar(
        self,
        ordem_id: UUID,
        veiculo: Veiculo,
        descricao_problema: str,
        solicitacao_id: UUID,
    ) -> Diagnostico:
        """``solicitacao_id``: id do comando, causa dos fatos que o mecanico gera."""
        novo = Diagnostico.solicitar(
            ordem_id=ordem_id,
            veiculo=veiculo,
            descricao_problema=descricao_problema,
            agora=datetime.now(UTC),
            solicitacao_id=solicitacao_id,
        )
        with self._uow:
            existente = self._repo.obter(ordem_id)
            if existente is not None:
                if existente.status in _DIAGNOSTICO_ENCERRADO:
                    _log.info(
                        "command_ignored",
                        codigo="COMANDO_ATRASADO",
                        comando="SolicitarDiagnostico",
                        correlation_id=str(ordem_id),
                        status=existente.status,
                    )
                self._uow.descartar()
                return existente
            self._repo.salvar(novo)
        return novo


# Superado pela conclusao ou compensado: a solicitacao atrasada nao muda nada.
_DIAGNOSTICO_ENCERRADO = frozenset(
    {StatusDiagnostico.CONCLUIDO, StatusDiagnostico.DESCARTADO}
)


class ListarDiagnosticos:
    def __init__(self, repo: DiagnosticoRepository) -> None:
        self._repo = repo

    def executar(
        self, status: StatusDiagnostico | None, offset: int, limit: int
    ) -> tuple[list[Diagnostico], int]:
        """Pagina por ordem de chegada e o total com o mesmo filtro."""
        return (
            self._repo.listar(status, offset=offset, limit=limit),
            self._repo.contar(status),
        )


class IniciarDiagnostico:
    def __init__(self, repo: DiagnosticoRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(self, ordem_id: UUID, mecanico_id: UUID) -> Diagnostico:
        """Mecanico assume o diagnostico; emite ``DiagnosticoIniciado``.

        Repetir pelo mesmo mecanico devolve o estado atual sem novo evento.
        """
        agora = datetime.now(UTC)
        with self._uow:
            diagnostico = _obter(self._repo, ordem_id, com_lock=True)
            if diagnostico.iniciar(mecanico_id, agora):
                self._repo.salvar(diagnostico)
                self._uow.registrar_evento(
                    DiagnosticoIniciadoEvent(
                        ordem_id=ordem_id,
                        ocorrido_em=agora,
                        causation_id=diagnostico.solicitacao_id,
                        mecanico_id=mecanico_id,
                        iniciado_em=agora,
                    )
                )
                self._uow.commit()
        return diagnostico


class ConcluirDiagnostico:
    """Mecanico registra servicos e pecas; emite ``DiagnosticoConcluido``.

    Tres etapas: (1) leitura curta que confere estado/responsavel/itens no
    agregado e as pecas no estoque local; (2) chamada ao Billing (timeout,
    retry e circuit breaker) sem transacao aberta, com a conexao ja devolvida
    ao pool; (3) escrita curta que le a linha de novo, sob lock, e conclui.
    So o mecanico que iniciou conclui; o admin (pode tudo) conclui em nome
    dele, sem trocar o responsavel.
    """

    def __init__(
        self,
        repo: DiagnosticoRepository,
        catalogo: CatalogoDePecasPort,
        validador: ValidadorDeItensPort,
        uow: UnitOfWork,
    ) -> None:
        self._repo = repo
        self._catalogo = catalogo
        self._validador = validador
        self._uow = uow

    def executar(
        self,
        ordem_id: UUID,
        mecanico_id: UUID,
        itens: Sequence[ItemDiagnostico],
        observacoes: str,
        *,
        pelo_admin: bool = False,
    ) -> Diagnostico:
        """Repetir pelo mesmo mecanico devolve o diagnostico ja concluido, sem evento.

        Raises:
            ItensInvalidosException: peca sem cadastro ativo ou codigo sem preco.
            DependenciaIndisponivelException: Billing fora do ar (503).
        """
        # A saida do bloco fecha a sessao: nenhuma conexao fica presa enquanto o
        # Billing responde (ate 3 tentativas de 2 s).
        with self._uow:
            atual = _obter(self._repo, ordem_id)
            responsavel = responsavel_efetivo(
                atual.mecanico_id, mecanico_id, pelo_admin=pelo_admin
            )
            if atual.concluido_por(responsavel):
                return atual
            atual.validar_conclusao(responsavel, itens, observacoes)
            self._validar_estoque_local(itens)
        self._validar_no_billing(itens)
        agora = datetime.now(UTC)
        with self._uow:
            diagnostico = _obter(self._repo, ordem_id, com_lock=True)
            responsavel = responsavel_efetivo(
                diagnostico.mecanico_id, mecanico_id, pelo_admin=pelo_admin
            )
            if diagnostico.concluir(responsavel, itens, observacoes, agora):
                self._repo.salvar(diagnostico)
                self._uow.registrar_evento(_concluido(diagnostico, agora))
                self._uow.commit()
                if pelo_admin:
                    registrar_auditoria(
                        "concluir_diagnostico",
                        ator_id=mecanico_id,
                        alvo=str(ordem_id),
                        mecanico_id=str(responsavel),
                    )
        return diagnostico

    def _validar_estoque_local(self, itens: Sequence[ItemDiagnostico]) -> None:
        # Antes do Billing: barato e evita a chamada remota quando ja ha erro.
        pecas = [item.codigo for item in itens if item.tipo is TipoItem.PECA]
        sem_estoque = self._catalogo.skus_indisponiveis(pecas) if pecas else []
        if sem_estoque:
            msg = (
                "Pecas sem cadastro ativo no estoque: "
                f"{', '.join(sem_estoque)}. Cadastre-as (admin) ou corrija o codigo."
            )
            raise ItensInvalidosException(msg)

    def _validar_no_billing(self, itens: Sequence[ItemDiagnostico]) -> None:
        pecas = [item.codigo for item in itens if item.tipo is TipoItem.PECA]
        servicos = [item.codigo for item in itens if item.tipo is TipoItem.SERVICO]
        invalidos = self._validador.codigos_invalidos(servicos=servicos, pecas=pecas)
        if invalidos:
            msg = (
                "Codigos sem preco na tabela do Billing: "
                f"{', '.join(invalidos)}. Corrija o codigo ou cadastre o preco."
            )
            raise ItensInvalidosException(msg)


def _concluido(diagnostico: Diagnostico, agora: datetime) -> DiagnosticoConcluidoEvent:
    return DiagnosticoConcluidoEvent(
        ordem_id=diagnostico.ordem_id,
        ocorrido_em=agora,
        causation_id=diagnostico.solicitacao_id,
        itens=tuple(
            ItemDTO(
                tipo=item.tipo.value, codigo=item.codigo, quantidade=item.quantidade
            )
            for item in diagnostico.itens
        ),
        observacoes=diagnostico.observacoes,
        concluido_em=agora,
    )


class DescartarDiagnostico:
    """Comando de compensacao ``DescartarDiagnostico``, idempotente por ordem.

    Qualquer estado nao final vira DESCARTADO e a resposta
    ``DiagnosticoDescartado`` e emitida; repetido, so reemite a resposta. Ordem
    sem diagnostico (``SolicitarDiagnostico`` ainda em voo): grava a lapide e
    responde. O ``motivo`` do comando nao e usado aqui: o historico fica no OS
    Service.
    """

    def __init__(self, repo: DiagnosticoRepository, uow: UnitOfWorkDoComando) -> None:
        self._repo = repo
        self._uow = uow

    @releitura_em_corrida
    def executar(self, ordem_id: UUID) -> None:
        agora = datetime.now(UTC)
        with self._uow:
            diagnostico = self._repo.obter(ordem_id, com_lock=True)
            lapide = diagnostico is None
            if diagnostico is None:
                diagnostico = Diagnostico.lapide(ordem_id=ordem_id, agora=agora)
            else:
                diagnostico.descartar(agora)
            self._repo.salvar(diagnostico)
            self._uow.registrar_evento(
                DiagnosticoDescartadoEvent(ordem_id=ordem_id, ocorrido_em=agora)
            )
        # Depois do bloco: a copia que perde a corrida pela lapide roda de novo
        # e nao registra uma lapide que nao gravou.
        _log.info("diagnosis_discarded", correlation_id=str(ordem_id), tombstone=lapide)
