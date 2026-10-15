"""Smoke das metricas na imagem (``make smoke``): o ``/metrics`` de um processo.

Roda dentro do container do processo (``python - <processo> <url>``, o script vem
da entrada padrao), logo depois do boot e sem trafego. Confere duas coisas:

- as familias do catalogo (ADR-043) que sao do processo estao todas no
  ``/metrics`` (``FAMILIAS``, pela linha ``# TYPE``): a imagem que perde uma
  familia nao passa. As gauges por consulta da API nao tem amostra enquanto nao
  ha execucao finalizada nem reserva recusada, entao a linha ``# TYPE`` e o que
  as prova;
- as series de rotulo fechado nascem com zero (``SERIES``): os dez tipos que o
  relay publica, os sete comandos vezes os cinco resultados do consumidor (e o
  ``desconhecido``), as reconexoes de cada componente e as gauges da outbox.

``tests/unitarios/test_smoke_metricas.py`` confere os dez tipos, os sete comandos
e os cinco resultados contra o contrato e o codigo, entao o script nao envelhece
sem o CI avisar.
"""

from __future__ import annotations

import sys
import urllib.request

TIPOS_PUBLICADOS = (
    "DiagnosticoIniciado",
    "DiagnosticoConcluido",
    "DiagnosticoDescartado",
    "ReservaDePecasFalhou",
    "PecasReservadas",
    "ReservaLiberada",
    "ExecucaoAgendada",
    "ExecucaoIniciada",
    "ExecucaoFinalizada",
    "ExecucaoCancelada",
)
COMANDOS = (
    "SolicitarDiagnostico",
    "DescartarDiagnostico",
    "ReservarPecas",
    "LiberarReserva",
    "AgendarExecucao",
    "CancelarExecucao",
    "AnonimizarVeiculo",
)
RESULTADOS = ("processada", "duplicada", "ignorada", "retry", "dlq")
DESCONHECIDO = "desconhecido"
CONSUMIDAS = "pytstop_mensagens_consumidas_total"
PUBLICADAS = "pytstop_mensagens_publicadas_total"
RECONEXOES = "pytstop_reconexoes_ao_broker_total"

# (familia, tipo) de cada processo: a linha `# TYPE <familia> <tipo>`.
FAMILIAS = {
    "api": (
        ("http_request_duration_seconds", "histogram"),
        ("pytstop_circuit_breaker_aberto", "gauge"),
        ("pytstop_jwks_falhas_total", "counter"),
        ("pytstop_outbox_mais_antiga_segundos", "gauge"),
        ("pytstop_execucao_duracao_media_segundos", "gauge"),
        ("pytstop_pecas_em_falta", "gauge"),
    ),
    "relay": (
        ("pytstop_mensagens_publicadas_total", "counter"),
        ("pytstop_reconexoes_ao_broker_total", "counter"),
        ("outbox_pendentes", "gauge"),
        ("outbox_dead", "gauge"),
    ),
    "consumidor": (
        ("pytstop_mensagens_consumidas_total", "counter"),
        ("pytstop_reconexoes_ao_broker_total", "counter"),
    ),
}


def serie(nome: str, valor: str, **rotulos: str) -> str:
    """Linha da raspagem (rotulos em ordem alfabetica, como o cliente do
    Prometheus os escreve), sem o fim: o valor e o que sai logo depois do boot."""
    if rotulos:
        pares = ",".join(f'{k}="{v}"' for k, v in sorted(rotulos.items()))
        nome = f"{nome}{{{pares}}}"
    return f"{nome} {valor}"


SERIES = {
    "api": (
        serie("pytstop_outbox_mais_antiga_segundos", "0.0"),
        serie("pytstop_circuit_breaker_aberto", "0.0", dependencia="jwks"),
        serie("pytstop_circuit_breaker_aberto", "0.0", dependencia="billing"),
        serie("pytstop_jwks_falhas_total", "0.0"),
    ),
    "relay": (
        *(serie(PUBLICADAS, "0.0", tipo=tipo) for tipo in TIPOS_PUBLICADOS),
        serie(RECONEXOES, "0.0", componente="relay"),
        serie("outbox_pendentes", "0.0"),
        serie("outbox_dead", "0.0"),
    ),
    "consumidor": (
        *(
            serie(CONSUMIDAS, "0.0", tipo=tipo, resultado=resultado)
            for tipo in (*COMANDOS, DESCONHECIDO)
            for resultado in RESULTADOS
        ),
        serie(RECONEXOES, "0.0", componente="consumidor"),
    ),
}


def faltas(processo: str, linhas: list[str]) -> tuple[list[str], list[str]]:
    """O que o ``/metrics`` do processo nao traz: as familias e as series."""
    sem_familia = [
        f"{familia} {tipo}"
        for familia, tipo in FAMILIAS[processo]
        if f"# TYPE {familia} {tipo}" not in linhas
    ]
    presentes = set(linhas)
    sem_serie = [esperada for esperada in SERIES[processo] if esperada not in presentes]
    return sem_familia, sem_serie


if __name__ == "__main__":
    processo, url = sys.argv[1], sys.argv[2]
    with urllib.request.urlopen(url, timeout=5) as resposta:  # noqa: S310 - URL do compose
        linhas = resposta.read().decode().splitlines()

    sem_familia, sem_serie = faltas(processo, linhas)
    if sem_familia or sem_serie:
        print(
            f"smoke: {processo} sem as familias {sem_familia} e as series {sem_serie}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        f"metricas do {processo} ok ({len(FAMILIAS[processo])} familias, "
        f"{len(SERIES[processo])} series)"
    )
