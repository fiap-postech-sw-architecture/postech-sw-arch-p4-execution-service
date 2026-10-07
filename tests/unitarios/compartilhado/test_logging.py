from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import structlog
import uvicorn

from src.compartilhado.infraestrutura.logging import (
    adicionar_versao_imagem,
    configurar_logging,
    redigir_pii_erro,
    scrub_pii,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping


class TestLogging:
    def test_scrub_cpf(self) -> None:
        event_dict: dict[str, object] = {"event": "CPF 123.456.789-00"}
        result = scrub_pii(None, "info", event_dict)
        assert "123.456.789-00" not in str(result["event"])
        assert "***" in str(result["event"])

    def test_scrub_cnpj(self) -> None:
        event_dict: dict[str, object] = {"event": "CNPJ 12.345.678/0001-90"}
        result = scrub_pii(None, "info", event_dict)
        assert "12.345.678/0001-90" not in str(result["event"])

    def test_mascara_cnpj_preserva_o_terceiro_grupo_correto(self) -> None:
        # NN.NNN.NNN/NNNN-NN: o grupo visivel da mascara e o TERCEIRO
        # (digitos raw[5:8] = "678"), nao um fatiamento deslocado ("567").
        event_dict: dict[str, object] = {"event": "CNPJ 12.345.678/0001-90"}
        result = scrub_pii(None, "info", event_dict)
        assert "**.***.678/****-**" in str(result["event"])

    def test_scrub_email(self) -> None:
        event_dict: dict[str, object] = {"event": "Email user@example.com"}
        result = scrub_pii(None, "info", event_dict)
        assert "user@example.com" not in str(result["event"])
        assert "u***@example.com" in str(result["event"])

    def test_scrub_non_string_values(self) -> None:
        event_dict: dict[str, object] = {"count": 42}
        result = scrub_pii(None, "info", event_dict)
        assert result["count"] == 42

    def test_configurar_logging_instala_um_handler_json(
        self, log_capturado: io.StringIO
    ) -> None:
        # log_capturado roda configurar_logging(stream=...) e restaura no fim; o
        # pytest acrescenta os proprios handlers de captura ao root.
        proprios = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler.formatter, structlog.stdlib.ProcessorFormatter)
        ]
        assert len(proprios) == 1
        structlog.get_logger("test.configurar").info("evento_qualquer", chave=1)
        registro = json.loads(log_capturado.getvalue().splitlines()[-1])
        assert (registro["event"], registro["chave"]) == ("evento_qualquer", 1)

    def test_adicionar_versao_imagem_injeta_git_sha_e_date(self) -> None:
        # Defaults vem do env do processo (PYTSTOP_GIT_SHA/DATE); em
        # tests sem essas vars setadas, valor esperado e "unknown".
        result = adicionar_versao_imagem(None, "info", {"event": "boot"})
        assert "git_sha" in result
        assert "git_date" in result

    def test_adicionar_versao_imagem_nao_sobrescreve_explicit(self) -> None:
        result = adicionar_versao_imagem(
            None, "info", {"event": "x", "git_sha": "explicit"}
        )
        assert result["git_sha"] == "explicit"

    def test_scrub_cpf_sem_pontuacao(self) -> None:
        event_dict: dict[str, object] = {"event": "CPF 12345678900"}
        result = scrub_pii(None, "info", event_dict)
        assert "12345678900" not in str(result["event"])

    def test_scrub_cnpj_sem_pontuacao(self) -> None:
        event_dict: dict[str, object] = {"event": "CNPJ 12345678000190"}
        result = scrub_pii(None, "info", event_dict)
        assert "12345678000190" not in str(result["event"])

    def test_scrub_recursivo_em_dict_aninhado(self) -> None:
        event_dict: dict[str, object] = {
            "payload": {
                "cliente": {
                    "cpf": "123.456.789-00",
                    "email": "joao@example.com",
                }
            }
        }
        result = scrub_pii(None, "info", event_dict)
        assert "123.456.789-00" not in str(result["payload"])
        assert "joao@example.com" not in str(result["payload"])

    def test_scrub_recursivo_em_lista(self) -> None:
        event_dict: dict[str, object] = {
            "itens": [
                {"cpf": "123.456.789-00"},
                "CNPJ 12.345.678/0001-90",
            ]
        }
        result = scrub_pii(None, "info", event_dict)
        itens = result["itens"]
        assert "123.456.789-00" not in str(itens)
        assert "12.345.678/0001-90" not in str(itens)

    def test_scrub_recursivo_em_tupla(self) -> None:
        event_dict: dict[str, object] = {
            "pair": ("user@example.com", "other-value"),
        }
        result = scrub_pii(None, "info", event_dict)
        pair = result["pair"]
        assert "user@example.com" not in str(pair)
        assert "u***@example.com" in str(pair)

    def test_scrub_recursivo_em_set_e_frozenset(self) -> None:
        event_dict: dict[str, object] = {
            "emails": {"user@example.com"},
            "docs": frozenset({"CPF 123.456.789-00"}),
        }
        result = scrub_pii(None, "info", event_dict)
        assert isinstance(result["emails"], set)
        assert isinstance(result["docs"], frozenset)
        assert "user@example.com" not in str(result["emails"])
        assert "123.456.789-00" not in str(result["docs"])

    def test_scrub_respeita_profundidade_maxima(self) -> None:
        # Ate o nivel 5 o valor e mascarado; dali em diante passa como esta
        # (teto contra estrutura ciclica ou patologica, sem estourar a pilha).
        raso: dict[str, object] = {"next": {"doc": "CPF 123.456.789-00"}}
        fundo: dict[str, object] = {"doc": "CPF 123.456.789-00"}
        for _ in range(8):
            fundo = {"next": fundo}
        result = scrub_pii(None, "info", {"raso": raso, "fundo": fundo})
        assert "123.456.789-00" not in str(result["raso"])
        assert "123.456.789-00" in str(result["fundo"])


