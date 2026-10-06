from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.estoque.aplicacao.use_cases import ReservarPecas
from src.estoque.dominio.reserva import ItemReserva
from src.estoque.dominio.sku import Sku
from src.estoque.infraestrutura.repository import (
    ItemEstoqueSQLAlchemyRepository,
    ReservaSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session, sessionmaker

URL = "/api/v1/estoque"


@pytest.fixture
def admin(autenticar: Callable[..., dict[str, str]]) -> dict[str, str]:
    return autenticar("admin")


def _cadastrar(
    api: TestClient, admin: dict[str, str], sku: str, quantidade: int
) -> None:
    resposta = api.post(
        URL,
        json={"sku": sku, "nome": f"Peca {sku}", "quantidade_disponivel": quantidade},
        headers=admin,
    )
    assert resposta.status_code == 201, resposta.text


def _reservar(
    session_factory: sessionmaker[Session], sku: str, quantidade: int
) -> None:
    with session_factory() as session:
        ReservarPecas(
            ItemEstoqueSQLAlchemyRepository(session),
            ReservaSQLAlchemyRepository(session),
            SQLAlchemyUnitOfWork(lambda: session),
        ).executar(uuid4(), [ItemReserva(Sku(sku), quantidade)])


def test_crud_completo_pelo_admin(api: TestClient, admin: dict[str, str]) -> None:
    resposta = api.post(
        URL,
        json={"sku": "PEC-VELA", "nome": "Vela de ignicao", "quantidade_disponivel": 0},
        headers=admin,
    )
    assert resposta.status_code == 201
    criado = resposta.json()
    assert criado | {"id": None} == {
        "id": None,
        "sku": "PEC-VELA",
        "nome": "Vela de ignicao",
        "quantidade_disponivel": 0,
        "quantidade_reservada": 0,
        "quantidade_livre": 0,
        "ativo": True,
    }

    ajuste = api.patch(
        f"{URL}/PEC-VELA/quantidade", json={"quantidade_disponivel": 12}, headers=admin
    )
    assert ajuste.json()["quantidade_livre"] == 12

    atualizado = api.put(
        f"{URL}/PEC-VELA", json={"nome": "Vela NGK", "ativo": True}, headers=admin
    )
    assert atualizado.json()["nome"] == "Vela NGK"

    assert api.delete(f"{URL}/PEC-VELA", headers=admin).status_code == 204
    assert api.get(f"{URL}/PEC-VELA", headers=admin).json()["ativo"] is False


def test_listagem_paginada(api: TestClient, admin: dict[str, str]) -> None:
    for sku in ["PEC-C", "PEC-A", "PEC-B"]:
        _cadastrar(api, admin, sku, 1)
    resposta = api.get(URL, params={"offset": 1, "limit": 1}, headers=admin)
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert [item["sku"] for item in corpo["items"]] == ["PEC-B"]
    assert (corpo["total"], corpo["offset"], corpo["limit"]) == (3, 1, 1)


@pytest.mark.parametrize("papel", ["mecanico", "atendente"])
def test_leitura_para_quem_opera_a_oficina(
    api: TestClient,
    admin: dict[str, str],
    autenticar: Callable[..., dict[str, str]],
    papel: str,
) -> None:
    _cadastrar(api, admin, "PEC-VELA", 1)
    headers = autenticar(papel)
    assert api.get(URL, headers=headers).status_code == 200
    assert api.get(f"{URL}/PEC-VELA", headers=headers).status_code == 200


@pytest.mark.parametrize("papel", ["mecanico", "atendente"])
def test_escrita_so_para_admin(
    api: TestClient,
    admin: dict[str, str],
    autenticar: Callable[..., dict[str, str]],
    papel: str,
) -> None:
    _cadastrar(api, admin, "PEC-VELA", 1)
    headers = autenticar(papel)
    corpo = {"sku": "PEC-NOVA", "nome": "x", "quantidade_disponivel": 1}
    assert api.post(URL, json=corpo, headers=headers).status_code == 403
    assert (
        api.put(
            f"{URL}/PEC-VELA", json={"nome": "x", "ativo": True}, headers=headers
        ).status_code
        == 403
    )
    assert (
        api.patch(
            f"{URL}/PEC-VELA/quantidade",
            json={"quantidade_disponivel": 9},
            headers=headers,
        ).status_code
        == 403
    )
    assert api.delete(f"{URL}/PEC-VELA", headers=headers).status_code == 403


def test_sem_token(api: TestClient) -> None:
    resposta = api.get(URL)
    assert resposta.status_code == 401
    assert resposta.json()["erro"]["codigo"] == "NAO_AUTENTICADO"


def test_sku_duplicado(api: TestClient, admin: dict[str, str]) -> None:
    _cadastrar(api, admin, "PEC-VELA", 1)
    resposta = api.post(
        URL,
        json={"sku": "PEC-VELA", "nome": "y", "quantidade_disponivel": 2},
        headers=admin,
    )
    assert resposta.status_code == 409
    assert resposta.json()["erro"]["codigo"] == "ENTIDADE_DUPLICADA"


@pytest.mark.parametrize(
    "corpo",
    [
        {"sku": "pec-minusculo", "nome": "x", "quantidade_disponivel": 1},
        {"sku": "PEC-X", "nome": "", "quantidade_disponivel": 1},
        {"sku": "PEC-X", "nome": "x", "quantidade_disponivel": -1},
        {"sku": "PEC-X", "nome": "x", "quantidade_disponivel": 1, "extra": True},
    ],
)
def test_validacao_de_entrada(
    api: TestClient, admin: dict[str, str], corpo: dict[str, object]
) -> None:
    assert api.post(URL, json=corpo, headers=admin).status_code == 422


def test_sku_inexistente_e_sku_mal_formado_no_path(
    api: TestClient, admin: dict[str, str]
) -> None:
    resposta = api.get(f"{URL}/PEC-NADA", headers=admin)
    assert resposta.status_code == 404
    assert (
        resposta.json()["erro"]["mensagem"] == "Item de estoque PEC-NADA nao encontrado"
    )
    assert api.get(f"{URL}/pec-nada", headers=admin).status_code == 422


def test_reserva_ativa_protege_o_saldo(
    api: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
) -> None:
    _cadastrar(api, admin, "PEC-VELA", 5)
    _reservar(session_factory, "PEC-VELA", 3)

    item = api.get(f"{URL}/PEC-VELA", headers=admin).json()
    assert (item["quantidade_reservada"], item["quantidade_livre"]) == (3, 2)
    ajuste = api.patch(
        f"{URL}/PEC-VELA/quantidade", json={"quantidade_disponivel": 2}, headers=admin
    )
    assert ajuste.status_code == 409
    assert api.delete(f"{URL}/PEC-VELA", headers=admin).status_code == 409
    desativar = api.put(
        f"{URL}/PEC-VELA", json={"nome": "x", "ativo": False}, headers=admin
    )
    assert desativar.status_code == 409
