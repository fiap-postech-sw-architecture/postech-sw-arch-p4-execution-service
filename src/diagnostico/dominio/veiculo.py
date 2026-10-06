from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.compartilhado.dominio.value_object import ValueObject

_PLACA_ANTIGA = re.compile(r"[A-Z]{3}\d{4}")
_PLACA_MERCOSUL = re.compile(r"[A-Z]{3}\d[A-Z]\d{2}")
ANO_PRIMEIRO_CARRO: Final = 1886
TAMANHO_MAXIMO_TEXTO: Final = 100


def _texto(valor: str, campo: str) -> str:
    valor = valor.strip()
    if not valor or len(valor) > TAMANHO_MAXIMO_TEXTO:
        msg = f"{campo} do veiculo deve ter de 1 a {TAMANHO_MAXIMO_TEXTO} caracteres"
        raise ValorInvalidoError(msg)
    return valor


@dataclass(frozen=True, slots=True)
class Veiculo(ValueObject):
    """Retrato do veiculo enviado pelo OS Service em ``SolicitarDiagnostico``.

    Copia local (o dono do cadastro e o OS Service): o mecanico precisa dela
    para achar o carro no patio, sem consultar outro servico.
    """

    placa: str
    marca: str
    modelo: str
    ano: int

    def __post_init__(self) -> None:
        placa = self.placa.strip().upper().replace("-", "")
        if not (_PLACA_ANTIGA.fullmatch(placa) or _PLACA_MERCOSUL.fullmatch(placa)):
            # Sem ecoar o valor: placa e PII e a mensagem volta no corpo do 422.
            msg = "Placa invalida (formatos aceitos: ABC1234 ou ABC1D23)"
            raise ValorInvalidoError(msg)
        object.__setattr__(self, "placa", placa)
        object.__setattr__(self, "marca", _texto(self.marca, "Marca"))
        object.__setattr__(self, "modelo", _texto(self.modelo, "Modelo"))
        ano_maximo = datetime.now(UTC).year + 1  # ano-modelo seguinte
        if not ANO_PRIMEIRO_CARRO < self.ano <= ano_maximo:
            minimo = ANO_PRIMEIRO_CARRO + 1
            msg = f"Ano do veiculo deve estar entre {minimo} e {ano_maximo}"
            raise ValorInvalidoError(msg)

    def __repr__(self) -> str:
        # Placa e PII: o repr default a vazaria em traceback e log.
        return (
            f"Veiculo(placa='{self.placa[:2]}*****', marca={self.marca!r}, "
            f"modelo={self.modelo!r}, ano={self.ano})"
        )
