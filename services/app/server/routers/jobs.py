"""Jobs, sessão por aba, travas de vídeo e cache."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import StreamingResponse

from .. import durable_jobs, proxy
from ..deps import current_client, current_user, get_object
from ..jobs import jobs, sse_stream
from ..locks import HEARTBEAT_SECONDS, TTL_SECONDS, LockHeld, locks
from ..users import User
from ..workspace import ObjectContext

# Jobs são globais de propósito: o job_id já é único no processo e o payload
# carrega o object_id. Aninhá-los no objeto quebraria a reconexão do watchJob sem
# ganho nenhum.
router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        durable = durable_jobs.get(job_id) if durable_jobs.enabled() else None
        if durable is not None:
            return durable
        raise HTTPException(404, "job não encontrado")
    return job.payload()


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = jobs.get(job_id)
    if job is None:
        if durable_jobs.enabled() and durable_jobs.get(job_id) is not None:
            async def durable_stream():
                import asyncio
                import json

                while True:
                    payload = await asyncio.to_thread(durable_jobs.get, job_id)
                    if payload is None:
                        return
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if payload["state"] in ("done", "cancelled", "error"):
                        return
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(1)

            return StreamingResponse(
                durable_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        raise HTTPException(404, "job não encontrado")
    return StreamingResponse(
        sse_stream(job, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        if durable_jobs.enabled():
            return {"cancelled": durable_jobs.cancel(job_id)}
        raise HTTPException(404, "job não encontrado")
    return {"cancelled": jobs.cancel(job_id)}


# --------------------------------------------------------------------------
# sessão por aba: troca de vídeo + heartbeat da trava
# --------------------------------------------------------------------------

scoped = APIRouter(prefix="/api/objects/{object_id}", tags=["session"])


class ActiveVideo(BaseModel):
    video_id: str | None = None


@scoped.post("/session/active-video")
async def set_active_video(
    payload: ActiveVideo,
    ctx: ObjectContext = Depends(get_object),
    client_id: str = Depends(current_client),
) -> dict:
    """Trocar de vídeo cancela as extrações DESTA aba no vídeo anterior.

    Sem isso, sair de um vídeo longo deixa um ffmpeg moendo CPU em background e
    ainda segura o semáforo pesado. O escopo por aba é o que impede que abrir um
    vídeo mate a extração de outra pessoa — o comportamento anterior cancelava
    todos os jobs do processo.

    Também renova a trava: uma requisição a cada 30 s em vez de duas.
    """
    cancelled = jobs.cancel_others(
        ctx.object_id,
        payload.video_id,
        ("proxy_full", "proxy_window"),
        client_id=client_id,
    )
    if durable_jobs.enabled():
        cancelled += durable_jobs.cancel_others(
            object_id=ctx.object_id,
            video_id=payload.video_id,
            kinds=("proxy_full", "proxy_window"),
            client_id=client_id,
        )
    lock = None
    if payload.video_id:
        lock = locks.heartbeat(ctx.object_id, payload.video_id, client_id)
    jobs.prune()
    return {
        "cancelled": cancelled,
        "lock": lock.to_json() if lock else None,
        "heartbeat_seconds": HEARTBEAT_SECONDS,
    }


class LockPayload(BaseModel):
    video_id: str
    force: bool = False


@scoped.post("/lock")
async def acquire_lock(
    payload: LockPayload,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    video = ctx.index.get(payload.video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")
    try:
        lock = locks.acquire(
            ctx.object_id,
            payload.video_id,
            video.relpath,
            user.display_name,
            client_id,
            force=payload.force,
        )
    except LockHeld as exc:
        raise HTTPException(
            409, {"detail": str(exc), "lock": exc.lock.public()}
        ) from exc
    return {
        "lock": lock.to_json(),
        "heartbeat_seconds": HEARTBEAT_SECONDS,
        "ttl_seconds": TTL_SECONDS,
    }


@scoped.post("/lock/release")
async def release_lock(
    payload: LockPayload,
    client: str | None = None,
    ctx: ObjectContext = Depends(get_object),
    client_id: str = Depends(current_client),
) -> dict:
    """POST e não DELETE porque o `pagehide` usa navigator.sendBeacon, que só faz
    POST — é o que libera o vídeo quando a aba fecha, em vez de esperar o TTL.

    O beacon também não manda header customizado, então o id da aba pode vir na
    query. Não é elevação de privilégio: quem sabe o client_id é quem tem a
    trava, e o pior caso é liberar um vídeo que já ia expirar em 90 s.
    """
    return {"released": locks.release(ctx.object_id, payload.video_id, client or client_id)}


@scoped.get("/locks")
async def list_locks(ctx: ObjectContext = Depends(get_object)) -> dict:
    return {
        "locks": {
            video_id: lock.to_json()
            for video_id, lock in locks.map_for(ctx.object_id).items()
        }
    }


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


@scoped.get("/cache")
async def cache_info(ctx: ObjectContext = Depends(get_object)) -> dict:
    size = proxy.cache_size_bytes(ctx)
    return {
        "bytes": size,
        "gb": round(size / 1024**3, 2),
        "limit_gb": ctx.cache_limit_gb,
    }


@scoped.delete("/cache")
async def clear_cache(
    video_id: str | None = None,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
) -> dict:
    del user
    try:
        proxy.clear_cache(ctx, video_id)
    except KeyError as exc:
        raise HTTPException(404, "video nao encontrado") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return await cache_info(ctx=ctx)
