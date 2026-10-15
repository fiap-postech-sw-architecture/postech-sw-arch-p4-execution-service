"""Processo filho de ``test_instrumentacao.py``: o SERVER de uma requisicao com dado
do chamador, na convencao semantica que o ambiente pede.

``OTEL_SEMCONV_STABILITY_OPT_IN`` e lida uma vez por processo, entao cada convencao
(``''``, ``http`` e ``http/dup``) roda num interpretador novo. Imprime os atributos
do span e tudo o que ele leva, em JSON.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind

from src.compartilhado.infraestrutura.instrumentacao import instrumentar_api
from src.compartilhado.infraestrutura.mensageria.telemetria import criar_provedor
from tests.spans import tudo_que_o_span_leva

CANARIO = "CANARIO-9f3a"


def main() -> None:
    exportados = InMemorySpanExporter()
    provedor = criar_provedor({}, processo="teste")
    provedor.add_span_processor(SimpleSpanProcessor(exportados))
    app = FastAPI()

    @app.get("/execucoes/{ordem_id}/inicio")
    def inicio(ordem_id: str) -> dict[str, str]:
        return {"ordem": ordem_id}

    instrumentar_api(app, provedor)

    TestClient(app).get(
        f"/execucoes/{CANARIO}/inicio?placa={CANARIO}",
        headers={
            "User-Agent": f"Navegador/{CANARIO}",
            "Host": f"{CANARIO}.exemplo:8443",
        },
    )

    (servidor,) = [
        s for s in exportados.get_finished_spans() if s.kind is SpanKind.SERVER
    ]
    print(
        json.dumps(
            {
                "atributos": dict(servidor.attributes or {}),
                "tudo": tudo_que_o_span_leva(servidor),
            }
        )
    )


if __name__ == "__main__":
    main()
