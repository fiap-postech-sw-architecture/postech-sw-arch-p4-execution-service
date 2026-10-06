from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, kw_only=True)
class IntegrationEvent:
    """Mensagem do catalogo da saga (brief secao 4) emitida por este servico.

    ``ordem_id`` e o correlation_id da saga; ``id`` vira o ``message_id`` do
    envelope e ``ocorrido_em`` o instante do fato. Os campos da subclasse, mais
    ``ordem_id``, formam o ``dados`` do envelope. O ``tipo`` e o nome da classe
    sem o sufixo ``Event`` (``PecasReservadasEvent`` -> ``PecasReservadas``).
    """

    ordem_id: UUID
    id: UUID = field(default_factory=uuid4)
    ocorrido_em: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def tipo(self) -> str:
        return type(self).__name__.removesuffix("Event")
