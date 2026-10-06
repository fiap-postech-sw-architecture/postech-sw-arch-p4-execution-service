from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import (
    OperacaoNaoPermitidaException,
    TransicaoStatusInvalidaException,
    ValorInvalidoError,
)
from src.compartilhado.dominio.maquina_de_estados import validar_transicao
from src.compartilhado.dominio.texto import texto_valido
from src.compartilhado.dominio.value_object import ValueObject

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.veiculo import Veiculo

# Codigos da tabela de precos do Billing (servicos e pecas): PEC-VELA, SRV-...
PADRAO_CODIGO: Final = r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$"
TAMANHO_MAXIMO_CODIGO: Final = 64
TAMANHO_MAXIMO_TEXTO_LIVRE: Final = 2000
_REGEX_CODIGO = re.compile(PADRAO_CODIGO)


class StatusDiagnostico(StrEnum):
    AGUARDANDO = "AGUARDANDO"
    EM_ANDAMENTO = "EM_ANDAMENTO"
    CONCLUIDO = "CONCLUIDO"
    DESCARTADO = "DESCARTADO"


class TipoItem(StrEnum):
    SERVICO = "servico"
    PECA = "peca"


_TRANSICOES: dict[StatusDiagnostico, frozenset[StatusDiagnostico]] = {
    StatusDiagnostico.AGUARDANDO: frozenset(
        {StatusDiagnostico.EM_ANDAMENTO, StatusDiagnostico.DESCARTADO}
    ),
    StatusDiagnostico.EM_ANDAMENTO: frozenset(
        {StatusDiagnostico.CONCLUIDO, StatusDiagnostico.DESCARTADO}
    ),
    StatusDiagnostico.CONCLUIDO: frozenset({StatusDiagnostico.DESCARTADO}),
    StatusDiagnostico.DESCARTADO: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ItemDiagnostico(ValueObject):
    """Servico ou peca apontado pelo mecanico; ``codigo`` e o do Billing."""

    tipo: TipoItem
    codigo: str
    quantidade: int

    def __post_init__(self) -> None:
        if not isinstance(self.tipo, TipoItem):
            # ValueError (nao TypeError): invariante de VO, que a API devolve em 422.
            msg = "Tipo do item deve ser 'servico' ou 'peca'"
            raise ValorInvalidoError(msg)
        # fullmatch: com match, o `$` aceitaria um "\n" final.
        if len(self.codigo) > TAMANHO_MAXIMO_CODIGO or not _REGEX_CODIGO.fullmatch(
            self.codigo
        ):
            msg = f"Codigo de item invalido: {self.codigo!r}"
            raise ValorInvalidoError(msg)
        if self.quantidade <= 0:
            msg = f"Quantidade do item {self.codigo} deve ser positiva"
            raise ValorInvalidoError(msg)


def _texto_livre(valor: str, campo: str, *, obrigatorio: bool) -> str:
    return texto_valido(
        valor,
        campo,
        maximo=TAMANHO_MAXIMO_TEXTO_LIVRE,
        obrigatorio=obrigatorio,
        multilinha=True,
    )


def _validar_itens(itens: Sequence[ItemDiagnostico]) -> None:
    if not itens:
        msg = "Diagnostico concluido precisa de ao menos um servico ou peca"
        raise ValorInvalidoError(msg)
    chaves = [(item.tipo, item.codigo) for item in itens]
    if len(chaves) != len(set(chaves)):
        msg = "Cada servico ou peca deve aparecer uma unica vez no diagnostico"
        raise ValorInvalidoError(msg)


@dataclass(eq=False, kw_only=True)
class Diagnostico(AggregateRoot):
    """Diagnostico de uma ordem; a identidade e o proprio ``ordem_id``.

    AGUARDANDO -> EM_ANDAMENTO -> CONCLUIDO; qualquer estado nao final ->
    DESCARTADO (compensacao da saga). So o mecanico que iniciou conclui. A
    lapide (descarte que chegou antes do ``SolicitarDiagnostico``) e o unico
    diagnostico sem veiculo e sem descricao.
    """

    _veiculo: Veiculo | None
    # Texto livre fora do repr (traceback e log): pode trazer nome ou placa.
    _descricao_problema: str | None = field(repr=False)
    _status: StatusDiagnostico = StatusDiagnostico.AGUARDANDO
    _mecanico_id: UUID | None = None
    _itens: tuple[ItemDiagnostico, ...] = ()
    _observacoes: str = field(default="", repr=False)
    _solicitado_em: datetime
    _iniciado_em: datetime | None = None
    _concluido_em: datetime | None = None
    _descartado_em: datetime | None = None

    def __post_init__(self) -> None:
        if self._veiculo is None or self._descricao_problema is None:
            if self._status is not StatusDiagnostico.DESCARTADO:
                msg = "So a lapide (DESCARTADO) fica sem veiculo e descricao"
                raise ValorInvalidoError(msg)
            return
        self._descricao_problema = _texto_livre(
            self._descricao_problema, "Descricao do problema", obrigatorio=True
        )

    @classmethod
    def solicitar(
        cls,
        *,
        ordem_id: UUID,
        veiculo: Veiculo,
        descricao_problema: str,
        agora: datetime,
    ) -> Diagnostico:
        return cls(
            id=ordem_id,
            _veiculo=veiculo.validado(agora),
            _descricao_problema=descricao_problema,
            _solicitado_em=agora,
        )

    @classmethod
    def lapide(cls, *, ordem_id: UUID, agora: datetime) -> Diagnostico:
        """Descarte que chegou antes do ``SolicitarDiagnostico``: nasce DESCARTADO.

        Sem veiculo nem descricao (o comando original nunca chegou); a
        solicitacao atrasada acha a lapide pela chave ``ordem_id`` e e
        descartada sem efeito (RFC-004, secao 4.5).
        """
        return cls(
            id=ordem_id,
            _veiculo=None,
            _descricao_problema=None,
            _status=StatusDiagnostico.DESCARTADO,
            _solicitado_em=agora,
            _descartado_em=agora,
        )

    @property
    def ordem_id(self) -> UUID:
        return self.id

    @property
    def veiculo(self) -> Veiculo | None:
        return self._veiculo

    @property
    def descricao_problema(self) -> str | None:
        return self._descricao_problema

    @property
    def status(self) -> StatusDiagnostico:
        return self._status

    @property
    def mecanico_id(self) -> UUID | None:
        return self._mecanico_id

    @property
    def itens(self) -> tuple[ItemDiagnostico, ...]:
        return self._itens

    @property
    def observacoes(self) -> str:
        return self._observacoes

    @property
    def solicitado_em(self) -> datetime:
        return self._solicitado_em

    @property
    def iniciado_em(self) -> datetime | None:
        return self._iniciado_em

    @property
    def concluido_em(self) -> datetime | None:
        return self._concluido_em

    @property
    def descartado_em(self) -> datetime | None:
        return self._descartado_em

    def iniciar(self, mecanico_id: UUID, agora: datetime) -> bool:
        """AGUARDANDO -> EM_ANDAMENTO; repetir pelo mesmo mecanico e no-op (False)."""
        if self._status is StatusDiagnostico.EM_ANDAMENTO:
            if self._mecanico_id == mecanico_id:
                return False
            msg = "Diagnostico ja iniciado por outro mecanico"
            raise TransicaoStatusInvalidaException(msg)
        self._transicionar(StatusDiagnostico.EM_ANDAMENTO)
        self._mecanico_id = mecanico_id
        self._iniciado_em = agora
        return True

    def concluido_por(self, mecanico_id: UUID) -> bool:
        return (
            self._status is StatusDiagnostico.CONCLUIDO
            and self._mecanico_id == mecanico_id
        )

    def validar_conclusao(
        self, mecanico_id: UUID, itens: Sequence[ItemDiagnostico], observacoes: str
    ) -> None:
        """Checa estado, responsavel, itens e observacoes sem alterar nada.

        Separado de ``concluir`` para o caso de uso validar antes da chamada
        remota ao Billing, sem segurar lock de linha durante a rede.
        """
        validar_transicao(
            _TRANSICOES,
            self._status,
            StatusDiagnostico.CONCLUIDO,
            agregado="Diagnostico",
        )
        if self._mecanico_id != mecanico_id:
            msg = "Somente o mecanico que iniciou o diagnostico pode conclui-lo"
            raise OperacaoNaoPermitidaException(msg)
        _validar_itens(itens)
        _texto_livre(observacoes, "Observacoes", obrigatorio=False)

    def concluir(
        self,
        mecanico_id: UUID,
        itens: Sequence[ItemDiagnostico],
        observacoes: str,
        agora: datetime,
    ) -> bool:
        """EM_ANDAMENTO -> CONCLUIDO; repetir pelo mesmo mecanico e no-op (False)."""
        if self.concluido_por(mecanico_id):
            return False
        self.validar_conclusao(mecanico_id, itens, observacoes)
        self._observacoes = _texto_livre(observacoes, "Observacoes", obrigatorio=False)
        self._itens = tuple(itens)
        self._status = StatusDiagnostico.CONCLUIDO
        self._concluido_em = agora
        return True

    def descartar(self, agora: datetime) -> None:
        """Compensacao: qualquer estado -> DESCARTADO. Idempotente."""
        if self._status is StatusDiagnostico.DESCARTADO:
            return
        self._transicionar(StatusDiagnostico.DESCARTADO)
        self._descartado_em = agora

    def _transicionar(self, para: StatusDiagnostico) -> None:
        validar_transicao(_TRANSICOES, self._status, para, agregado="Diagnostico")
        self._status = para
