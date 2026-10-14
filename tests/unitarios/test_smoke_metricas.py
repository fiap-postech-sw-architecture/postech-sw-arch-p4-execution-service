"""O smoke das metricas (``scripts/smoke_metricas.py``) confere o conjunto completo.

O script roda dentro da imagem, no ``make smoke``, e nao importa o codigo do
servico: os nomes dele sao escritos a mao. Estes testes o amarram ao contrato e
ao codigo, entao ele nao envelhece sem o CI avisar, e provam que o que ele pede
e o que um processo recem-subido de fato mostra.
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src.compartilhado.infraestrutura.mensageria.consumidor import Resultado
from src.compartilhado.infraestrutura.mensageria.contratos import (
    tipos_publicados_por,
)
from src.consumidor import HANDLERS
from tests.unitarios.test_preinicializacao_das_metricas import (
    _BOOT_DO_CONSUMIDOR,
    _BOOT_DO_RELAY,
    _metricas_do_boot,
)

if TYPE_CHECKING:
    from types import ModuleType

RAIZ = Path(__file__).resolve().parents[2]


def _carregar() -> ModuleType:
    spec = spec_from_file_location("smoke_metricas", RAIZ / "scripts/smoke_metricas.py")
    assert spec is not None
    assert spec.loader is not None
    modulo = module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


smoke = _carregar()


def test_os_dez_tipos_publicados_sao_os_do_contrato() -> None:
    assert len(smoke.TIPOS_PUBLICADOS) == 10
    assert set(smoke.TIPOS_PUBLICADOS) == set(tipos_publicados_por("execucao"))


def test_os_sete_comandos_sao_os_que_o_consumidor_atende() -> None:
    assert len(smoke.COMANDOS) == 7
    assert set(smoke.COMANDOS) == set(HANDLERS)


def test_os_cinco_resultados_sao_os_do_consumidor() -> None:
    assert tuple(r.value for r in Resultado) == smoke.RESULTADOS


def test_cada_processo_tem_o_conjunto_completo() -> None:
    assert len(smoke.SERIES["relay"]) == 10 + 1 + 2
    assert len(smoke.SERIES["consumidor"]) == (7 + 1) * 5 + 1
    assert set(smoke.FAMILIAS) == set(smoke.SERIES) == {"api", "relay", "consumidor"}


def _linhas_do_boot(codigo: str) -> list[str]:
    return [f"{serie} {valor}" for serie, valor in _metricas_do_boot(codigo).items()]


def test_o_consumidor_recem_subido_tem_tudo_o_que_o_smoke_pede() -> None:
    linhas = _linhas_do_boot(_BOOT_DO_CONSUMIDOR)
    # O `# TYPE` nao entra em `_metricas_do_boot`: as series bastam aqui.
    _sem_familia, sem_serie = smoke.faltas("consumidor", linhas)

    assert sem_serie == []


def test_o_relay_recem_subido_tem_as_series_do_smoke_menos_as_gauges_do_banco() -> None:
    linhas = _linhas_do_boot(_BOOT_DO_RELAY)

    _sem_familia, sem_serie = smoke.faltas("relay", linhas)

    # outbox_pendentes e outbox_dead sao lidas no banco a cada raspagem: sem banco
    # (o do boot falso) saem como NaN, e o `make smoke` as confere com banco.
    assert sem_serie == ["outbox_pendentes 0.0", "outbox_dead 0.0"]


@pytest.mark.parametrize(
    ("processo", "serie_a_menos"),
    [
        pytest.param(
            "relay",
            'pytstop_mensagens_publicadas_total{tipo="ExecucaoIniciada"} 0.0',
            id="relay",
        ),
        pytest.param(
            "consumidor",
            "pytstop_mensagens_consumidas_total"
            '{resultado="dlq",tipo="desconhecido"} 0.0',
            id="consumidor",
        ),
        pytest.param("api", "pytstop_outbox_mais_antiga_segundos 0.0", id="api"),
    ],
)
def test_o_smoke_reprova_quando_falta_uma_serie_ou_uma_familia(
    processo: str, serie_a_menos: str
) -> None:
    linhas = [
        *(f"# TYPE {f} {t}" for f, t in smoke.FAMILIAS[processo]),
        *(s for s in smoke.SERIES[processo] if s != serie_a_menos),
    ]
    sem_familia, sem_serie = smoke.faltas(processo, linhas)
    assert (sem_familia, sem_serie) == ([], [serie_a_menos])

    familia, tipo = smoke.FAMILIAS[processo][0]
    sem_familia, _ = smoke.faltas(
        processo, [linha for linha in linhas if linha != f"# TYPE {familia} {tipo}"]
    )
    assert sem_familia == [f"{familia} {tipo}"]
