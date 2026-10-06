from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

from src.compartilhado.dominio.veiculo import Veiculo

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Dialect


class DadoPersistidoInvalidoError(Exception):
    """Linha gravada que nao reidrata o objeto de dominio.

    Defeito do servidor (dado corrompido ou migracao faltando): vira 500, nunca o
    422 de um ``ValorInvalidoError``, que culparia o cliente pelo dado do banco.
    """


def reidratar[T](de: Callable[[Any], T], valor: Any) -> T:  # noqa: ANN401 - valor cru do driver (JSON ou texto)
    """Aplica ``de`` ao valor lido; falha vira ``DadoPersistidoInvalidoError``."""
    try:
        return de(valor)
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"Valor gravado fora do formato do dominio ({type(exc).__name__})"
        raise DadoPersistidoInvalidoError(msg) from exc


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

    # Any nos dois metodos: e a assinatura do TypeDecorator (valor JSON do driver).
    def process_bind_param(self, value: T | None, dialect: Dialect) -> Any:  # noqa: ANN401
        return None if value is None else self._para_json(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> T | None:  # noqa: ANN401
        return None if value is None else reidratar(self._de_json, value)


def _veiculo_para_json(veiculo: Veiculo) -> dict[str, Any]:
    return {
        "veiculo_id": str(veiculo.veiculo_id),
        "placa": veiculo.placa,
        "marca": veiculo.marca,
        "modelo": veiculo.modelo,
        "ano": veiculo.ano,
    }


def _veiculo_de_json(dados: dict[str, Any]) -> Veiculo:
    # Sem revalidar: a placa anonimizada (LGPD) nao tem formato de placa.
    return Veiculo(
        veiculo_id=UUID(dados["veiculo_id"]),
        placa=dados["placa"],
        marca=dados["marca"],
        modelo=dados["modelo"],
        ano=dados["ano"],
    )


def retrato_do_veiculo() -> JsonDeDominio[Veiculo]:
    """Tipo da coluna JSONB com o retrato do veiculo (diagnostico e execucao)."""
    return JsonDeDominio(_veiculo_para_json, _veiculo_de_json)
