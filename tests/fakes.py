"""Fakes em memoria dos ports, para os testes unitarios dos casos de uso.

Implementam o comportamento do contrato (Protocol), nao so o tipo: os
agregados sao os reais, so a persistencia e a rede ficam em memoria.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from src.compartilhado.dominio.exceptions import DependenciaIndisponivelException
from src.estoque.aplicacao.use_cases import baixar_reserva, reserva_ativa
from src.execucao.aplicacao.events import PecaConsumida

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime
    from types import TracebackType
    from uuid import UUID

    from src.compartilhado.aplicacao.integration_event import IntegrationEvent
    from src.diagnostico.dominio.diagnostico import Diagnostico, StatusDiagnostico
    from src.estoque.dominio.item_estoque import ItemEstoque
    from src.estoque.dominio.reserva import Reserva
    from src.estoque.dominio.sku import Sku
    from src.execucao.aplicacao.ports import ItemDaFila
    from src.execucao.dominio.execucao import Execucao


class FakeUnitOfWork:
    """``eventos`` guarda so o que foi comitado (o que iria para a outbox)."""

    def __init__(self) -> None:
        self.eventos: list[IntegrationEvent] = []
        self.commits = 0
        self.rollbacks = 0
        self._pendentes: list[IntegrationEvent] = []

    def __enter__(self) -> Self:
        self._pendentes = []
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollback()

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        self._pendentes.append(evento)

    def commit(self) -> None:
        self.eventos.extend(self._pendentes)
        self._pendentes = []
        self.commits += 1

    def rollback(self) -> None:
        self._pendentes = []
        self.rollbacks += 1


class ItensEmMemoria:
    def __init__(self, *itens: ItemEstoque) -> None:
        self.itens: dict[Sku, ItemEstoque] = {item.sku: item for item in itens}
        self.locks: list[list[Sku]] = []

    def obter_por_sku(self, sku: Sku, *, com_lock: bool = False) -> ItemEstoque | None:
        return self.itens.get(sku)

    def obter_com_lock(self, skus: Collection[Sku]) -> dict[Sku, ItemEstoque]:
        self.locks.append(sorted(skus, key=str))
        return {sku: self.itens[sku] for sku in skus if sku in self.itens}

    def salvar(self, item: ItemEstoque) -> None:
        self.itens[item.sku] = item

    def listar(self, offset: int, limit: int) -> list[ItemEstoque]:
        ordenados = sorted(self.itens.values(), key=lambda i: str(i.sku))
        return ordenados[offset : offset + limit]

    def contar(self) -> int:
        return len(self.itens)


class ReservasEmMemoria:
    def __init__(self, *reservas: Reserva) -> None:
        self.reservas: dict[UUID, Reserva] = {r.ordem_id: r for r in reservas}

    def obter_por_ordem(
        self, ordem_id: UUID, *, com_lock: bool = False
    ) -> Reserva | None:
        return self.reservas.get(ordem_id)

    def salvar(self, reserva: Reserva) -> None:
        self.reservas[reserva.ordem_id] = reserva


class DiagnosticosEmMemoria:
    def __init__(self, *diagnosticos: Diagnostico) -> None:
        self.diagnosticos: dict[UUID, Diagnostico] = {
            d.ordem_id: d for d in diagnosticos
        }
        self.salvos = 0

    def obter(self, ordem_id: UUID, *, com_lock: bool = False) -> Diagnostico | None:
        return self.diagnosticos.get(ordem_id)

    def salvar(self, diagnostico: Diagnostico) -> None:
        self.diagnosticos[diagnostico.ordem_id] = diagnostico
        self.salvos += 1

    def _filtrados(self, status: StatusDiagnostico | None) -> list[Diagnostico]:
        return sorted(
            (d for d in self.diagnosticos.values() if status in (None, d.status)),
            key=lambda d: d.solicitado_em,
        )

    def listar(
        self, status: StatusDiagnostico | None, offset: int, limit: int
    ) -> list[Diagnostico]:
        return self._filtrados(status)[offset : offset + limit]

    def contar(self, status: StatusDiagnostico | None) -> int:
        return len(self._filtrados(status))


class ExecucoesEmMemoria:
    def __init__(self, *execucoes: Execucao) -> None:
        self.execucoes: dict[UUID, Execucao] = {e.ordem_id: e for e in execucoes}

    def obter(self, ordem_id: UUID, *, com_lock: bool = False) -> Execucao | None:
        return self.execucoes.get(ordem_id)

    def salvar(self, execucao: Execucao) -> None:
        self.execucoes[execucao.ordem_id] = execucao


class FilaFixa:
    """Fila de leitura com posicao fixa (a ordenacao real e testada no Postgres)."""

    def __init__(self, posicao: int = 1) -> None:
        self._posicao = posicao

    def listar(self, offset: int, limit: int) -> list[ItemDaFila]:
        return []

    def contar(self) -> int:
        return 0

    def posicao(self, execucao: Execucao) -> int:
        return self._posicao


class EstoqueEmMemoria:
    """``EstoquePort`` com as funcoes reais do estoque sobre repositorios em memoria."""

    def __init__(self, itens: ItensEmMemoria, reservas: ReservasEmMemoria) -> None:
        self._itens = itens
        self._reservas = reservas
        self.baixas = 0

    def tem_reserva_ativa(self, ordem_id: UUID) -> bool:
        return reserva_ativa(self._reservas, ordem_id)

    def consumir_reserva(
        self, ordem_id: UUID, agora: datetime
    ) -> list[PecaConsumida] | None:
        self.baixas += 1
        reserva = baixar_reserva(self._itens, self._reservas, ordem_id, agora)
        if reserva is None:
            return None
        return [
            PecaConsumida(sku=str(linha.sku), quantidade=linha.quantidade)
            for linha in reserva.itens
        ]


class ValidadorFake:
    def __init__(
        self, invalidos: Sequence[str] = (), *, indisponivel: bool = False
    ) -> None:
        self._invalidos = list(invalidos)
        self._indisponivel = indisponivel
        self.chamadas: list[dict[str, Any]] = []

    def codigos_invalidos(
        self, *, servicos: Sequence[str], pecas: Sequence[str]
    ) -> list[str]:
        self.chamadas.append({"servicos": list(servicos), "pecas": list(pecas)})
        if self._indisponivel:
            msg = "Billing fora do ar"
            raise DependenciaIndisponivelException(msg)
        return self._invalidos


class CatalogoFake:
    def __init__(self, indisponiveis: Sequence[str] = ()) -> None:
        self._indisponiveis = set(indisponiveis)
        self.chamadas: list[list[str]] = []

    def skus_indisponiveis(self, skus: Sequence[str]) -> list[str]:
        self.chamadas.append(list(skus))
        return [sku for sku in skus if sku in self._indisponiveis]
