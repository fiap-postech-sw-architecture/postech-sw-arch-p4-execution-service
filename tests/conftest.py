from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import jwt
import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from src.compartilhado.infraestrutura.logging import configurar_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Colima (macOS): o socket do Docker nao fica em /var/run/docker.sock. Ajusta o
# ambiente antes de o testcontainers ler DOCKER_HOST, para `uv run pytest` puro
# funcionar; no CI (ubuntu) e no Docker Desktop nada muda.
_SOCKET_COLIMA = Path.home() / ".colima" / "default" / "docker.sock"
if "DOCKER_HOST" not in os.environ and _SOCKET_COLIMA.exists():
    os.environ["DOCKER_HOST"] = f"unix://{_SOCKET_COLIMA}"
    os.environ.setdefault(
        "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock"
    )

# Provider global do SDK com exportador em memoria, instalado uma vez (o OTel
# so aceita um por processo): os testes leem os spans de relay e consumidor.
_SPANS = InMemorySpanExporter()
_PROVEDOR = TracerProvider()
_PROVEDOR.add_span_processor(SimpleSpanProcessor(_SPANS))
trace.set_tracer_provider(_PROVEDOR)

KID = "chave-de-teste"
EMISSOR = "pytstop-os-service"
AUDIENCIA = "pytstop"


class ServidorJwks(ThreadingHTTPServer):
    """JWKS do "OS Service"; ``status``, ``atraso`` e ``corpo`` simulam falhas."""

    jwks: dict[str, Any]
    requisicoes: int
    status: int = 200
    atraso: float = 0.0
    corpo: bytes | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/.well-known/jwks.json"


class _JwksHandler(BaseHTTPRequestHandler):
    server: ServidorJwks

    def do_GET(self) -> None:
        self.server.requisicoes += 1
        time.sleep(self.server.atraso)
        corpo = self.server.corpo or json.dumps(self.server.jwks).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *_args: object) -> None:
        pass


def _subir_servidor_jwks(jwk_publico: dict[str, Any]) -> ServidorJwks:
    servidor = ServidorJwks(("127.0.0.1", 0), _JwksHandler)
    servidor.jwks = {"keys": [jwk_publico]}
    servidor.requisicoes = 0
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


def _derrubar(servidor: ServidorJwks) -> None:
    servidor.shutdown()
    servidor.server_close()


@pytest.fixture(scope="session")
def chave_privada() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwk_publico(chave_privada: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(chave_privada.public_key(), as_dict=True)
    return {**jwk, "kid": KID, "use": "sig", "alg": "RS256"}


@pytest.fixture(scope="session")
def servidor_jwks(jwk_publico: dict[str, Any]) -> Iterator[ServidorJwks]:
    """JWKS do "OS Service" servido por HTTP real (o PyJWKClient usa urllib)."""
    servidor = _subir_servidor_jwks(jwk_publico)
    yield servidor
    _derrubar(servidor)


@pytest.fixture
def servidor_jwks_proprio(jwk_publico: dict[str, Any]) -> Iterator[ServidorJwks]:
    """JWKS so deste teste, para simular falha sem afetar os outros."""
    servidor = _subir_servidor_jwks(jwk_publico)
    yield servidor
    _derrubar(servidor)


@pytest.fixture(scope="session")
def jwks_url(servidor_jwks: ServidorJwks) -> str:
    return servidor_jwks.url


def assinar(
    chave: rsa.RSAPrivateKey, claims: dict[str, Any], kid: str | None = KID
) -> str:
    cabecalho = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, chave, algorithm="RS256", headers=cabecalho)


def claims_padrao(papel: str, sub: UUID | str) -> dict[str, Any]:
    agora = datetime.now(UTC)
    return {
        "sub": str(sub),
        "papel": papel,
        "iss": EMISSOR,
        "aud": AUDIENCIA,
        "iat": agora,
        "exp": agora + timedelta(minutes=15),
        "jti": str(uuid4()),
        "type": "access",
    }


@pytest.fixture(scope="session")
def emitir_token(chave_privada: rsa.RSAPrivateKey) -> Callable[..., str]:
    """Token RS256 como o OS Service emitiria; ``extras`` sobrescreve claims."""

    def _emitir(papel: str, sub: UUID | str | None = None, **extras: Any) -> str:
        claims = claims_padrao(papel, sub if sub is not None else uuid4())
        claims.update(extras)
        return assinar(chave_privada, claims)

    return _emitir


@pytest.fixture
def log_capturado() -> Iterator[io.StringIO]:
    """Pipeline de log real (scrub incluso) escrevendo num buffer.

    Restaura no teardown o root logger e o structlog de antes: configurar_logging
    troca o handler do root e nao pode vazar entre testes.
    """
    root = logging.getLogger()
    handlers_anteriores, nivel_anterior = root.handlers[:], root.level
    config_anterior = structlog.get_config()
    buffer = io.StringIO()
    configurar_logging(stream=buffer)
    try:
        yield buffer
    finally:
        root.handlers = handlers_anteriores
        root.setLevel(nivel_anterior)
        structlog.configure(**config_anterior)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """Spans terminados durante o teste."""
    _SPANS.clear()
    return _SPANS
