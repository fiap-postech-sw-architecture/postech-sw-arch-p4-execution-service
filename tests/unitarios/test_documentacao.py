"""Documentos do servico: docs/api, o curl de cada rota no README e a arquitetura."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator

import src.mapeamentos  # noqa: F401 - registra as tabelas de todos os contextos
from src.compartilhado.infraestrutura.database import metadata
from src.main import criar_app
from tests.conftest import provedor_dos_testes

if TYPE_CHECKING:
    from types import ModuleType

RAIZ = Path(__file__).resolve().parents[2]
README = RAIZ / "README.md"
ARQUITETURA = RAIZ / "docs" / "arquitetura.md"
OPENAPI = RAIZ / "docs" / "api" / "openapi.json"
COLECAO = RAIZ / "docs" / "api" / "execucao.postman_collection.json"
GERADOR = RAIZ / "scripts" / "gerar_docs_api.py"


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    # Ida e volta pelo JSON: e assim que o documento chega ao arquivo.
    return json.loads(json.dumps(criar_app(provedor_dos_testes()).openapi()))


@pytest.fixture(scope="module")
def gerador() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gerar_docs_api", GERADOR)
    assert spec is not None
    assert spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _pares(openapi: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted(
        (metodo, rota) for rota, ops in openapi["paths"].items() for metodo in ops
    )


def _requisicoes(colecao: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for pasta in colecao["item"] for item in pasta["item"]]


def _par(pedido: dict[str, Any]) -> tuple[str, str]:
    caminho = "/" + "/".join(pedido["url"]["path"])
    return pedido["method"].lower(), re.sub(r"\{\{(\w+)\}\}", r"{\1}", caminho)


def _fora_do_schema(
    openapi: dict[str, Any], declaracao: dict[str, Any], valor: object, onde: str
) -> list[str]:
    """Erros de ``valor`` contra o schema JSON de um requestBody ou de uma resposta."""
    schema = declaracao.get("content", {}).get("application/json", {}).get("schema")
    if schema is None:
        return [f"{onde}: a rota nao declara corpo JSON"]
    validador = Draft202012Validator({**schema, "components": openapi["components"]})
    return [f"{onde}: {erro.message}" for erro in validador.iter_errors(valor)]


def test_openapi_versionado_e_o_do_app(openapi: dict[str, Any]) -> None:
    versionado = json.loads(OPENAPI.read_text())

    assert versionado == openapi, (
        "docs/api/openapi.json atras do app: rode make docs-api"
    )


def test_colecao_postman_tem_uma_requisicao_por_rota_do_openapi(
    openapi: dict[str, Any],
) -> None:
    colecao = json.loads(COLECAO.read_text())

    pares = sorted(_par(item["request"]) for item in _requisicoes(colecao))

    assert pares == _pares(openapi)


def test_colecao_postman_e_a_que_o_script_gera(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    colecao = json.loads(COLECAO.read_text())

    assert colecao == gerador.colecao(openapi), "colecao atras do script: make docs-api"


def test_exemplos_da_colecao_seguem_o_schema_da_rota(openapi: dict[str, Any]) -> None:
    erros: list[str] = []
    for item in _requisicoes(json.loads(COLECAO.read_text())):
        metodo, rota = _par(item["request"])
        operacao = openapi["paths"][rota][metodo]
        onde = f"{metodo.upper()} {rota}"
        if "requestBody" in operacao:
            corpo = json.loads(item["request"].get("body", {}).get("raw", "null"))
            erros += _fora_do_schema(openapi, operacao["requestBody"], corpo, onde)
        elif "body" in item["request"]:
            erros.append(f"{onde}: corpo numa rota sem requestBody")
        for resposta in item["response"]:
            declarada = operacao["responses"][str(resposta["code"])]
            if resposta["body"]:
                valor = json.loads(resposta["body"])
                erros += _fora_do_schema(openapi, declarada, valor, f"{onde} resposta")

    assert erros == []


def test_rota_nova_sem_exemplo_reprova_a_geracao(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    com_rota_nova = json.loads(json.dumps(openapi))
    com_rota_nova["paths"]["/api/v1/nova"] = {"get": {"summary": "x", "tags": ["X"]}}

    with pytest.raises(ValueError, match="faltam \\[\\('get', '/api/v1/nova'\\)\\]"):
        gerador.colecao(com_rota_nova)


def test_arquitetura_existe_e_o_readme_a_linka() -> None:
    assert ARQUITETURA.is_file()
    assert "](docs/arquitetura.md)" in README.read_text()


def _colunas_do_er(texto: str) -> dict[str, set[str]]:
    """Tabela -> colunas do primeiro ``erDiagram`` do documento."""
    er = re.search(r"```mermaid\nerDiagram\n(.*?)```", texto, flags=re.DOTALL)
    assert er is not None, "docs/arquitetura.md sem o erDiagram do modelo"
    return {
        tabela: {linha.split()[1] for linha in corpo.strip().splitlines()}
        for tabela, corpo in re.findall(
            r"^\s*(\w+) \{\n(.*?)^\s*\}", er[1], re.M | re.S
        )
    }


def test_er_da_arquitetura_tem_toda_tabela_e_coluna_do_mapeamento() -> None:
    mapeado = {
        tabela.name: {coluna.name for coluna in tabela.columns}
        for tabela in metadata.sorted_tables
    }

    assert _colunas_do_er(ARQUITETURA.read_text()) == mapeado


_SEPARADORES = ";&|()\n"


def _comandos_curl(texto: str) -> list[list[str]]:
    """Argumentos de cada ``curl`` dos blocos ``bash``, separados como no shell.

    A quebra de linha com ``\\`` continua o comando; a quebra dentro de aspas (o
    JSON do corpo em varias linhas) fica no argumento; ``|``, ``;``, ``)`` e a
    quebra de linha comum terminam o comando.
    """
    comandos: list[list[str]] = []
    for bloco in re.findall(r"```bash\n(.*?)```", texto, flags=re.DOTALL):
        if "curl" not in bloco:
            continue
        lexer = shlex.shlex(
            bloco.replace("\\\n", " "), posix=True, punctuation_chars=_SEPARADORES
        )
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        atual: list[str] | None = None
        for token in lexer:
            if token == "curl":
                atual = []
                comandos.append(atual)
            elif token and set(token) <= set(_SEPARADORES):
                atual = None
            elif atual is not None:
                atual.append(token)
    return comandos


@dataclass(frozen=True)
class _Curl:
    metodo: str
    caminho: str
    corpo: str | None
    cabecalhos: tuple[str, ...]


def _curl(argumentos: list[str]) -> _Curl | None:
    """Metodo, caminho a partir de ``/api/v1/``, corpo e cabecalhos de um ``curl``."""
    metodo = corpo = url = None
    cabecalhos: list[str] = []
    restantes = iter(argumentos)
    for argumento in restantes:
        if argumento in {"-X", "--request"}:
            metodo = next(restantes)
        elif argumento in {"-d", "--data", "--data-raw"}:
            corpo = next(restantes)
        elif argumento in {"-H", "--header"}:
            cabecalhos.append(next(restantes))
        elif "/api/v1/" in argumento:
            url = argumento
    if url is None:
        return None
    caminho = url[url.index("/api/v1/") :].split("?")[0]
    # Como o curl: sem -X, -d vira POST.
    metodo = metodo or ("POST" if corpo is not None else "GET")
    return _Curl(metodo, caminho, corpo, tuple(cabecalhos))


def test_readme_tem_um_curl_por_rota_com_token_e_corpo_aceito(
    openapi: dict[str, Any],
) -> None:
    curls = [c for a in _comandos_curl(README.read_text()) if (c := _curl(a))]
    erros: list[str] = []
    for rota, operacoes in openapi["paths"].items():
        modelo = re.compile(re.sub(r"\{\w+\}", "[^/]+", rota))
        for metodo, operacao in operacoes.items():
            onde = f"{metodo.upper()} {rota}"
            casados = [
                c
                for c in curls
                if c.metodo == metodo.upper() and modelo.fullmatch(c.caminho)
            ]
            if not casados:
                erros.append(f"{onde}: sem curl no README")
            for curl in casados:
                com_token = any(
                    h.startswith("Authorization: Bearer ") for h in curl.cabecalhos
                )
                if "401" in operacao["responses"] and not com_token:
                    erros.append(f"{onde}: curl sem o token")
                if "requestBody" in operacao:
                    corpo = json.loads(curl.corpo or "null")
                    erros += _fora_do_schema(
                        openapi, operacao["requestBody"], corpo, onde
                    )
                elif curl.corpo is not None:
                    erros.append(f"{onde}: corpo numa rota sem requestBody")

    assert erros == []


def test_curl_em_varias_linhas_com_json_quebrado_e_lido_inteiro() -> None:
    texto = (
        "```bash\n"
        'export ID=$(curl -sk "$B/api/v1/x?y=1" \\\n'
        '  -H "Authorization: Bearer $T" | jq -r .id)\n'
        'curl -sk -X PUT "$B/api/v1/x/$ID" -d \'{"a": 1,\n'
        '   "b": 2}\'\n'
        "curl -sk \"$B/api/v1/x\" -d '{}'\n"
        "curl -sk -X POST \"$B/api/v1/y\" -d ''\n"
        "```\n"
    )

    curls = [_curl(argumentos) for argumentos in _comandos_curl(texto)]

    assert curls == [
        _Curl("GET", "/api/v1/x", None, ("Authorization: Bearer $T",)),
        _Curl("PUT", "/api/v1/x/$ID", '{"a": 1,\n   "b": 2}', ()),
        _Curl("POST", "/api/v1/x", "{}", ()),
        _Curl("POST", "/api/v1/y", "", ()),
    ]
