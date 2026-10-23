"""Documentos do servico: docs/api, o curl de cada rota, os links e a arquitetura.

O README segue o esqueleto comum dos servicos e guarda as ancoras que o
platform cita.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import src.mapeamentos  # noqa: F401 - registra as tabelas de todos os contextos
from src.compartilhado.infraestrutura.database import metadata
from src.main import criar_app
from tests.conftest import provedor_dos_testes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

RAIZ = Path(__file__).resolve().parents[2]
README = RAIZ / "README.md"
ARQUITETURA = RAIZ / "docs" / "arquitetura.md"
EXEMPLOS_CURL = RAIZ / "docs" / "api" / "exemplos.md"
DOCUMENTOS = sorted((RAIZ / "docs").glob("**/*.md"))
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
    # Sem o FormatChecker o jsonschema ignora o `format` (uuid, date-time).
    validador = Draft202012Validator(
        {**schema, "components": openapi["components"]},
        format_checker=FormatChecker(),
    )
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


_TOKEN_DA_COLECAO = {
    "type": "bearer",
    "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}],
}
_CORPO_JSON = {"key": "Content-Type", "value": "application/json"}
_VARIAVEIS_DA_COLECAO = ["baseUrl", "ordem_id", "sku", "token"]


def _promessas_quebradas(openapi: dict[str, Any], colecao: dict[str, Any]) -> list[str]:
    """O que a documentacao promete da colecao e ela nao cumpre.

    O token da colecao, herdado pelas rotas com 401, e ``noauth`` nas outras (as
    sondas); ``Content-Type`` em todo corpo; a URL a partir de ``{{baseUrl}}``; e
    uma resposta de exemplo por requisicao; e as variaveis ``baseUrl``, ``token``,
    ``ordem_id`` e ``sku``, todas declaradas e so elas usadas.
    """
    erros = []
    if colecao.get("auth") != _TOKEN_DA_COLECAO:
        erros.append("colecao sem o token {{token}} como bearer")
    declaradas = sorted(v["key"] for v in colecao.get("variable", []))
    if declaradas != _VARIAVEIS_DA_COLECAO:
        erros.append(f"variaveis da colecao fora das prometidas: {declaradas}")
    usadas = set(re.findall(r"\{\{(\w+)\}\}", json.dumps(colecao)))
    if usadas - set(declaradas):
        erros.append(
            f"variavel usada e nao declarada: {sorted(usadas - set(declaradas))}"
        )
    for item in _requisicoes(colecao):
        pedido = item["request"]
        metodo, rota = _par(pedido)
        onde = f"{metodo.upper()} {rota}"
        protegida = "401" in openapi["paths"][rota][metodo]["responses"]
        if pedido.get("auth") != (None if protegida else {"type": "noauth"}):
            erros.append(f"{onde}: auth fora do padrao (token so nas rotas com 401)")
        if "body" in pedido and _CORPO_JSON not in pedido["header"]:
            erros.append(f"{onde}: corpo sem Content-Type: application/json")
        url = pedido["url"]
        if url["host"] != ["{{baseUrl}}"] or not url["raw"].startswith("{{baseUrl}}/"):
            erros.append(f"{onde}: URL sem a variavel baseUrl")
        if not item["response"]:
            erros.append(f"{onde}: sem resposta de exemplo")
    return erros


def test_colecao_versionada_e_a_do_script_cumprem_o_que_a_documentacao_promete(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    assert _promessas_quebradas(openapi, json.loads(COLECAO.read_text())) == []
    assert _promessas_quebradas(openapi, gerador.colecao(openapi)) == []


def test_colecao_que_quebra_cada_promessa_e_apontada(openapi: dict[str, Any]) -> None:
    colecao = json.loads(COLECAO.read_text())
    itens = {_par(item["request"]): item for item in _requisicoes(colecao)}
    del colecao["auth"]
    del itens["get", "/api/v1/saude"]["request"]["auth"]
    itens["get", "/api/v1/fila"]["request"]["auth"] = {"type": "noauth"}
    itens["post", "/api/v1/estoque"]["request"]["header"] = []
    itens["get", "/api/v1/estoque"]["request"]["url"]["raw"] = (
        "http://127.0.0.1:8003/api/v1/estoque?limit=20"
    )
    itens["delete", "/api/v1/estoque/{sku}"]["response"] = []
    colecao["variable"] = [v for v in colecao["variable"] if v["key"] != "sku"]

    assert sorted(_promessas_quebradas(openapi, colecao)) == [
        "DELETE /api/v1/estoque/{sku}: sem resposta de exemplo",
        "GET /api/v1/estoque: URL sem a variavel baseUrl",
        "GET /api/v1/fila: auth fora do padrao (token so nas rotas com 401)",
        "GET /api/v1/saude: auth fora do padrao (token so nas rotas com 401)",
        "POST /api/v1/estoque: corpo sem Content-Type: application/json",
        "colecao sem o token {{token}} como bearer",
        "variaveis da colecao fora das prometidas: ['baseUrl', 'ordem_id', 'token']",
        "variavel usada e nao declarada: ['sku']",
    ]


def _exemplos_fora_do_schema(
    openapi: dict[str, Any], colecao: dict[str, Any]
) -> list[str]:
    """Corpos e respostas de exemplo da colecao que o schema da rota recusa."""
    erros: list[str] = []
    for item in _requisicoes(colecao):
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
    return erros


def test_exemplos_da_colecao_seguem_o_schema_da_rota(openapi: dict[str, Any]) -> None:
    assert _exemplos_fora_do_schema(openapi, json.loads(COLECAO.read_text())) == []


def test_exemplo_fora_do_schema_ou_do_formato_e_apontado(
    openapi: dict[str, Any],
) -> None:
    colecao = json.loads(COLECAO.read_text())
    itens = {_par(item["request"]): item for item in _requisicoes(colecao)}
    itens["get", "/api/v1/fila"]["request"]["body"] = {"mode": "raw", "raw": "{}"}
    itens["delete", "/api/v1/estoque/{sku}"]["response"][0]["body"] = "{}"
    peca = itens["get", "/api/v1/estoque/{sku}"]["response"][0]
    peca["body"] = json.dumps({**json.loads(peca["body"]), "id": "nao-e-uuid"})
    fila = itens["get", "/api/v1/diagnosticos"]["response"][0]
    pagina = json.loads(fila["body"])
    pagina["items"][0]["solicitado_em"] = "ontem de manha"
    fila["body"] = json.dumps(pagina)

    assert sorted(_exemplos_fora_do_schema(openapi, colecao)) == [
        "DELETE /api/v1/estoque/{sku} resposta: a rota nao declara corpo JSON",
        "GET /api/v1/diagnosticos resposta: 'ontem de manha' is not a 'date-time'",
        "GET /api/v1/estoque/{sku} resposta: 'nao-e-uuid' is not a 'uuid'",
        "GET /api/v1/fila: corpo numa rota sem requestBody",
    ]


def test_rota_nova_sem_exemplo_reprova_a_geracao(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    com_rota_nova = json.loads(json.dumps(openapi))
    com_rota_nova["paths"]["/api/v1/nova"] = {"get": {"summary": "x", "tags": ["X"]}}

    with pytest.raises(ValueError, match="faltam \\[\\('get', '/api/v1/nova'\\)\\]"):
        gerador.colecao(com_rota_nova)


@pytest.fixture
def docs_api_temporario(
    gerador: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """O ``make docs-api`` escrevendo em ``tmp_path``, nao no repositorio."""
    destino = tmp_path / "docs" / "api"
    monkeypatch.setattr(gerador, "RAIZ", tmp_path)
    monkeypatch.setattr(gerador, "DOCS_API", destino)
    monkeypatch.setattr(gerador, "OPENAPI", destino / OPENAPI.name)
    monkeypatch.setattr(gerador, "COLECAO", destino / COLECAO.name)
    return destino


def test_make_docs_api_escreve_os_arquivos_versionados_e_repete_igual(
    gerador: ModuleType,
    docs_api_temporario: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versionados = {
        OPENAPI.name: OPENAPI.read_bytes(),
        COLECAO.name: COLECAO.read_bytes(),
    }

    assert gerador.main() == 0
    primeira = {a.name: a.read_bytes() for a in docs_api_temporario.iterdir()}
    assert gerador.main() == 0
    segunda = {a.name: a.read_bytes() for a in docs_api_temporario.iterdir()}

    assert primeira == segunda == versionados
    aviso = f"atualizados: docs/api/{OPENAPI.name} e docs/api/{COLECAO.name}\n"
    assert capsys.readouterr().out == aviso * 2


def test_make_docs_api_sem_exemplo_de_uma_rota_sai_com_1_sem_escrever(
    gerador: ModuleType,
    docs_api_temporario: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sem_a_sonda = dict(gerador.EXEMPLOS)
    del sem_a_sonda["get", "/api/v1/saude"]
    monkeypatch.setattr(gerador, "EXEMPLOS", sem_a_sonda)

    assert gerador.main() == 1
    assert "faltam [('get', '/api/v1/saude')]" in capsys.readouterr().err
    assert not docs_api_temporario.exists()


def test_arquitetura_existe_e_o_readme_a_linka() -> None:
    assert ARQUITETURA.is_file()
    assert "](docs/arquitetura.md)" in README.read_text()


def test_readme_linka_cada_documento_de_docs() -> None:
    readme = README.read_text()
    caminhos = [documento.relative_to(RAIZ).as_posix() for documento in DOCUMENTOS]

    assert [c for c in caminhos if f"]({c}" not in readme] == []


def _fora_de_codigo(texto: str) -> Iterator[str]:
    """Linhas do Markdown fora dos blocos de codigo."""
    em_codigo = False
    for linha in texto.splitlines():
        if linha.lstrip().startswith("```"):
            em_codigo = not em_codigo
        elif not em_codigo:
            yield linha


def _ancoras(documento: Path) -> set[str]:
    """Ancoras que o GitHub gera para os titulos (o repetido ganha -1, -2...)."""
    ancoras: set[str] = set()
    vistas: Counter[str] = Counter()
    for linha in _fora_de_codigo(documento.read_text()):
        if titulo := re.fullmatch(r"#{1,6} (.+)", linha):
            ancora = re.sub(r"[^\w\- ]", "", titulo[1].strip().lower())
            ancora = ancora.replace(" ", "-")
            ancoras.add(f"{ancora}-{vistas[ancora]}" if vistas[ancora] else ancora)
            vistas[ancora] += 1
    return ancoras


def _links_quebrados(documento: Path) -> list[str]:
    """Links relativos do documento cujo arquivo ou ancora nao existe.

    Os absolutos (o ``platform`` e outros sites) ficam de fora: o teste roda
    sem rede.
    """
    quebrados = []
    for linha in _fora_de_codigo(documento.read_text()):
        for alvo in re.findall(r"\]\(([^)\s]+)\)", linha):
            if alvo.startswith(("http://", "https://")):
                continue
            caminho, _, ancora = alvo.partition("#")
            arquivo = documento.parent / caminho if caminho else documento
            if not arquivo.exists() or (
                ancora and arquivo.suffix == ".md" and ancora not in _ancoras(arquivo)
            ):
                quebrados.append(alvo)
    return quebrados


@pytest.mark.parametrize(
    "documento",
    [README, *DOCUMENTOS],
    ids=lambda documento: documento.relative_to(RAIZ).as_posix(),
)
def test_links_relativos_apontam_arquivo_e_ancora_que_existem(
    documento: Path,
) -> None:
    assert _links_quebrados(documento) == []


def test_link_para_arquivo_ou_ancora_que_nao_existe_e_apontado(
    tmp_path: Path,
) -> None:
    (tmp_path / "outro.md").write_text(
        "# Título\n\n## Seção `um`, 2\n\n## Seção `um`, 2\n"
    )
    (tmp_path / "codigo.py").write_text("# comentario, nao titulo\n")
    documento = tmp_path / "doc.md"
    documento.write_text(
        "# Aqui\n\n"
        "[a](outro.md) [b](outro.md#título) [c](outro.md#seção-um-2-1) [d](#aqui)\n"
        "[e](codigo.py#L1) [f](https://exemplo.invalid/x.md#nada)\n"
        "[g](sumiu.md) [h](outro.md#seção-dois) [i](#ali)\n"
        "```bash\n[j](sumiu.md)\n```\n"
    )

    assert _links_quebrados(documento) == ["sumiu.md", "outro.md#seção-dois", "#ali"]


# Os titulos do README que o platform cita por ancora (entrega da fase 4 e matriz
# de rastreabilidade). Renomear um deles quebra o link do outro lado sem aviso.
ANCORAS_CITADAS_NO_README = frozenset({"cobertura", "mutação"})
# Os titulos de nivel 2, na ordem: o esqueleto que os tres servicos repetem. O
# sumario sao duas linhas de links, sem titulo proprio.
ESQUELETO_DO_README = [
    "Como rodar",
    "Como testar",
    "Cobertura",
    "API",
    "Mensageria",
    "Arquitetura",
    "Observabilidade",
    "CI/CD",
    "Segurança",
    "Decisões e rastreabilidade",
]


def test_readme_guarda_as_ancoras_que_o_platform_cita() -> None:
    assert _ancoras(README) >= ANCORAS_CITADAS_NO_README
    # O platform cita tambem docs/arquitetura.md#3-modelo-de-dados.
    assert "3-modelo-de-dados" in _ancoras(ARQUITETURA)


def test_readme_segue_o_esqueleto_comum_dos_servicos() -> None:
    texto = "\n".join(_fora_de_codigo(README.read_text()))

    assert re.findall(r"^## (.+)$", texto, re.MULTILINE) == ESQUELETO_DO_README
    assert re.search(r"^\*\*Sumário\*\*: ", texto, re.MULTILINE) is not None


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


def _erros_dos_curls(openapi: dict[str, Any], texto: str) -> list[str]:
    """Erros dos ``curl`` dos blocos ``bash`` de ``texto`` contra o OpenAPI.

    Rota sem ``curl``; ``curl`` sem o token numa rota com 401, sem
    ``Content-Type: application/json`` ou com um corpo que a rota recusa.
    """
    curls = [c for a in _comandos_curl(texto) if (c := _curl(a))]
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
                erros.append(f"{onde}: sem curl nos exemplos")
            for curl in casados:
                cabecalhos = [h.lower() for h in curl.cabecalhos]
                com_token = any(
                    h.startswith("authorization: bearer ") for h in cabecalhos
                )
                if "401" in operacao["responses"] and not com_token:
                    erros.append(f"{onde}: curl sem o token")
                if "requestBody" in operacao:
                    if "content-type: application/json" not in cabecalhos:
                        erros.append(
                            f"{onde}: corpo sem Content-Type: application/json"
                        )
                    corpo = json.loads(curl.corpo or "null")
                    erros += _fora_do_schema(
                        openapi, operacao["requestBody"], corpo, onde
                    )
                elif curl.corpo is not None:
                    erros.append(f"{onde}: corpo numa rota sem requestBody")
    return erros


def test_exemplos_tem_um_curl_por_rota_com_token_e_corpo_aceito(
    openapi: dict[str, Any],
) -> None:
    assert _erros_dos_curls(openapi, EXEMPLOS_CURL.read_text()) == []


def test_curl_que_falta_ou_que_a_rota_recusa_e_apontado(
    openapi: dict[str, Any],
) -> None:
    rotas = ("/api/v1/saude", "/api/v1/estoque", "/api/v1/fila")
    recorte = {**openapi, "paths": {rota: openapi["paths"][rota] for rota in rotas}}
    texto = (
        "```bash\n"
        "curl -sk -X GET \"$B/api/v1/saude\" -d '{}'\n"
        'curl -sk -X POST "$B/api/v1/estoque" -H "Authorization: Bearer $T" \\\n'
        '  -d \'{"sku": "PEC-X", "nome": "X", "quantidade_disponivel": 1}\'\n'
        'curl -sk "$B/api/v1/fila"\n'
        'curl -sk "$B/metrics"\n'
        "```\n"
    )

    assert sorted(_erros_dos_curls(recorte, texto)) == [
        "GET /api/v1/estoque: sem curl nos exemplos",
        "GET /api/v1/fila: curl sem o token",
        "GET /api/v1/saude: corpo numa rota sem requestBody",
        "POST /api/v1/estoque: corpo sem Content-Type: application/json",
    ]


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
