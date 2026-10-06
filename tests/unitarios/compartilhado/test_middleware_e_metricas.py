from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from src.compartilhado.infraestrutura.metrics import configurar_metricas
from src.compartilhado.interfaces.middleware import SecurityHeadersMiddleware


def _app() -> TestClient:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    configurar_metricas(app)

    @app.get("/itens/{item_id}")
    def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError

    return TestClient(app, raise_server_exceptions=False)


def _contagem(rota: str, status: str, metodo: str = "GET") -> float:
    valor = REGISTRY.get_sample_value(
        "http_request_duration_seconds_count",
        {"method": metodo, "rota": rota, "status": status},
    )
    return valor or 0.0


class TestSecurityHeaders:
    def test_headers_de_seguranca_em_toda_resposta(self) -> None:
        resposta = _app().get("/itens/1")
        assert resposta.headers["X-Content-Type-Options"] == "nosniff"
        assert resposta.headers["X-Frame-Options"] == "DENY"
        assert resposta.headers["Cache-Control"] == "no-store"
        assert resposta.headers["Content-Security-Policy"] == "default-src 'none'"
        assert "max-age=31536000" in resposta.headers["Strict-Transport-Security"]

    def test_erro_nao_tratado_sai_com_headers_e_request_id(self) -> None:
        # O handler de Exception roda por fora do middleware: sem a conversao
        # no proprio middleware, o 500 saia sem estes headers.
        resposta = _app().get("/explode", headers={"X-Request-ID": "req-500"})
        assert resposta.status_code == 500
        assert resposta.json()["erro"] == {
            "codigo": "ERRO_INTERNO",
            "mensagem": "Erro interno do servidor",
            "id_requisicao": "req-500",
        }
        assert resposta.headers["X-Request-ID"] == "req-500"
        assert resposta.headers["X-Content-Type-Options"] == "nosniff"
        assert resposta.headers["Content-Security-Policy"] == "default-src 'none'"

    def test_access_log_estruturado_com_request_id(
        self, log_capturado: io.StringIO
    ) -> None:
        _app().get("/itens/7", headers={"X-Request-ID": "req-acesso"})
        registros = [
            json.loads(linha) for linha in log_capturado.getvalue().splitlines()
        ]
        [acesso] = [r for r in registros if r["event"] == "http_request"]
        assert (acesso["method"], acesso["path"], acesso["status"]) == (
            "GET",
            "/itens/7",
            200,
        )
        assert acesso["request_id"] == "req-acesso"
        assert acesso["duracao_ms"] >= 0

    def test_swagger_fica_sem_csp(self) -> None:
        assert "Content-Security-Policy" not in _app().get("/docs").headers

    def test_request_id_valido_da_borda_e_propagado(self) -> None:
        resposta = _app().get("/itens/1", headers={"X-Request-ID": "kong-abc_1.2="})
        assert resposta.headers["X-Request-ID"] == "kong-abc_1.2="

    @pytest.mark.parametrize(
        "recebido",
        ["", "a b", "x" * 129, "a;b", "<script>"],
        ids=["vazio", "espaco", "129-caracteres", "ponto-e-virgula", "html"],
    )
    def test_request_id_invalido_e_trocado_por_uuid(self, recebido: str) -> None:
        resposta = _app().get("/itens/1", headers={"X-Request-ID": recebido})
        gerado = resposta.headers["X-Request-ID"]
        assert gerado != recebido
        assert len(gerado) == 36


class TestMetricas:
    def test_latencia_por_template_de_rota(self) -> None:
        antes = _contagem("/itens/{item_id}", "200")
        cliente = _app()
        cliente.get("/itens/1")
        cliente.get("/itens/2")
        assert _contagem("/itens/{item_id}", "200") == antes + 2

    def test_rota_inexistente_agrega_em_nao_roteada(self) -> None:
        antes = _contagem("nao_roteada", "404")
        _app().get("/qualquer/coisa")
        assert _contagem("nao_roteada", "404") == antes + 1

    def test_excecao_conta_como_500(self) -> None:
        antes = _contagem("/explode", "500")
        assert _app().get("/explode").status_code == 500
        assert _contagem("/explode", "500") == antes + 1

    def test_metrics_responde_direto_no_formato_prometheus(self) -> None:
        cliente = _app()
        cliente.get("/itens/1")
        antes = _contagem("/metrics", "200")
        resposta = cliente.get("/metrics", follow_redirects=False)
        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("text/plain")
        assert "http_request_duration_seconds_bucket" in resposta.text
        # O scrape conta na propria rota, nao em `nao_roteada` via redirect.
        assert _contagem("/metrics", "200") == antes + 1