class TestScrubTelefone:
    """Telefone BR formatado deve ser mascarado; numero qualquer NAO (LGPD)."""

    @pytest.mark.parametrize(
        "telefone",
        [
            "(11) 99999-0000",
            "(11) 9999-0000",
            "+55 11 99999-0000",
            "+55 (11) 99999-0000",
            "11 99999-0000",
            # Sem espaco apos o DDD (issue #99 do p2): separador agora e opcional.
            "(11)99999-0000",
            "1199999-0000",
            # +55 com numero corrido, sem hifen local (issue #99 do p2).
            "+5511999990000",
            "+55 11999990000",
        ],
    )
    def test_telefone_formatado_mascarado(self, telefone: str) -> None:
        event_dict: dict[str, object] = {"event": f"contato {telefone}"}
        result = scrub_pii(None, "info", event_dict)
        text = str(result["event"])
        assert telefone not in text
        assert "***" in text

    @pytest.mark.parametrize(
        "nao_telefone",
        [
            "id 12345",  # id curto
            "valor R$ 1500.00",  # preco
            "ordem 998877",  # numero de ordem
            "ano 2026",
            "porta 8000",
            "cep 12345-678",  # bloco local 5-3 nao casa o split 4-4/5-4
            "id longo 123456789012345",  # 15 digitos corridos sem +55
        ],
    )
    def test_nao_telefone_preservado(self, nao_telefone: str) -> None:
        # Guard contra falso-positivo: numeros que nao sao telefone ficam intactos.
        event_dict: dict[str, object] = {"event": nao_telefone}
        result = scrub_pii(None, "info", event_dict)
        assert str(result["event"]) == nao_telefone

    def test_telefone_11_digitos_corrido_mascarado(self) -> None:
        # 11 digitos corridos tem o shape de CPF e caem no _CPF_PATTERN --
        # mascarado por valor de qualquer forma (issue #99 do p2). Campos NOMEADOS
        # telefone/celular/contato caem na denylist de chaves.
        event_dict: dict[str, object] = {"event": "retorno 11999990000"}
        result = scrub_pii(None, "info", event_dict)
        assert "11999990000" not in str(result["event"])

    @pytest.mark.parametrize(
        "texto",
        [
            pytest.param("tel (11) 99999-0000, ok", id="virgula-depois"),
            pytest.param("(+55 11 99999-0000)", id="entre-parenteses"),
            pytest.param("contato: 11 99999-0000.", id="ponto-final"),
            pytest.param("Key (contato)=(11 99999-0000) existe", id="detalhe-do-banco"),
        ],
    )
    def test_telefone_cercado_de_pontuacao_mascarado(self, texto: str) -> None:
        result = scrub_pii(None, "info", {"event": texto})
        assert "99999-0000" not in str(result["event"])
        assert "***" in str(result["event"])


# UUID v4 de verdade cujo grupo "02-3465-4237" (dd-dddd-dddd) casava com o telefone.
_UUID_COM_SPLIT_DE_TELEFONE = "732ffc02-3465-4237-a5f6-12fd4a2b3be0"


