from __future__ import annotations

from dataclasses import dataclass

from src.compartilhado.dominio.entity import Entity


@dataclass(eq=False)
class AggregateRoot(Entity):
    """Raiz de agregado: unica porta de entrada para leitura e persistencia.

    Sem lista de eventos de proposito: as mensagens do catalogo da saga sao
    eventos de integracao montados pela camada de aplicacao (que conhece o
    contexto do comando) e gravados na outbox pela ``UnitOfWork``.
    """
