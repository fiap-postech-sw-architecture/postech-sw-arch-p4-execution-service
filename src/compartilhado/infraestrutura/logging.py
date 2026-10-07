from __future__ import annotations

import logging
import os
import re
import sys
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from typing import TextIO

# git_sha/git_date sao injetadas em build args -> ENV pelas pipelines
# (Makefile + Dockerfiles). Lidas uma vez no import e adicionadas a todo
# log structlog via processor -- assim ficam visiveis mesmo apos
# `clear_contextvars()` que o SecurityHeadersMiddleware faz a cada
# request. `[:12]` casa com o curto exibido no banner de boot.
_GIT_SHA = os.environ.get("PYTSTOP_GIT_SHA", "unknown")[:12]
_GIT_DATE = os.environ.get("PYTSTOP_GIT_DATE", "unknown")


def adicionar_versao_imagem(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Injeta git_sha/git_date em todo evento (sem sobrescrever explicit)."""
    event_dict.setdefault("git_sha", _GIT_SHA)
    event_dict.setdefault("git_date", _GIT_DATE)
    return event_dict


def adicionar_contexto_de_trace(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """``trace_id``/``span_id`` do span corrente: o Grafana leva do log ao trace."""
    contexto = trace.get_current_span().get_span_context()
    if contexto.is_valid:
        event_dict.setdefault("trace_id", format(contexto.trace_id, "032x"))
        event_dict.setdefault("span_id", format(contexto.span_id, "016x"))
    return event_dict


_CPF_PATTERN = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ_PATTERN = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
# Dominio casado label a label (`.` fora da classe): correcao do achado S5852
# (backtracking) do SonarQube no p3. O scrubber roda sobre o event_dict inteiro,
# tracebacks inclusos, sem limite de tamanho.
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)

# Telefone BR: duas formas estruturais, escolhidas para nao gerar falso-positivo
# em precos (`1500.00`), ids (`12345`), anos (`2026`), portas (`8000`) e CEPs
# (`12345-678`) -- nenhum deles tem o split `\d{4,5}-\d{4}` nem prefixo `+55`:
#   1. DDD (com/sem parenteses) + separador OPCIONAL + bloco local com hifen
#      4-4/5-4 -- cobre `(11)99999-0000` e `1199999-0000` alem dos formatados.
#      Colateral aceito (direcao LGPD-safe): ids numericos hifenizados com
#      shape 6+4 (`123456-7890`) tambem sao mascarados.
#   2. `+55` seguido de 10-11 digitos corridos -- cobre `+5511999990000` (o
#      prefixo de pais e estrutura suficiente; nada legitimo em log tem essa
#      forma). `+55 11999990000` (com espaco) e `11999990000` (sem nada) tem
#      11 digitos corridos com shape de CPF e caem no _CPF_PATTERN acima
#      antes desta regex; campos NOMEADOS telefone/celular/contato sao
#      mascarados pela denylist abaixo.
# Os ids do servico (`ordem_id`, `correlation_id`, `request_id`, ator e alvo) sao
# UUID, e o v4 traz entre os grupos trechos `dd-dddd-dddd` (`732ffc02-3465-4237-...`)
# que o split 4-4 casaria (cerca de 1,4% dos UUID saiam mascarados). Por isso o
# numero nao pode vir colado a digito hexadecimal, salvo quando abre com `(` ou
# `+`, e nao pode continuar em digito. Colado a letra fora de A-F, a hifen ou a
# espaco (`tel-11 99999-0000`, `tel(11)99999-0000`) continua mascarado; medido
# com 0 falso positivo em 400 mil UUID v4.
_TELEFONE_PATTERN = re.compile(
    r"(?:(?<![0-9A-Fa-f])|(?=[(+]))"  # nao colado a digito hexadecimal (UUID)
    r"(?:"
    r"(?:\+55[\s.-]?)?"  # codigo do pais opcional
    r"(?:\(\d{2}\)|\d{2})"  # DDD com ou sem parenteses
    r"[\s.-]?"  # separador opcional entre DDD e numero
    r"9?\d{4}-\d{4}"  # 8 ou 9 digitos com hifen 4-4/5-4
    r"|"
    r"\+55[\s.-]?\d{10,11}"  # +55 com numero corrido (sem hifen local)
    r")"
    r"(?!\d)"  # nao continua em digito (numero maior)
)

# Placa antiga (ABC1234, ABC-1234) e Mercosul (ABC1D23) solta em texto: o
# retrato do veiculo e PII (LGPD). Palavra inteira de 7 caracteres, entao uuid,
# sha e SKU com letras depois do hifen nao casam.
_PLACA_PATTERN = re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE)

# Denylist de chaves: quando o NOME do campo indica segredo ou PII, o valor
# inteiro e mascarado -- independente de casar regex. Cobre credenciais sem
# forma fixa (tokens, segredos), PII cujo valor pode nao ter estrutura
# detectavel (telefone sem formatacao, contato) e o texto livre do servico
# (descricao do problema, observacoes e motivo podem trazer nome ou placa).
_CHAVES_SENSIVEIS = frozenset(
    {
        "password",
        "senha",
        "senha_hash",
        "token",
        "secret",
        "authorization",
        "refresh_token",
        "access_token",
        "api_key",
        "telefone",
        "celular",
        "phone",
        "contato",
        "placa",
        "descricao_problema",
        "observacoes",
        "motivo",
    }
)

_MASCARA = "***"

# Loggers que o uvicorn configura com handler proprio + `propagate=False`.
# `configurar_logging` os religa ao root para passarem pelo scrubber (issue #86
# do p2: https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p2/issues/86).
_LOGGER_DE_ACESSO = "uvicorn.access"
_LOGGERS_UVICORN = ("uvicorn", "uvicorn.error", _LOGGER_DE_ACESSO)

# Teto de profundidade do scrub em estruturas aninhadas: protege contra payload
# ciclico ou patologico sem perder o aninhamento real (eventos tem 2 ou 3
# niveis, no maximo).
_MAX_SCRUB_DEPTH = 6


def _mask_cpf(match: re.Match[str]) -> str:
    raw = match.group().replace(".", "").replace("-", "")
    return f"***.***.{raw[6:9]}-**"


def _mask_cnpj(match: re.Match[str]) -> str:
    # Digitos do CNPJ NN.NNN.NNN/NNNN-NN: o terceiro grupo e raw[5:8].
    raw = match.group().replace(".", "").replace("/", "").replace("-", "")
    return f"**.***.{raw[5:8]}/****-**"


def _mask_email(match: re.Match[str]) -> str:
    email = match.group()
    local, domain = email.split("@", 1)
    masked_local = local[0] + "***" if local else "***"
    return f"{masked_local}@{domain}"


def _mask_placa(match: re.Match[str]) -> str:
    # Mesmo formato do repr do Veiculo: as 2 primeiras letras e o resto oculto.
    return f"{match.group()[:2]}*****"


def _mask_string(value: str) -> str:
    value = _CPF_PATTERN.sub(_mask_cpf, value)
    value = _CNPJ_PATTERN.sub(_mask_cnpj, value)
    value = _EMAIL_PATTERN.sub(_mask_email, value)
    value = _TELEFONE_PATTERN.sub(_MASCARA, value)
    return _PLACA_PATTERN.sub(_mask_placa, value)


def _chave_sensivel(key: Any) -> bool:  # noqa: ANN401  # chaves podem nao ser str
    return isinstance(key, str) and key.lower() in _CHAVES_SENSIVEIS


def _scrub_value(value: Any, depth: int) -> Any:  # noqa: ANN401 - qualquer JSON
    # Recursivamente normaliza qualquer estrutura JSON-like; o tipo de entrada
    # nao e conhecivel a priori, dai o Any.
    if depth >= _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, dict):
        # Mascara o valor inteiro quando a chave esta na denylist; senao desce.
        return {
            k: (_MASCARA if _chave_sensivel(k) else _scrub_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, depth + 1) for item in value)
    if isinstance(value, (set, frozenset)):
        limpo = {_scrub_value(item, depth + 1) for item in value}
        return limpo if isinstance(value, set) else frozenset(limpo)
    return value


def scrub_pii(
    _logger: Any,  # noqa: ANN401  # structlog bound logger; nao inspecionado
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor que mascara PII e segredos em todo o event_dict.

    Mascara CPF, CNPJ, email, telefone BR formatado e placa por regex de VALOR; e
    mascara o valor inteiro quando o NOME do campo esta na denylist
    `_CHAVES_SENSIVEIS` (password/token/placa/observacoes/...). Percorre
    recursivamente strings, dicts, listas e tuplas ate `_MAX_SCRUB_DEPTH` para
    pegar PII em payloads estruturados. Aplicado automaticamente pelo pipeline
    de logging (inclusive na chave `exception` do traceback, que
    `format_exc_info` monta ANTES deste processor) para impedir vazamento de PII
    em logs -- structlog e stdlib (LGPD).
    """
    for key, value in event_dict.items():
        event_dict[key] = _MASCARA if _chave_sensivel(key) else _scrub_value(value, 0)
    return event_dict


_MAX_ERRO_LEN = 200


def redigir_pii_erro(erro: str) -> str:
    """Remove PII (CPF, CNPJ, e-mail, telefone, placa) de strings de erro.

    Complementa o scrubber de log (``scrub_pii``): aquele atua no pipeline do
    structlog; esta funcao atua em strings devolvidas ao cliente (mensagem de
    ``ValueError`` no corpo do 422), que o scrubber de log nao alcanca.

    Trunca o resultado em ``_MAX_ERRO_LEN`` caracteres.
    """
    redacted = _mask_string(erro)
    if len(redacted) > _MAX_ERRO_LEN:
        redacted = redacted[:_MAX_ERRO_LEN] + "…"
    return redacted


# Cadeia COMPARTILHADA entre logs structlog e logs stdlib estrangeiros (uvicorn,
# bibliotecas, handler 500). ORDEM CRITICA (issue #86 do p2): `format_exc_info` monta a
# chave `exception` a partir do `exc_info` e DEVE vir ANTES de `scrub_pii`, senao
# o traceback (com possivel PII no repr da excecao) escapa do mascaramento.
# `StackInfoRenderer` (stack_info -> string) tambem precede o scrub, que entao
# mascara ambas as strings. Nenhum renderer final aqui: o `ProcessorFormatter`
# (abaixo) renderiza para JSON tanto os logs structlog quanto os stdlib.
def _cadeia_compartilhada() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        adicionar_versao_imagem,
        adicionar_contexto_de_trace,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        scrub_pii,
    ]


