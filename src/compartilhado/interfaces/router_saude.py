from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["Saude"])


@router.get("/api/v1/saude", summary="Liveness/readiness")
async def saude() -> dict[str, str]:
    """Responde 200 com o processo de pe.

    ``async`` de proposito (licao do p3): as rotas de negocio sao sync e rodam
    no threadpool; sob carga que satura o pool, uma saude sync ficaria na fila
    e o kubelet reiniciaria o pod justamente no pico.
    """
    return {"status": "ok"}