class TestScrubUuid:
    """Os ids do servico (``ordem_id``, ``request_id``, ator e alvo) sao UUID."""

    def test_uuid_com_trecho_dd_dddd_dddd_fica_intacto(self) -> None:
        texto = f"ordem {_UUID_COM_SPLIT_DE_TELEFONE}"
        assert scrub_pii(None, "info", {"event": texto})["event"] == texto

    @pytest.mark.parametrize(
        "caixa", [str.lower, str.upper], ids=["minusculas", "maiusculas"]
    )
    def test_dez_mil_uuid4_ficam_intactos(self, caixa: Callable[[str], str]) -> None:
        # Antes da correcao cerca de 1,4% dos UUID v4 saiam mascarados.
        ids = [caixa(str(uuid4())) for _ in range(10_000)]
        mascarados = [
            valor
            for valor in ids
            if scrub_pii(None, "info", {"id": valor})["id"] != valor
        ]
        assert mascarados == []

    @pytest.mark.parametrize(
        "texto",
        [
            pytest.param("tel-11 99999-0000", id="colado-a-hifen"),
            pytest.param("tel(11)99999-0000", id="colado-a-letra"),
            pytest.param("ligar+5511999990000", id="mais-55-colado-a-letra"),
            pytest.param("+5511999990000", id="mais-55-corrido"),
        ],
    )
    def test_telefone_colado_a_letra_ou_hifen_continua_mascarado(
        self, texto: str
    ) -> None:
        resultado = str(scrub_pii(None, "info", {"event": texto})["event"])
        assert "9999" not in resultado
        assert "***" in resultado

    def test_correlation_id_sai_intacto_no_log_json(
        self, log_capturado: io.StringIO
    ) -> None:
        # Pelo pipeline real: era o campo de busca da saga que sumia do log.
        structlog.get_logger("test.uuid").info(
            "evento", correlation_id=_UUID_COM_SPLIT_DE_TELEFONE
        )
        registro = json.loads(log_capturado.getvalue().splitlines()[-1])
        assert registro["correlation_id"] == _UUID_COM_SPLIT_DE_TELEFONE


class TestScrubChavesSensiveis:
    """Denylist de chaves: o VALOR e mascarado pelo nome do campo, nao por regex."""

    @pytest.mark.parametrize(
        "chave",
        [
            "password",
            "senha",
            "senha_hash",
            "token",
            "secret",
            "authorization",
            "refresh_token",
            "access_token",
            "api_key",
            # PII sem forma detectavel por regex (issue #99 do p2): mascara por nome.
            "telefone",
            "celular",
            "phone",
            "contato",
            # Placa e o texto livre do servico (pode trazer nome ou placa).
            "placa",
            "descricao_problema",
            "observacoes",
            "motivo",
        ],
    )
    def test_chave_sensivel_mascara_valor(self, chave: str) -> None:
        event_dict: dict[str, object] = {chave: "super-secreto-xyz"}
        result = scrub_pii(None, "info", event_dict)
        assert "super-secreto-xyz" not in str(result[chave])
        assert result[chave] == "***"

    def test_chave_sensivel_case_insensitive(self) -> None:
        event_dict: dict[str, object] = {"Authorization": "Bearer abc.def.ghi"}
        result = scrub_pii(None, "info", event_dict)
        assert "abc.def.ghi" not in str(result["Authorization"])

    def test_chave_sensivel_aninhada(self) -> None:
        event_dict: dict[str, object] = {
            "payload": {"user": "joao", "password": "hunter2"}
        }
        result = scrub_pii(None, "info", event_dict)
        inner: Mapping[str, object] = result["payload"]
        assert inner["user"] == "joao"
        assert inner["password"] == "***"

    def test_chave_nao_sensivel_preservada(self) -> None:
        event_dict: dict[str, object] = {"username": "joao", "count": 3}
        result = scrub_pii(None, "info", event_dict)
        assert result["username"] == "joao"
        assert result["count"] == 3


