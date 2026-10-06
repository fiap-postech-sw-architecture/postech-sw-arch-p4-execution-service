from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.compartilhado.dominio.value_object import ValueObject

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

_PLACA_ANTIGA = re.compile(r"[A-Z]{3}\d{4}")
_PLACA_MERCOSUL = re.compile(r"[A-Z]{3}\d[A-Z]\d{2}")
ANO_PRIMEIRO_CARRO: Final = 1886
TAMANHO_MAXIMO_TEXTO: Final = 100
# Marcador da eliminacao LGPD, o mesmo do p3 (PlacaAnonimizada): unico por
# veiculo e sem nada do titular.
PREFIXO_ANONIMIZADO: Final = "ANONIMIZADO:"


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
    para achar o carro no patio, sem consultar outro servico. ``veiculo_id``
    liga o retrato ao cadastro do OS: na eliminacao LGPD (``AnonimizarVeiculo``)
    a placa vira ``ANONIMIZADO:{veiculo_id}``.

    O construtor nao valida: e a reidratacao do que ja foi gravado, inclusive a
    placa anonimizada, que nao tem formato de placa. Retrato vindo de fora passa
    por ``validado``, que normaliza e confere formato, textos e ano.
    """

    veiculo_id: UUID
    placa: str
    marca: str
    modelo: str
    ano: int

    def validado(self, agora: datetime) -> Veiculo:
        """Copia normalizada (placa sem hifen e em maiusculas, textos aparados).

        Raises:
            ValorInvalidoError: placa fora dos formatos antigo e Mercosul, marca
                ou modelo vazios ou longos, ano fora de 1887 ate o ano seguinte
                ao de ``agora`` (ano-modelo).
        """
        placa = self.placa.strip().upper().replace("-", "")
        if not (_PLACA_ANTIGA.fullmatch(placa) or _PLACA_MERCOSUL.fullmatch(placa)):
            # Sem ecoar o valor: placa e PII e a mensagem volta no corpo do 422.
            msg = "Placa invalida (formatos aceitos: ABC1234 ou ABC1D23)"
            raise ValorInvalidoError(msg)
        ano_maximo = agora.year + 1
        if not ANO_PRIMEIRO_CARRO < self.ano <= ano_maximo:
            minimo = ANO_PRIMEIRO_CARRO + 1
            msg = f"Ano do veiculo deve estar entre {minimo} e {ano_maximo}"
            raise ValorInvalidoError(msg)
        return replace(
            self,
            placa=placa,
            marca=_texto(self.marca, "Marca"),
            modelo=_texto(self.modelo, "Modelo"),
        )

    @property
    def anonimizado(self) -> bool:
        return self.placa.startswith(PREFIXO_ANONIMIZADO)

    def anonimizar(self) -> Veiculo:
        """O mesmo retrato com a placa trocada pelo marcador da LGPD."""
        return replace(self, placa=f"{PREFIXO_ANONIMIZADO}{self.veiculo_id}")

    def __repr__(self) -> str:
        # Placa e PII: o repr default a vazaria em traceback e log.
        placa = self.placa if self.anonimizado else f"{self.placa[:2]}*****"
        return (
            f"Veiculo(veiculo_id={self.veiculo_id}, placa='{placa}', "
            f"marca={self.marca!r}, modelo={self.modelo!r}, ano={self.ano})"
        )
