"""Metricas Prometheus da API, expostas em ``/metrics``.

``http_request_duration_seconds{method,rota,status}`` mantem o nome e os labels
do p3 (contrato com os dashboards). ``rota`` e o TEMPLATE da rota casada
(``/api/v1/execucoes/{ordem_id}/inicio``), nunca o path bruto: cardinalidade
limitada; request sem rota casada agrega em ``nao_roteada``.

Diferente do p3 (OTel MeterProvider + PrometheusMetricReader atras de flag),
aqui o ``prometheus_client`` e usado direto e sempre ligado: um processo
uvicorn por pod, sem SDK extra so para metricas.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request

_ROTA_NAO_ROTEADA: Final = "nao_roteada"

# 5ms..5s: do hit simples ao pior caso com lock pessimista ou retry no Billing.
_LATENCIA_HTTP = Histogram(
    "http_request_duration_seconds",
    "Duracao das requests HTTP da API por rota.",
    ["method", "rota", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


def _rota_template(request: Request) -> str:
    rota = request.scope.get("route")
    template = getattr(rota, "path_format", None) or getattr(rota, "path", None)
    return template if isinstance(template, str) else _ROTA_NAO_ROTEADA


class MetricasHTTPMiddleware(BaseHTTPMiddleware):
    """Observa a latencia de cada request; excecao conta como 500 e propaga."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        inicio = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            _LATENCIA_HTTP.labels(
                request.method, _rota_template(request), str(status)
            ).observe(time.perf_counter() - inicio)


def configurar_metricas(app: FastAPI) -> None:
    """Expoe ``GET /metrics`` e instala o middleware (antes do app servir).

    Rota comum, e nao o ``make_asgi_app`` montado: o mount responde ``/metrics``
    com 307 para ``/metrics/`` e cada scrape viraria duas series, uma delas
    ``nao_roteada``.
    """

    @app.get("/metrics", include_in_schema=False)
    def metricas() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.add_middleware(MetricasHTTPMiddleware)
