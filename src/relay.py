"""Processo relay da outbox para o RabbitMQ: ``python -m src.relay``.

Mesma imagem da API, outro comando. Nao roda migracao (no Kubernetes o Job de
migracao roda antes do rollout; no compose, a API migra no boot).
"""

from __future__ import annotations

import structlog

from src.compartilhado.infraestrutura.ambiente import variavel_obrigatoria
from src.compartilhado.infraestrutura.database import criar_engine
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.processo import (
    SinaisDoProcesso,
    parada_por_sinal,
    servir_metricas,
)
from src.compartilhado.infraestrutura.mensageria.relay import Relay
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    configurar_telemetria,
)

_log = structlog.get_logger(__name__)


def main() -> None:
    configurar_logging()
    configurar_telemetria("relay")
    engine = criar_engine(variavel_obrigatoria("DATABASE_URL"))
    relay = Relay(
        engine,
        variavel_obrigatoria("RABBITMQ_URL"),
        SinaisDoProcesso.do_processo("relay"),
    )
    servir_metricas()
    _log.info("relay_started")
    try:
        relay.executar(parada_por_sinal())
    finally:
        engine.dispose()
    _log.info("relay_stopped")


if __name__ == "__main__":
    main()
