from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.veiculo import Veiculo
    from src.execucao.aplicacao.events import PecaConsumidaDTO
    from src.execucao.dominio.execucao import Execucao, Prioridade


@dataclass(frozen=True, slots=True)
class ItemDaFila:
    posicao: int
    ordem_id: UUID
    prioridade: Prioridade
    enfileirada_em: datetime
    veiculo: Veiculo | None


class FilaDeExecucaoPort(Protocol):
    """Leitura da fila: execucoes AGUARDANDO, ``alta`` antes, depois por chegada."""

    def listar(self, offset: int, limit: int) -> list[ItemDaFila]:
        """Pagina da fila com a posicao absoluta de cada ordem."""

    def contar(self) -> int:
        """Tamanho da fila."""

    def posicao(self, execucao: Execucao) -> int:
        """Posicao (1 = proxima) de uma execucao AGUARDANDO ja gravada na sessao."""


class EstoquePort(Protocol):
    """Estoque do proprio servico, na mesma transacao do caso de uso."""

    def tem_reserva_ativa(self, ordem_id: UUID) -> bool:
        """A ordem tem pecas separadas esperando a baixa.

        Trava a reserva ate o fim da transacao: uma liberacao em curso termina
        antes e e vista; uma que chegar depois espera esta comitar.
        """

    def consumir_reserva(
        self, ordem_id: UUID, agora: datetime
    ) -> list[PecaConsumidaDTO] | None:
        """Baixa a reserva ATIVA da ordem; ``None`` se a ordem nao tem reserva.

        Raises:
            TransicaoStatusInvalidaException: reserva liberada, recusada ou ja
                consumida.
        """


class VeiculosPort(Protocol):
    """Retrato do veiculo que o diagnostico da ordem registrou."""

    def da_ordem(self, ordem_id: UUID) -> Veiculo | None:
        """Retrato (inclusive anonimizado); ``None`` se a ordem nao tem diagnostico."""


@dataclass(frozen=True, slots=True)
class DiagnosticoAnonimizado:
    """Diagnostico do veiculo que teve placa e textos livres trocados agora."""

    ordem_id: UUID
    em_andamento: bool


class DiagnosticosDoVeiculoPort(Protocol):
    """Diagnosticos do contexto vizinho que guardam o retrato do veiculo (LGPD)."""

    def anonimizar(self, veiculo_id: UUID) -> list[DiagnosticoAnonimizado]:
        """Troca placa e textos livres de cada diagnostico do veiculo.

        Devolve so os que mudaram nesta chamada (idempotente).
        """


class MensagensGuardadasPort(Protocol):
    """Mensagens ainda guardadas na outbox (pendentes, entregues ou ``dead``)."""

    def anonimizar(self, ordens: Sequence[UUID]) -> int:
        """Troca o texto livre das mensagens dessas ordens; devolve quantas mudaram."""
