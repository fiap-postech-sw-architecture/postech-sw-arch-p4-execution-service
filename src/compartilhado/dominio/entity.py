from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4


@dataclass(eq=False)
class Entity:
    """Objeto com identidade: igualdade e hash pelo ``id``, que nao muda."""

    id: UUID = field(default_factory=uuid4)

    def __setattr__(self, name: str, value: object) -> None:
        # Identidade imutavel: a primeira atribuicao de ``id`` (no __init__ do
        # dataclass) passa; reatribuicao e rejeitada. A reidratacao do
        # SQLAlchemy escreve direto no __dict__, sem passar por aqui.
        if name == "id" and "id" in self.__dict__:
            msg = "Identidade da entidade nao pode ser alterada apos criacao"
            raise AttributeError(msg)
        super().__setattr__(name, value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
