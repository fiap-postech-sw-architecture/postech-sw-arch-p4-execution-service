"""RabbitMQ 4.3.6 de teste com a topologia e os usuarios copiados do platform.

Sobe a imagem com o ``definitions.json``, o ``rabbitmq.conf`` e o admin do
compose da plataforma e roda o ``criar-usuarios.sh`` copiado, que cria os
usuarios ``os``, ``billing`` e ``execucao`` com as permissoes de
``permissoes.json``. Os testes publicam comandos como ``os`` (o orquestrador) e
leem a fila ``os.eventos`` como admin.
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import uuid4

import pika
from pika.exceptions import AMQPError
from testcontainers.core.container import DockerContainer

from src.compartilhado.infraestrutura.mensageria.amqp import propriedades
from src.compartilhado.infraestrutura.mensageria.consumidor import NIVEIS_DE_RETRY

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

IMAGEM = "rabbitmq:4.3.6-management"
_RABBITMQ = Path(__file__).resolve().parents[2] / "contratos" / "rabbitmq"
_ADMIN = json.loads((_RABBITMQ / "rabbitmq-admin.json").read_text())["users"][0]
# Senhas de demonstracao, as mesmas do compose da plataforma e deste servico.
SENHAS = {
    "admin": _ADMIN["password"],
    "os": "pytstop-os-demo-2026",  # gitleaks:allow
    "billing": "pytstop-billing-demo-2026",  # gitleaks:allow
    "execucao": "pytstop-execucao-demo-2026",  # gitleaks:allow
}
# Nos testes o atraso de cada fila de retry cai para 100 ms (o TTL e argumento
# da fila): a copia volta logo, pelo mesmo caminho do broker de verdade.
TTL_DE_TESTE_MS = 100
FILAS_DO_SERVICO = (
    "execucao.comandos",
    *NIVEIS_DE_RETRY,
    "execucao.comandos.dlq",
    "os.eventos",
)
_MONTAGENS = {
    "rabbitmq.conf": "/etc/rabbitmq/conf.d/20-pytstop.conf",
    "enabled_plugins": "/etc/rabbitmq/enabled_plugins",
    "definitions.json": "/etc/rabbitmq/definitions/definitions.json",
    "rabbitmq-admin.json": "/etc/rabbitmq/definitions/admin.json",
    "criar-usuarios.sh": "/scripts/criar-usuarios.sh",
    "permissoes.json": "/scripts/permissoes.json",
}


def esperar_ate(
    condicao: Callable[[], Any], prazo_s: float = 20.0, intervalo_s: float = 0.05
) -> Any:
    """Repete ``condicao`` ate ela devolver algo verdadeiro (ou estoura o prazo)."""
    fim = time.monotonic() + prazo_s
    while True:
        resultado = condicao()
        if resultado:
            return resultado
        if time.monotonic() > fim:
            msg = f"condicao nao satisfeita em {prazo_s} s"
            raise AssertionError(msg)
        time.sleep(intervalo_s)


class Broker:
    """O container e atalhos para publicar, ler e operar o broker nos testes."""

    def __init__(self, container: DockerContainer) -> None:
        self.container = container
        self.host = container.get_container_host_ip()
        self.porta = int(container.get_exposed_port(5672))

    def url(self, usuario: str) -> str:
        return f"amqp://{usuario}:{SENHAS[usuario]}@{self.host}:{self.porta}/%2F"

    @contextmanager
    def canal(self, usuario: str = "admin") -> Iterator[Any]:
        conexao = pika.BlockingConnection(pika.URLParameters(self.url(usuario)))
        try:
            canal = conexao.channel()
            canal.confirm_delivery()
            yield canal
        finally:
            if conexao.is_open:
                conexao.close()

    def publicar_comando(
        self,
        envelope: Mapping[str, Any],
        *,
        usuario: str = "os",
        headers: Mapping[str, Any] | None = None,
    ) -> None:
        """Publica como o orquestrador publicaria (``user_id`` = usuario)."""
        tipo = re.sub(r"(?<!^)(?=[A-Z])", "_", envelope["tipo"]).lower()
        with self.canal(usuario) as canal:
            canal.basic_publish(
                exchange="pytstop.comandos",
                routing_key=f"comando.execucao.{tipo}",
                body=json.dumps(envelope).encode(),
                properties=propriedades(
                    envelope, usuario=usuario, headers=headers or {}
                ),
                mandatory=True,
            )

    def pegar(self, fila: str) -> tuple[Any, bytes] | None:
        """Tira uma mensagem da fila (como admin): propriedades e corpo."""
        with self.canal() as canal:
            metodo, props, corpo = canal.basic_get(fila, auto_ack=True)
        return None if metodo is None else (props, corpo)

    def contar(self, fila: str) -> int:
        with self.canal() as canal:
            total: int = canal.queue_declare(fila, passive=True).method.message_count
        return total

    def esvaziar(self) -> None:
        with self.canal() as canal:
            for fila in FILAS_DO_SERVICO:
                canal.queue_purge(fila)

    def redeclarar_retry(self, fila: str, ttl_ms: int) -> None:
        """Recria a fila de retry com outro TTL (argumento imutavel da fila)."""
        with self.canal() as canal:
            canal.queue_delete(fila)
            canal.queue_declare(
                fila,
                durable=True,
                arguments={"x-queue-type": "quorum", "x-message-ttl": ttl_ms},
            )
            canal.queue_bind(fila, "pytstop.retry", routing_key=fila)

    def rabbitmqctl(self, *argumentos: str) -> str:
        codigo, saida = self.container.get_wrapped_container().exec_run(
            ["rabbitmqctl", *argumentos], user="999:999"
        )
        texto: str = saida.decode()
        assert codigo == 0, texto
        return texto


def _esperar_topologia(broker: Broker, prazo_s: float = 90.0) -> None:
    # pytstop.retry so existe se o definitions.json foi importado no boot.
    def pronto() -> bool:
        try:
            with broker.canal() as canal:
                canal.exchange_declare("pytstop.retry", passive=True)
        except (AMQPError, OSError):
            return False
        return True

    esperar_ate(pronto, prazo_s=prazo_s, intervalo_s=0.5)


def subir_broker() -> tuple[DockerContainer, Broker]:
    container = DockerContainer(IMAGEM).with_exposed_ports(5672)
    # Mesmo usuario do compose da plataforma (o .erlang.cookie e do 999).
    container.with_kwargs(user="999:999", hostname="rabbitmq")
    for arquivo, destino in _MONTAGENS.items():
        container.with_volume_mapping(str(_RABBITMQ / arquivo), destino, "ro")
    container.start()
    broker = Broker(container)
    _esperar_topologia(broker)
    ambiente = {
        "RABBITMQADMIN_TARGET_HOST": "localhost",
        "RABBITMQADMIN_TARGET_PORT": "15672",
        "RABBITMQADMIN_NON_INTERACTIVE_MODE": "true",
        "RABBITMQADMIN_USERNAME": "admin",
        "RABBITMQADMIN_PASSWORD": SENHAS["admin"],
        "RABBITMQ_OS_PASSWORD": SENHAS["os"],
        "RABBITMQ_BILLING_PASSWORD": SENHAS["billing"],
        "RABBITMQ_EXECUCAO_PASSWORD": SENHAS["execucao"],
    }
    codigo, saida = container.get_wrapped_container().exec_run(
        ["sh", "/scripts/criar-usuarios.sh"], environment=ambiente, user="999:999"
    )
    assert codigo == 0, saida.decode()
    for fila in NIVEIS_DE_RETRY:
        broker.redeclarar_retry(fila, TTL_DE_TESTE_MS)
    return container, broker


def envelope_de_comando(
    tipo: str,
    dados: Mapping[str, Any],
    *,
    mensagem_id: Any = None,
    versao: int = 1,
) -> dict[str, Any]:
    """Comando no envelope do contrato, como o OS Service publica."""
    correlacao = dados.get("ordem_id") or dados.get("veiculo_id")
    return {
        "id": str(mensagem_id or uuid4()),
        "tipo": tipo,
        "versao": versao,
        "origem": "os-service",
        "correlation_id": str(correlacao),
        "causation_id": None,
        "ocorrido_em": datetime.now(UTC).isoformat(),
        "dados": dict(dados),
    }


class EmSegundoPlano:
    """Roda ``executar(parar)`` (relay ou consumidor) numa thread durante o bloco."""

    def __init__(self, processo: Any) -> None:
        self._processo = processo
        self.parar = threading.Event()
        self._erro: BaseException | None = None
        self._thread = threading.Thread(target=self._rodar, daemon=True)

    def _rodar(self) -> None:
        try:
            self._processo.executar(self.parar)
        except BaseException as exc:
            self._erro = exc

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.parar.set()
        self._thread.join(timeout=15)
        assert not self._thread.is_alive(), "o processo nao parou com o sinal"
        if self._erro is not None:
            raise self._erro
