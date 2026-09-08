"""Fila do SAM3: rotas de operador (na interface) e de worker (o runner)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel

from ..deps import current_user, get_object
from ..sam3 import DEFAULT_LEASE_SECONDS, queue
from ..sam3_worker_status import worker_status
from ..users import User
from ..videos import iso
from ..workspace import ObjectContext, workspace

# Escopado no objeto: é o que a interface usa.
router = APIRouter(prefix="/api/objects/{object_id}", tags=["sam3"])

# Global: o runner drena todos os objetos com um único loop.
worker_router = APIRouter(prefix="/api/sam3", tags=["sam3-worker"])


def _bind(ctx: ObjectContext) -> None:
    queue.bind(ctx.object_id, ctx.output_root)


# --------------------------------------------------------------------------
# operador
# --------------------------------------------------------------------------


class EnqueueIn(BaseModel):
    force: bool = False


@router.post("/videos/{video_id}/sam3", status_code=202)
async def enqueue(
    video_id: str,
    payload: EnqueueIn | None = None,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
) -> dict:
    _bind(ctx)
    video = ctx.index.get(video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")

    entry = ctx.store.entry(video.relpath) or {}
    export = entry.get("export")
    if not export or not export.get("segments"):
        raise HTTPException(
            409, "exporte o vídeo antes: o SAM3 consome os frames e o prompt.json do export"
        )

    item = queue.enqueue(
        ctx.object_id,
        video_id=video_id,
        relpath=video.relpath,
        name=video.name,
        export_root=export["root"],
        segments=list(export["segments"]),
        user=user.user_id,
        force=bool(payload.force if payload else False),
    )
    return item.public()


@router.delete("/videos/{video_id}/sam3")
async def cancel(
    video_id: str,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
) -> dict:
    _bind(ctx)
    video = ctx.index.get(video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")
    item = queue.cancel(ctx.object_id, video.relpath)
    if item is None:
        raise HTTPException(404, "este vídeo não está na fila")
    return item.public()


@router.get("/sam3")
async def list_queue(ctx: ObjectContext = Depends(get_object)) -> dict:
    _bind(ctx)
    items = queue.list(ctx.object_id)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.state] = counts.get(item.state, 0) + 1
    return {
        "object_id": ctx.object_id,
        "counts": counts,
        "active": any(item.active for item in items),
        "worker": worker_status.public(),
        # Chaveado por video_id: é o que o card tem em mãos.
        "videos": {item.video_id: item.public() for item in items},
    }


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------


def require_worker(x_mst_worker: str | None = Header(default=None)) -> str:
    """Token compartilhado entre a triagem e o runner.

    A ferramenta não tem autenticação de verdade e avisa isso em voz alta ao
    subir fora do loopback. Este token não conserta aquilo — ele só impede que
    um browser qualquer da LAN roube um lease e trave um vídeo, que é um estrago
    barato de causar e chato de diagnosticar.

    Sem MST_WORKER_TOKEN no ambiente, as rotas de worker ficam desligadas: é
    melhor o runner falhar com uma mensagem clara do que a fila ficar aberta.
    """
    expected = os.environ.get("MST_WORKER_TOKEN", "")
    if not expected:
        raise HTTPException(
            503, "MST_WORKER_TOKEN não configurado no servidor — a fila do SAM3 está desligada"
        )
    if not x_mst_worker or x_mst_worker != expected:
        raise HTTPException(401, "token de worker inválido")
    return x_mst_worker


class WorkerStatusIn(BaseModel):
    state: str
    message: str | None = None
    worker_id: str


@worker_router.post("/status")
async def report_worker_status(
    payload: WorkerStatusIn, _: str = Depends(require_worker)
) -> dict:
    try:
        worker_status.report(payload.state, payload.message, payload.worker_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


def _resolve_export_root(object_id: str, export_root: str) -> str:
    """O export_root pode ter sido gravado antes de um rehome.

    O runner monta o MESMO caminho de workspace que a triagem, então um caminho
    que não existe aqui também não existiria lá. Recalcular a partir do registro
    é mais confiável que confiar no que foi gravado meses atrás.
    """
    if Path(export_root).is_dir():
        return export_root
    try:
        cfg = workspace.get(object_id)
    except KeyError:
        return export_root
    candidate = cfg.output_root / Path(export_root.replace("\\", "/")).name
    return str(candidate) if candidate.is_dir() else export_root


@worker_router.get("/next")
async def next_job(
    worker: str = "sam3-runner",
    lease: int = DEFAULT_LEASE_SECONDS,
    wait: int = 25,
    _: str = Depends(require_worker),
) -> Response:
    """Próximo job. 204 quando não há nada.

    Faz long-poll: espera até `wait` segundos por uma chegada antes de responder
    vazio. Evita um loop de polling ocupado sem precisar de fila de mensagens.
    """
    # Os objetos só entram no mapa da fila quando alguém os toca; ligar todos
    # aqui garante que o worker enxergue o que ficou pendente antes do restart.
    for cfg in workspace.list():
        queue.bind(cfg.object_id, cfg.output_root)

    taken = queue.take(worker, lease)
    if taken is None and wait > 0:
        event = queue.arrivals()
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(wait, 60))
        except asyncio.TimeoutError:
            pass
        taken = queue.take(worker, lease)

    if taken is None:
        return Response(status_code=204)

    object_id, item = taken
    export_root = _resolve_export_root(object_id, item.export_root)
    try:
        label = workspace.get(object_id).label
    except KeyError:
        label = object_id

    from fastapi.responses import JSONResponse

    return JSONResponse(
        {
            "lease_id": item.lease_id,
            "object_id": object_id,
            "video_id": item.video_id,
            "relpath": item.relpath,
            "name": item.name,
            "label": label,
            "force": item.force,
            "attempt": item.attempts,
            "export_root": export_root,
            "segments": [
                {"segment": segment, "dir": str(Path(export_root) / segment)}
                for segment in item.segments
            ],
            "lease_seconds": lease,
            "issued_at": iso(),
        }
    )


class HeartbeatIn(BaseModel):
    progress: dict | None = None


@worker_router.post("/lease/{lease_id}/heartbeat")
async def heartbeat(
    lease_id: str,
    payload: HeartbeatIn | None = None,
    lease: int = DEFAULT_LEASE_SECONDS,
    _: str = Depends(require_worker),
) -> dict:
    found = queue.heartbeat(lease_id, payload.progress if payload else None, lease)
    if found is None:
        # Lease expirou ou foi roubado: o runner precisa PARAR, senão dois
        # workers processam o mesmo vídeo e escrevem os mesmos arquivos.
        raise HTTPException(409, "lease inválido ou expirado — abandone este job")
    item, cancel_requested = found
    return {
        "ok": True,
        "cancel_requested": cancel_requested,
        "lease_seconds": lease,
        "state": item.state,
    }


class ResultIn(BaseModel):
    state: str = "done"
    result: dict | None = None
    error: str | None = None


@worker_router.post("/lease/{lease_id}/result")
async def result(
    lease_id: str, payload: ResultIn, _: str = Depends(require_worker)
) -> dict:
    found = queue.finish(
        lease_id, state=payload.state, result=payload.result, error=payload.error
    )
    if found is None:
        raise HTTPException(409, "lease inválido ou expirado")
    object_id, item = found

    # O resultado durável vai para o annotations.json, junto de tudo mais que
    # descreve o vídeo — a fila é estado de sessão, o dataset é o artefato.
    if item.state == "done":
        try:
            ctx = workspace.context(object_id)
            ctx.ensure_loaded()

            def apply(doc: dict) -> None:
                entry = doc["videos"].get(item.relpath)
                if entry is not None:
                    entry["sam3"] = {
                        "at": iso(),
                        "runner_version": (payload.result or {}).get("runner_version"),
                        **{
                            key: value
                            for key, value in (payload.result or {}).items()
                            if key != "runner_version"
                        },
                    }

            await ctx.store.mutate(apply)
        except Exception:  # noqa: BLE001 — a fila já registrou; não derruba o worker
            pass

    return {"ok": True, "state": item.state}


@worker_router.get("/health")
async def worker_health(_: str = Depends(require_worker)) -> dict:
    return {"ok": True, "pending": queue.has_pending()}
