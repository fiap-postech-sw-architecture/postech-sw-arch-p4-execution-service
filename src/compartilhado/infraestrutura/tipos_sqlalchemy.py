from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Dialect


class JsonDeDominio[T](TypeDecorator[T]):
    """Coluna JSONB lida e gravada como objeto de dominio (VO ou tupla de VOs).

    O agregado so enxerga o objeto; a forma JSON fica no par de funcoes
    passado pelo ``mapping.py`` do contexto.
    """

    impl = JSONB
    cache_ok = True

    def __init__(
        self, para_json: Callable[[T], Any], de_json: Callable[[Any], T]
    ) -> None:
        super().__init__()
        self._para_json = para_json
        self._de_json = de_json

    def process_bind_param(self, value: T | None, dialect: Dialect) -> Any:  # noqa: ANN401
        return None if value is None else self._para_json(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> T | None:  # noqa: ANN401
        return None if value is None else self._de_json(value)