def configurar_logging(stream: TextIO | None = None) -> None:
    """Configura structlog e roteia o logging stdlib pelo mesmo scrubber de PII.

    Saida JSON com timestamp ISO. O ``ProcessorFormatter`` do root faz TODO log
    stdlib (handler 500, uvicorn, bibliotecas) passar pela
    ``_cadeia_compartilhada``, ``scrub_pii`` incluso, via ``foreign_pre_chain``,
    sem reprocessar o que ja veio do structlog: fecha a brecha da issue #86 do
    p2 (traceback cru com PII fora do pipeline). ``stream`` redireciona a saida
    (padrao ``sys.stdout``); os testes capturam por ele.
    """
    compartilhada = _cadeia_compartilhada()
    structlog.configure(
        processors=[
            *compartilhada,
            # Handoff para o ProcessorFormatter do root handler (nao renderiza aqui).
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        # Logs estrangeiros (stdlib) passam por esta cadeia ANTES da renderizacao;
        # logs ja-structlog ja a percorreram e a pulam (sem duplo processamento).
        foreign_pre_chain=compartilhada,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # Substitui handlers existentes (de uma chamada anterior, por exemplo): o
    # scrubber fica sendo o unico caminho de saida, idempotente em restart/teste.
    root.handlers = [handler]
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    _religar_loggers_do_uvicorn()


def _religar_loggers_do_uvicorn() -> None:
    # O uvicorn (lancado por CLI no container) instala handlers proprios com
    # `propagate=False`: seus logs nao passariam pelo scrub do root. Isto roda
    # com o app montado, depois de o uvicorn montar os loggers: tira os handlers
    # crus e religa a propagacao (JSON unico e scrubado). Issue #86 do p2.
    for nome in _LOGGERS_UVICORN:
        uvlog = logging.getLogger(nome)
        if nome == _LOGGER_DE_ACESSO and not uvlog.handlers and not uvlog.propagate:
            # `--no-access-log` deixa o logger assim, e o uvicorn decide por
            # `hasHandlers()`, a cada conexao, se escreve o acesso. Religar a
            # propagacao o ligaria de novo ao handler do root: a linha de texto
            # voltaria, em duplicidade com o `http_request` do middleware.
            continue
        uvlog.handlers = []
        uvlog.propagate = True
