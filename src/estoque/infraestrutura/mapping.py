from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Integer,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.types import TypeDecorator

from src.compartilhado.infraestrutura.database import mapper_registry, metadata
from src.compartilhado.infraestrutura.tipos_sqlalchemy import (
    JsonDeDominio,
    check_de_enum,
    reidratar,
)
from src.estoque.dominio.item_estoque import ItemEstoque
from src.estoque.dominio.reserva import Faltante, ItemReserva, Reserva, StatusReserva
from src.estoque.dominio.sku import TAMANHO_MAXIMO_SKU, Sku

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect


class SkuType(TypeDecorator[Sku]):
    """VARCHAR <-> ``Sku``; aceita ``str`` no bind (filtro vindo de outro contexto)."""

    impl = String(TAMANHO_MAXIMO_SKU)
    cache_ok = True

    def process_bind_param(
        self, value: Sku | str | None, dialect: Dialect
    ) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Sku | None:
        return None if value is None else reidratar(Sku, value)


def _itens_para_json(itens: tuple[ItemReserva, ...]) -> list[dict[str, Any]]:
    return [{"sku": str(item.sku), "quantidade": item.quantidade} for item in itens]


def _itens_de_json(dados: list[dict[str, Any]]) -> tuple[ItemReserva, ...]:
    return tuple(
        ItemReserva(sku=Sku(item["sku"]), quantidade=item["quantidade"])
        for item in dados
    )


def _faltantes_para_json(faltantes: tuple[Faltante, ...]) -> list[dict[str, Any]]:
    return [
        {"sku": str(f.sku), "solicitado": f.solicitado, "disponivel": f.disponivel}
        for f in faltantes
    ]


def _faltantes_de_json(dados: list[dict[str, Any]]) -> tuple[Faltante, ...]:
    return tuple(
        Faltante(
            sku=Sku(f["sku"]), solicitado=f["solicitado"], disponivel=f["disponivel"]
        )
        for f in dados
    )


itens_estoque_table = Table(
    "itens_estoque",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("sku", SkuType(), nullable=False),
    Column("nome", String(255), nullable=False),
    Column("quantidade_disponivel", Integer, nullable=False),
    Column("quantidade_reservada", Integer, nullable=False),
    Column("ativo", Boolean, nullable=False),
    UniqueConstraint("sku", name="uq_itens_estoque_sku"),
    # Defesa em profundidade do invariante do agregado: nem um bug de
    # concorrencia deixa saldo negativo ou reserva acima do estoque fisico.
    CheckConstraint(
        "quantidade_disponivel >= 0", name="ck_itens_estoque_disponivel_nao_negativa"
    ),
    CheckConstraint(
        "quantidade_reservada >= 0 AND quantidade_reservada <= quantidade_disponivel",
        name="ck_itens_estoque_reservada_dentro_do_disponivel",
    ),
)

reservas_table = Table(
    "reservas",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("ordem_id", Uuid, nullable=False),
    Column("status", Enum(StatusReserva, native_enum=False, length=20), nullable=False),
    Column("itens", JsonDeDominio(_itens_para_json, _itens_de_json), nullable=False),
    # So a reserva RECUSADA tem faltantes: a resposta repetida sai deles.
    Column(
        "faltantes",
        JsonDeDominio(_faltantes_para_json, _faltantes_de_json),
        nullable=False,
    ),
    Column("criada_em", DateTime(timezone=True), nullable=False),
    Column("encerrada_em", DateTime(timezone=True), nullable=True),
    UniqueConstraint("ordem_id", name="uq_reservas_ordem_id"),
    check_de_enum("status", StatusReserva, "ck_reservas_status"),
)

mapper_registry.map_imperatively(
    ItemEstoque,
    itens_estoque_table,
    properties={
        "id": itens_estoque_table.c.id,
        "_sku": itens_estoque_table.c.sku,
        "_nome": itens_estoque_table.c.nome,
        "_quantidade_disponivel": itens_estoque_table.c.quantidade_disponivel,
        "_quantidade_reservada": itens_estoque_table.c.quantidade_reservada,
        "_ativo": itens_estoque_table.c.ativo,
    },
)

mapper_registry.map_imperatively(
    Reserva,
    reservas_table,
    properties={
        "id": reservas_table.c.id,
        "_ordem_id": reservas_table.c.ordem_id,
        "_status": reservas_table.c.status,
        "_itens": reservas_table.c.itens,
        "_faltantes": reservas_table.c.faltantes,
        "_criada_em": reservas_table.c.criada_em,
        "_encerrada_em": reservas_table.c.encerrada_em,
    },
)
