"""Toda serie de rotulo fechado vale zero logo depois do boot de cada processo.

Sem a serie o ``increase()`` dos paineis perde o primeiro evento dela. O registro
do ``prometheus_client`` e global e os outros testes ja contaram nele, entao cada
caso sobe o processo num interpretador novo e le o ``/metrics`` de antes do
primeiro evento.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.compartilhado.infraestrutura.mensageria.consumidor import Resultado

RAIZ = Path(__file__).resolve().parents[2]
_BANCO_FALSO = "postgresql://x:y@127.0.0.1:1/nada"  # gitleaks:allow - nunca conecta
_BROKER_FALSO = "amqp://execucao:x@127.0.0.1:1/%2F"  # gitleaks:allow - nunca conecta

# Os comandos que o consumidor atende e os eventos que o relay publica, escritos
# aqui de proposito: mudar o contrato obriga a rever os paineis.
COMANDOS = {
    "SolicitarDiagnostico",
    "DescartarDiagnostico",
    "ReservarPecas",
    "LiberarReserva",
    "AgendarExecucao",
    "CancelarExecucao",
    "AnonimizarVeiculo",
}
EVENTOS = {
    "DiagnosticoIniciado",
    "DiagnosticoConcluido",
    "DiagnosticoDescartado",
    "PecasReservadas",
    "ReservaDePecasFalhou",
    "ReservaLiberada",
    "ExecucaoAgendada",
    "ExecucaoCancelada",
    "ExecucaoIniciada",
    "ExecucaoFinalizada",
}

_BOOT_DO_RELAY = f"""
from prometheus_client import generate_latest
from src.compartilhado.infraestrutura.database import criar_engine
from src.compartilhado.infraestrutura.mensageria.processo import SinaisDoProcesso
from src.compartilhado.infraestrutura.mensageria.relay import Relay
sinais = SinaisDoProcesso.do_processo("x")
Relay(criar_engine({_BANCO_FALSO!r}), {_BROKER_FALSO!r}, sinais)
print(generate_latest().decode())
"""
_BOOT_DO_CONSUMIDOR = f"""
from prometheus_client import generate_latest
from src.compartilhado.infraestrutura.database import criar_engine
from src.compartilhado.infraestrutura.mensageria.consumidor import Consumidor
from src.compartilhado.infraestrutura.mensageria.processo import SinaisDoProcesso
from src.consumidor import HANDLERS
sinais = SinaisDoProcesso.do_processo("x")
Consumidor(criar_engine({_BANCO_FALSO!r}), {_BROKER_FALSO!r}, HANDLERS, sinais)
print(generate_latest().decode())
"""


def _metricas_do_boot(codigo: str) -> dict[str, float]:
    """O ``/metrics`` do processo recem-subido: serie (nome e rotulos) e valor."""
    saida = subprocess.run(  # noqa: S603 - comando fixo do teste
        [sys.executable, "-c", codigo],
        check=True,
        capture_output=True,
        text=True,
        cwd=RAIZ,
    ).stdout
    series = (
        linha.rsplit(" ", 1)
        for linha in saida.splitlines()
        if linha and not linha.startswith("#")
    )
    return {serie: float(valor) for serie, valor in series}


def test_consumidor_nasce_com_zero_em_cada_tipo_e_resultado() -> None:
    metricas = _metricas_do_boot(_BOOT_DO_CONSUMIDOR)

    esperadas = {
        f'pytstop_mensagens_consumidas_total{{resultado="{r.value}",tipo="{tipo}"}}'
        for tipo in (*COMANDOS, "desconhecido")
        for r in Resultado
    }
    series = {
        s for s in metricas if s.startswith("pytstop_mensagens_consumidas_total{")
    }
    assert series == esperadas
    assert {metricas[s] for s in esperadas} == {0.0}


def test_relay_nasce_com_zero_em_cada_tipo_publicado() -> None:
    metricas = _metricas_do_boot(_BOOT_DO_RELAY)

    esperadas = {f'pytstop_mensagens_publicadas_total{{tipo="{t}"}}' for t in EVENTOS}
    series = {
        s for s in metricas if s.startswith("pytstop_mensagens_publicadas_total{")
    }
    assert series == esperadas
    assert {metricas[s] for s in esperadas} == {0.0}


@pytest.mark.parametrize(
    ("codigo", "componente"),
    [
        pytest.param(_BOOT_DO_RELAY, "relay", id="relay"),
        pytest.param(_BOOT_DO_CONSUMIDOR, "consumidor", id="consumidor"),
    ],
)
def test_cada_processo_nasce_com_zero_nas_reconexoes_do_proprio_componente(
    codigo: str, componente: str
) -> None:
    metricas = _metricas_do_boot(codigo)

    reconexoes = {
        s: v
        for s, v in metricas.items()
        if s.startswith("pytstop_reconexoes_ao_broker_total")
    }
    assert reconexoes == {
        f'pytstop_reconexoes_ao_broker_total{{componente="{componente}"}}': 0.0
    }
