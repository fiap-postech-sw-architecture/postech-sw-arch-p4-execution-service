from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

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

KID = "chave-de-teste"
EMISSOR = "pytstop-os-service"
AUDIENCIA = "pytstop"


class _ServidorJwks(ThreadingHTTPServer):
    jwks: dict[str, Any]
    requisicoes: int


class _JwksHandler(BaseHTTPRequestHandler):
    server: _ServidorJwks

    def do_GET(self) -> None:
        self.server.requisicoes += 1
        corpo = json.dumps(self.server.jwks).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture(scope="session")
def chave_privada() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwk_publico(chave_privada: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(chave_privada.public_key(), as_dict=True)
    return {**jwk, "kid": KID, "use": "sig", "alg": "RS256"}


@pytest.fixture(scope="session")
def servidor_jwks(jwk_publico: dict[str, Any]) -> Iterator[_ServidorJwks]:
    """JWKS do "OS Service" servido por HTTP real (o PyJWKClient usa urllib)."""
    servidor = _ServidorJwks(("127.0.0.1", 0), _JwksHandler)
    servidor.jwks = {"keys": [jwk_publico]}
    servidor.requisicoes = 0
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    yield servidor
    servidor.shutdown()
    servidor.server_close()


@pytest.fixture(scope="session")
def jwks_url(servidor_jwks: _ServidorJwks) -> str:
    return f"http://127.0.0.1:{servidor_jwks.server_port}/.well-known/jwks.json"


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
