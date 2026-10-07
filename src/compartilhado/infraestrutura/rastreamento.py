"""Trace dos passos retomados pelo mecanico (ADR-043; RFC-004, secao 9).

Diagnostico e execucao sao os registros que esperam o mecanico: guardam o
``traceparent``/``tracestate`` de quem os criou, o span do consumidor do
``SolicitarDiagnostico`` ou do ``AgendarExecucao``. A acao do mecanico que
retoma o passo roda como filha desse contexto, com span link para o trace de
quem a retomou (a requisicao HTTP, quando instrumentada): o fato que ela grava
na outbox sai no mesmo trace da saga, e a saga vira um trace so.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Link
from sqlalchemy import Column, Text, select

from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    contexto_de,
    tracer,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from sqlalchemy import Table
    from sqlalchemy.orm import Session


def colunas_do_contexto_de_espera() -> tuple[Column[str], Column[str]]:
    """``traceparent`` e ``tracestate``, preenchidas no INSERT com o span corrente.

    Fora do mapeamento do agregado (contexto de trace nao e dominio): quem cria
    o registro e o comando da saga, no span do consumidor.
    """
    return (
        Column(
            "traceparent",
            Text,
            nullable=True,
            default=lambda: contexto_atual().get("traceparent"),
        ),
        Column(
            "tracestate",
            Text,
            nullable=True,
            default=lambda: contexto_atual().get("tracestate"),
        ),
    )


@contextmanager
def retomando(
    sessao: Session, tabela: Table, ordem_id: UUID, passo: str
) -> Iterator[None]:
    """Roda o bloco como filho do contexto guardado no registro da ordem.

    O span do passo leva link para o span corrente (quem retomou), e o que o
    bloco gravar na outbox sai com o contexto dele. Sem registro, ou sem
    contexto guardado, o passo abre trace proprio. Excecao do caso de uso nao
    vai para o span: a mensagem dela pode trazer dado do pedido.
    """
    guardado = sessao.execute(
        select(tabela.c.traceparent, tabela.c.tracestate).where(
            tabela.c.ordem_id == ordem_id
        )
    ).one_or_none()
    quem_retomou = trace.get_current_span().get_span_context()
    with tracer.start_as_current_span(
        passo,
        context=contexto_de({} if guardado is None else dict(guardado._mapping)),
        links=[Link(quem_retomou)] if quem_retomou.is_valid else None,
        attributes={"correlation_id": str(ordem_id)},
        record_exception=False,
        set_status_on_exception=False,
    ):
        yield
