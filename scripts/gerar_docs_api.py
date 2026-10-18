"""Gera os documentos da API em docs/api a partir do proprio app (make docs-api).

- ``docs/api/openapi.json``: o ``app.openapi()`` do servico, o mesmo do ``/docs``.
- ``docs/api/execucao.postman_collection.json``: colecao Postman v2.1 com uma
  requisicao por metodo e rota do OpenAPI, cada uma com corpo e resposta de
  exemplo (``EXEMPLOS``) e as variaveis ``{{baseUrl}}``, ``{{token}}``,
  ``{{ordem_id}}`` e ``{{sku}}``.

Os dois saem juntos, do mesmo OpenAPI, para nao divergirem. O script so monta o
app e le o OpenAPI dele: nao sobe servidor nem banco. ``tests/unitarios/
test_documentacao.py`` reprova quando os arquivos ficam atras do codigo, quando
falta exemplo para uma rota, quando um exemplo sai do schema da rota ou quando a
colecao perde o token, o ``noauth`` das sondas, o ``Content-Type`` dos corpos, o
``{{baseUrl}}``, a resposta de exemplo ou uma das quatro variaveis.
"""

from __future__ import annotations

import json
import re
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

RAIZ = Path(__file__).resolve().parents[1]
DOCS_API = RAIZ / "docs" / "api"
OPENAPI = DOCS_API / "openapi.json"
COLECAO = DOCS_API / "execucao.postman_collection.json"

ESQUEMA_POSTMAN = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
DESCRICAO = (
    "Uma requisicao por rota do Execution Service, com corpo e resposta de "
    "exemplo. baseUrl aponta para o Kong do kind (https://localhost/execucao; o "
    "certificado e autoassinado: desligue a verificacao de SSL do Postman) e "
    "pode ser trocada pelo compose do servico (http://127.0.0.1:8003), onde so "
    "as sondas respondem sem o OS Service. token e o access_token do POST "
    "/api/v1/autenticacao/login do OS Service (admin ou mecanico); ordem_id e "
    "uma ordem que o OS Service abriu (GET /api/v1/diagnosticos?status=AGUARDANDO "
    "a mostra); sku e a peca cadastrada pela requisicao de cadastro. Gerada por "
    "scripts/gerar_docs_api.py (make docs-api); o OpenAPI esta em "
    "docs/api/openapi.json."
)
VARIAVEIS = {
    "baseUrl": "https://localhost/execucao",
    "token": "",
    "ordem_id": "3c9a7e10-5b2f-4f6d-8a41-0e2d9b7c6f55",
    "sku": "PEC-CORREIA-DENTADA",
}

_ORDEM = VARIAVEIS["ordem_id"]
_MECANICO = "7b1d2c3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"
_VEICULO = {"placa": "BRA2E19", "marca": "Fiat", "modelo": "Argo", "ano": 2022}
_ITENS = [
    {"tipo": "servico", "codigo": "SRV-FREIOS", "quantidade": 1},
    {"tipo": "peca", "codigo": "PEC-PASTILHA-FREIO", "quantidade": 1},
]
_PECA_NOVA = {
    "id": "5f0c6b2e-8d1a-4c3b-9e7f-2a4b6c8d0e1f",
    "sku": VARIAVEIS["sku"],
    "nome": "Correia dentada",
    "quantidade_disponivel": 8,
    "quantidade_reservada": 0,
    "quantidade_livre": 8,
    "ativo": True,
}
_DIAGNOSTICO = {
    "ordem_id": _ORDEM,
    "status": "AGUARDANDO",
    "veiculo": _VEICULO,
    "descricao_problema": "Barulho ao frear",
    "mecanico_id": None,
    "itens": [],
    "observacoes": "",
    "solicitado_em": "2026-10-18T13:00:00Z",
    "iniciado_em": None,
    "concluido_em": None,
    "descartado_em": None,
}
_EM_ANDAMENTO = {
    **_DIAGNOSTICO,
    "status": "EM_ANDAMENTO",
    "mecanico_id": _MECANICO,
    "iniciado_em": "2026-10-18T13:05:00Z",
}
_OBSERVACOES = "Pastilhas dianteiras no limite"
_EXECUCAO = {
    "ordem_id": _ORDEM,
    "status": "EM_EXECUCAO",
    "prioridade": "normal",
    "enfileirada_em": "2026-10-18T14:00:00Z",
    "veiculo": _VEICULO,
    "mecanico_id": _MECANICO,
    "iniciada_em": "2026-10-18T14:10:00Z",
    "finalizada_em": None,
    "cancelada_em": None,
}