class TestPipelineMascaraTraceback:
    """O traceback (chave `exception`) deve sair mascarado pelo pipeline real.

    Cobre o bug central da issue #86 do p2 (https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p2/issues/86):
    `scrub_pii` rodava ANTES de
    `format_exc_info`, entao a chave `exception` (montada por format_exc_info)
    escapava do mascaramento. Apos o reorder, o traceback e mascarado.
    """

    def test_excecao_com_pii_no_traceback_structlog(
        self, log_capturado: io.StringIO
    ) -> None:
        log = structlog.get_logger("test.pipeline")
        try:
            raise RuntimeError(
                "cliente CPF 123.456.789-00 email joao@example.com tel (11) 99999-0000"
            )
        except RuntimeError:
            log.exception("falha no processamento")

        saida = log_capturado.getvalue()
        assert saida, "pipeline nao emitiu nada"
        # A chave exception precisa existir (format_exc_info rodou)...
        registro = json.loads(saida.strip().splitlines()[-1])
        assert "exception" in registro
        # ...e o traceback nao pode conter PII crua.
        assert "123.456.789-00" not in saida
        assert "joao@example.com" not in saida
        assert "(11) 99999-0000" not in saida

    def test_excecao_com_pii_no_traceback_stdlib(
        self, log_capturado: io.StringIO
    ) -> None:
        # Caminho do handler 500 (error_handler.py): logger STDLIB, nao structlog.
        # Deve passar pelo ProcessorFormatter (foreign_pre_chain) e ser scrubado.
        stdlogger = logging.getLogger("test.stdlib.handler500")
        try:
            raise ValueError("documento 987.654.321-00 contato +55 11 98888-7777")
        except ValueError:
            stdlogger.exception("Erro interno (request_id=abc)")

        saida = log_capturado.getvalue()
        assert saida, "pipeline stdlib nao emitiu nada"
        assert "987.654.321-00" not in saida
        assert "+55 11 98888-7777" not in saida
        # Confirma que o traceback foi de fato renderizado (nao so a mensagem).
        registro = json.loads(saida.strip().splitlines()[-1])
        assert "exception" in registro

    def test_stdlib_log_simples_e_scrubado(self, log_capturado: io.StringIO) -> None:
        # Log stdlib sem excecao (ex.: uvicorn access log) tambem e scrubado.
        logging.getLogger("uvicorn.access").warning(
            "request de joao@example.com cpf 111.222.333-44"
        )
        saida = log_capturado.getvalue()
        assert "joao@example.com" not in saida
        assert "111.222.333-44" not in saida

    def test_uvicorn_loggers_religados_ao_root(self) -> None:
        # uvicorn instala handler proprio + propagate=False; configurar_logging
        # deve limpar o handler cru e religar propagate para o scrubber do root.
        root = logging.getLogger()
        handlers_anteriores = root.handlers[:]
        nivel_anterior = root.level
        config_anterior = structlog.get_config()

        # Simula o estado que o uvicorn deixa: handler proprio + propagate desligado.
        acc = logging.getLogger("uvicorn.access")
        handlers_acc_anteriores = acc.handlers[:]
        propagate_acc_anterior = acc.propagate
        handler_cru = logging.StreamHandler(io.StringIO())
        acc.handlers = [handler_cru]
        acc.propagate = False

        buffer = io.StringIO()
        try:
            configurar_logging(stream=buffer)
            # O handler cru do uvicorn foi removido e propagate religado.
            assert handler_cru not in acc.handlers
            assert acc.handlers == []
            assert acc.propagate is True

            acc.info("GET /clientes?cpf=123.456.789-00")
            saida = buffer.getvalue()
            # Saiu pelo scrubber do root (e nao pelo handler cru do uvicorn).
            assert "123.456.789-00" not in saida
            assert isinstance(handler_cru.stream, io.StringIO)
            assert handler_cru.stream.getvalue() == ""
        finally:
            root.handlers = handlers_anteriores
            root.setLevel(nivel_anterior)
            acc.handlers = handlers_acc_anteriores
            acc.propagate = propagate_acc_anterior
            structlog.configure(**config_anterior)


class TestScrubPlaca:
    """Placa solta em texto (antiga ou Mercosul) e mascarada por regex de valor."""

    @pytest.mark.parametrize(
        "placa",
        [
            pytest.param("ABC1D23", id="mercosul"),
            pytest.param("ABC1234", id="antiga"),
            pytest.param("ABC-1234", id="antiga-com-hifen"),
            pytest.param("abc1d23", id="minusculas"),
        ],
    )
    def test_placa_em_texto_mascarada(self, placa: str) -> None:
        result = scrub_pii(None, "info", {"event": f"veiculo {placa} no patio"})
        assert placa not in str(result["event"])
        assert f"{placa[:2]}*****" in str(result["event"])

    @pytest.mark.parametrize(
        "nao_placa",
        [
            pytest.param("sku PEC-OLEO-5W30", id="sku"),
            pytest.param("servico SRV-FREIOS", id="codigo"),
            pytest.param("git a1b2c3d4e5f6", id="sha"),
            pytest.param("ordem 3c9a7e10-5b2f-4f6d-8a41-0e2d9b7c6f55", id="uuid"),
            pytest.param("fila AGUARDANDO 2026", id="texto-com-ano"),
        ],
    )
    def test_nao_placa_preservado(self, nao_placa: str) -> None:
        result = scrub_pii(None, "info", {"event": nao_placa})
        assert result["event"] == nao_placa


