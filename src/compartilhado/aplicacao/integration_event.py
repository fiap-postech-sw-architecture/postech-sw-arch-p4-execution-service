from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, kw_only=True)
class IntegrationEvent:
    """Mensagem do catalogo da saga (RFC-004, secao 5.3) emitida por este servico.

    ``ordem_id`` e o correlation_id da saga; ``id`` vira o ``message_id`` do
    envelope e ``ocorrido_em`` o instante do fato. Os campos da subclasse, mais
    ``ordem_id``, formam o ``dados`` do envelope. O ``tipo`` e o nome da classe
    sem o sufixo ``Event`` (``PecasReservadasEvent`` -> ``PecasReservadas``).

    ``causation_id`` so e informado no fato que nasce de uma acao pela API: o
    ``id`` do comando que abriu o fluxo (``SolicitarDiagnostico`` ou
    ``AgendarExecucao``), pelo qual o orquestrador casa o evento. Na resposta a
    um comando ele fica vazio e a outbox usa o ``id`` do comando em processamento.
    """

    ordem_id: UUID
    id: UUID = field(default_factory=uuid4)
    ocorrido_em: datetime = field(default_factory=lambda: datetime.now(UTC))
    causation_id: UUID | None = None

    @property
    def tipo(self) -> str:
        return type(self).__name__.removesuffix("Event")