def _pagina(item: dict[str, Any]) -> dict[str, Any]:
    return {"items": [item], "total": 1, "offset": 0, "limit": 20}


# Ordem dos fluxos do README; as chaves sao as do OpenAPI (metodo, rota).
EXEMPLOS: dict[tuple[str, str], dict[str, Any]] = {
    ("get", "/api/v1/saude"): {"status": 200, "resposta": {"status": "ok"}},
    ("get", "/api/v1/saude/pronto"): {"status": 200, "resposta": {"status": "ok"}},
    ("post", "/api/v1/estoque"): {
        "corpo": {
            "sku": VARIAVEIS["sku"],
            "nome": "Correia dentada",
            "quantidade_disponivel": 8,
        },
        "status": 201,
        "resposta": _PECA_NOVA,
    },
    ("get", "/api/v1/estoque"): {"status": 200, "resposta": _pagina(_PECA_NOVA)},
    ("get", "/api/v1/estoque/{sku}"): {"status": 200, "resposta": _PECA_NOVA},
    ("patch", "/api/v1/estoque/{sku}/quantidade"): {
        "corpo": {"quantidade_disponivel": 12},
        "status": 200,
        "resposta": {**_PECA_NOVA, "quantidade_disponivel": 12, "quantidade_livre": 12},
    },
    ("put", "/api/v1/estoque/{sku}"): {
        "corpo": {"nome": "Correia dentada (kit)", "ativo": True},
        "status": 200,
        "resposta": {**_PECA_NOVA, "nome": "Correia dentada (kit)"},
    },
    ("delete", "/api/v1/estoque/{sku}"): {"status": 204},
    ("get", "/api/v1/diagnosticos"): {
        "query": {"status": "AGUARDANDO"},
        "status": 200,
        "resposta": _pagina(_DIAGNOSTICO),
    },
    ("post", "/api/v1/diagnosticos/{ordem_id}/inicio"): {
        "status": 200,
        "resposta": _EM_ANDAMENTO,
    },
    ("post", "/api/v1/diagnosticos/{ordem_id}/conclusao"): {
        "corpo": {"itens": _ITENS, "observacoes": _OBSERVACOES},
        "status": 200,
        "resposta": {
            **_EM_ANDAMENTO,
            "status": "CONCLUIDO",
            "itens": _ITENS,
            "observacoes": _OBSERVACOES,
            "concluido_em": "2026-10-18T13:40:00Z",
        },
    },
    ("get", "/api/v1/fila"): {
        "status": 200,
        "resposta": _pagina(
            {
                "posicao": 1,
                "ordem_id": _ORDEM,
                "prioridade": "normal",
                "enfileirada_em": "2026-10-18T14:00:00Z",
                "veiculo": _VEICULO,
            }
        ),
    },
    ("post", "/api/v1/execucoes/{ordem_id}/inicio"): {
        "status": 200,
        "resposta": _EXECUCAO,
    },
    ("post", "/api/v1/execucoes/{ordem_id}/finalizacao"): {
        "status": 200,
        "resposta": {
            **_EXECUCAO,
            "status": "FINALIZADA",
            "finalizada_em": "2026-10-18T16:30:00Z",
        },
    },
}


