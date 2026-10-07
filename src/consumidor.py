"""Processo consumidor dos comandos da saga: ``python -m src.consumidor``.

Mesma imagem da API, outro comando. Liga cada tipo de comando da fila
``execucao.comandos`` ao handler do contexto dono (RFC-004, secao 5.3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

from src.compartilhado.infraestrutura.ambiente import variavel_obrigatoria
from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.consumidor import Consumidor
from src.compartilhado.infraestrutura.mensageria.processo import (
    SinaisDoProcesso,
    parada_por_sinal,
    servir_metricas,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    configurar_telemetria,
)
from src.diagnostico.interfaces.comandos import (
    descartar_diagnostico,
    solicitar_diagnostico,
)
from src.estoque.interfaces.comandos import liberar_reserva, reservar_pecas
from src.execucao.interfaces.comandos import (
    agendar_execucao,
    anonimizar_veiculo,
    cancelar_execucao,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.compartilhado.infraestrutura.mensageria.consumidor import (
        HandlerDeComando,
    )

_log = structlog.get_logger(__name__)

HANDLERS: Final[Mapping[str, HandlerDeComando]] = {
    "SolicitarDiagnostico": solicitar_diagnostico,
    "DescartarDiagnostico": descartar_diagnostico,
    "ReservarPecas": reservar_pecas,
    "LiberarReserva": liberar_reserva,
    "AgendarExecucao": agendar_execucao,
    "CancelarExecucao": cancelar_execucao,
    "AnonimizarVeiculo": anonimizar_veiculo,
}


def main() -> None:
    configurar_logging()
    configurar_telemetria()
    engine = criar_engine(variavel_obrigatoria("DATABASE_URL"))
    consumidor = Consumidor(
        criar_session_factory(engine),
        variavel_obrigatoria("RABBITMQ_URL"),
        HANDLERS,
        SinaisDoProcesso.do_processo("consumidor"),
    )
    servir_metricas()
    _log.info("consumer_started")
    try:
        consumidor.executar(parada_por_sinal())
    finally:
        engine.dispose()
    _log.info("consumer_stopped")


if __name__ == "__main__":
    main()