class TestRedigirPiiErro:
    def test_mascara_placa_na_mensagem_devolvida_ao_cliente(self) -> None:
        assert "ABC1D23" not in redigir_pii_erro("veiculo ABC1D23 nao encontrado")

    def test_mascara_pii_na_mensagem_devolvida_ao_cliente(self) -> None:
        resultado = redigir_pii_erro("falhou para joao@example.com, CPF 123.456.789-00")
        assert "joao@example.com" not in resultado
        assert "123.456.789-00" not in resultado

    def test_trunca_mensagem_longa(self) -> None:
        resultado = redigir_pii_erro("x" * 500)
        assert len(resultado) == 201
        assert resultado.endswith("…")


@pytest.fixture
def buffer_com_uvicorn_restaurado() -> Iterator[io.StringIO]:
    """Buffer para o ``configurar_logging``; no fim, desfaz o que ele e o uvicorn mexem.

    O ``uvicorn.Config`` troca handlers e ``propagate`` de tres loggers globais, e
    o ``configurar_logging`` troca o handler do root: nada disso pode vazar.
    """
    nomes = ("uvicorn", "uvicorn.error", "uvicorn.access")
    root = logging.getLogger()
    handlers_anteriores, nivel_anterior = root.handlers[:], root.level
    config_anterior = structlog.get_config()
    estado = {
        nome: (
            logging.getLogger(nome).handlers[:],
            logging.getLogger(nome).propagate,
            logging.getLogger(nome).level,
        )
        for nome in nomes
    }
    yield io.StringIO()
    root.handlers = handlers_anteriores
    root.setLevel(nivel_anterior)
    structlog.configure(**config_anterior)
    for nome, (handlers, propagate, nivel) in estado.items():
        logger = logging.getLogger(nome)
        logger.handlers = handlers
        logger.propagate = propagate
        logger.setLevel(nivel)


class TestLoggersDoUvicorn:
    """O uvicorn monta os loggers antes de importar o app (``--no-access-log``)."""

    def test_sem_access_log_o_uvicorn_continua_sem_access_log(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        # O uvicorn deixa `uvicorn.access` sem handler e sem propagar e le o
        # `hasHandlers()` a cada conexao: religar a propagacao ao root traria de
        # volta a linha de texto do acesso, em duplicidade com o `http_request`.
        uvicorn.Config("src.main:app", access_log=False)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        acesso = logging.getLogger("uvicorn.access")
        assert not acesso.hasHandlers()
        acesso.info('127.0.0.1:5000 - "GET /api/v1/saude HTTP/1.1" 200')
        assert buffer_com_uvicorn_restaurado.getvalue() == ""

    def test_o_resto_do_uvicorn_sai_em_json_com_o_access_log_desligado(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        uvicorn.Config("src.main:app", access_log=False)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        logging.getLogger("uvicorn.error").info("Started server process [1]")
        registro = json.loads(buffer_com_uvicorn_restaurado.getvalue())
        assert (registro["logger"], registro["event"]) == (
            "uvicorn.error",
            "Started server process [1]",
        )

    def test_com_access_log_ligado_o_acesso_sai_em_json_e_scrubado(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        uvicorn.Config("src.main:app", access_log=True)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        logging.getLogger("uvicorn.access").info("GET /x?email=joao@example.com")
        saida = buffer_com_uvicorn_restaurado.getvalue()
        assert json.loads(saida)["logger"] == "uvicorn.access"
        assert "joao@example.com" not in saida

    def test_configurar_duas_vezes_mantem_o_access_log_desligado(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        # A fabrica do app e o lifespan podem chamar `configurar_logging` em
        # sequencia: a segunda chamada ve o estado deixado pela primeira.
        uvicorn.Config("src.main:app", access_log=False)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        assert not logging.getLogger("uvicorn.access").hasHandlers()


def test_criar_app_configura_o_log_json_antes_do_lifespan(
    capsys: pytest.CaptureFixture[str], log_capturado: io.StringIO
) -> None:
    # O uvicorn importa o modulo e monta o app antes de logar "Started server
    # process": configurado so no lifespan, o boot saia em texto no meio do JSON.
    # `log_capturado` so devolve o root e o structlog ao que eram no fim do teste.
    from src.main import criar_app

    logging.getLogger().handlers = []
    criar_app()  # sem entrar no lifespan
    capsys.readouterr()

    logging.getLogger("uvicorn.error").info("Started server process [1]")
    registro = json.loads(capsys.readouterr().out)
    assert registro["event"] == "Started server process [1]"