def _json(documento: object) -> str:
    return json.dumps(documento, indent=2, ensure_ascii=False) + "\n"


def _requisicao(
    metodo: str, rota: str, operacao: dict[str, Any], exemplo: dict[str, Any]
) -> dict[str, Any]:
    caminho = re.sub(r"\{(\w+)\}", r"{{\1}}", rota)
    query = exemplo.get("query", {})
    url: dict[str, Any] = {
        "raw": "{{baseUrl}}" + caminho + (f"?{urlencode(query)}" if query else ""),
        "host": ["{{baseUrl}}"],
        "path": caminho.strip("/").split("/"),
    }
    if query:
        url["query"] = [
            {"key": chave, "value": valor} for chave, valor in query.items()
        ]
    pedido: dict[str, Any] = {
        "method": metodo.upper(),
        "header": [],
        "url": url,
        "description": operacao.get("description", operacao["summary"]),
    }
    # Rota sem 401 no OpenAPI (as sondas) nao leva o token da colecao.
    if "401" not in operacao["responses"]:
        pedido["auth"] = {"type": "noauth"}
    if "corpo" in exemplo:
        pedido["header"] = [{"key": "Content-Type", "value": "application/json"}]
        pedido["body"] = {
            "mode": "raw",
            "raw": _json(exemplo["corpo"]).rstrip("\n"),
            "options": {"raw": {"language": "json"}},
        }
    status = HTTPStatus(exemplo["status"])
    resposta: dict[str, Any] = {
        "name": f"{status.value} {status.phrase}",
        "originalRequest": pedido,
        "status": status.phrase,
        "code": status.value,
        "header": [],
        "body": "",
    }
    if "resposta" in exemplo:
        resposta["header"] = [{"key": "Content-Type", "value": "application/json"}]
        resposta["body"] = _json(exemplo["resposta"]).rstrip("\n")
        resposta["_postman_previewlanguage"] = "json"
    return {"name": operacao["summary"], "request": pedido, "response": [resposta]}


def colecao(openapi: dict[str, Any]) -> dict[str, Any]:
    """Colecao Postman v2.1 do OpenAPI: uma pasta por tag, uma requisicao por rota.

    Raises:
        ValueError: ``EXEMPLOS`` nao cobre exatamente os pares do OpenAPI.
    """
    pares = {(metodo, rota) for rota, ops in openapi["paths"].items() for metodo in ops}
    if pares != EXEMPLOS.keys():
        faltam = sorted(pares - EXEMPLOS.keys())
        sobram = sorted(EXEMPLOS.keys() - pares)
        msg = f"EXEMPLOS fora do OpenAPI: faltam {faltam}, sobram {sobram}"
        raise ValueError(msg)
    pastas: dict[str, list[dict[str, Any]]] = {}
    for (metodo, rota), exemplo in EXEMPLOS.items():
        operacao = openapi["paths"][rota][metodo]
        pastas.setdefault(operacao["tags"][0], []).append(
            _requisicao(metodo, rota, operacao, exemplo)
        )
    return {
        "info": {
            "name": openapi["info"]["title"],
            "description": DESCRICAO,
            "schema": ESQUEMA_POSTMAN,
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}],
        },
        "variable": [
            {"key": chave, "value": valor} for chave, valor in VARIAVEIS.items()
        ],
        "item": [{"name": nome, "item": itens} for nome, itens in pastas.items()],
    }


def main() -> int:
    # O import monta o app (src.main o cria no import); os testes so usam colecao().
    from src.main import app

    documento = app.openapi()
    try:
        postman = colecao(documento)
    except ValueError as erro:
        print(erro, file=sys.stderr)
        return 1
    DOCS_API.mkdir(parents=True, exist_ok=True)
    OPENAPI.write_text(_json(documento))
    COLECAO.write_text(_json(postman))
    print(f"atualizados: {OPENAPI.relative_to(RAIZ)} e {COLECAO.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
