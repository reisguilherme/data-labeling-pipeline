"""Metadados, streaming e frames de proxy de um vídeo."""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from .. import durable_jobs, media, proxy
from ..config import WINDOW_RADIUS
from ..deps import current_client, get_object
from ..workspace import ObjectContext

router = APIRouter(prefix="/api/objects/{object_id}/videos", tags=["video"])


async def frame_count_for(ctx: ObjectContext, video_id: str) -> int | None:
    """Melhor contagem conhecida, promovendo o proxy completo quando existe."""
    exact = proxy.is_complete(ctx, video_id)
    if exact is not None:
        return exact
    cached = ctx.index.cached_probe(video_id)
    if cached is None:
        # Nunca percorra o arquivo inteiro dentro de uma requisição. A extração
        # completa promove a contagem exata quando terminar no worker.
        cached = await ctx.index.probe(video_id, count_packets=False)
    return cached.get("frame_count")


def _require(ctx: ObjectContext, video_id: str):
    video = ctx.index.get(video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")
    return video


@router.get("/{video_id}/meta")
async def get_meta(
    video_id: str, refresh: bool = False, ctx: ObjectContext = Depends(get_object)
) -> dict:
    video = _require(ctx, video_id)

    cached = None if refresh else ctx.index.cached_probe(video_id)
    if cached is None or refresh:
        # Um probe de cabeçalho é limitado; contar todos os pacotes de um vídeo
        # grande fazia abrir a triagem levar vários minutos.
        try:
            cached = await ctx.index.probe(video_id, count_packets=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, f"ffprobe falhou: {exc}") from exc

    return {
        "video_id": video_id,
        "object_id": ctx.object_id,
        "label": ctx.label,
        "relpath": video.relpath,
        "name": video.name,
        "size_bytes": video.size_bytes,
        "file_mtime": video.file_mtime,
        "media": cached,
        "stream_url": f"/api/objects/{ctx.object_id}/videos/{video_id}/stream",
    }


@router.api_route("/{video_id}/stream", methods=["GET", "HEAD"])
async def stream(
    video_id: str, request: Request, ctx: ObjectContext = Depends(get_object)
) -> Response:
    _require(ctx, video_id)
    path = ctx.index.resolve_path(video_id)
    guessed, _ = mimetypes.guess_type(path.name)
    return media.range_response(request, path, guessed or "video/mp4")


# --------------------------------------------------------------------------
# proxy
# --------------------------------------------------------------------------


class ProxyPayload(BaseModel):
    force: bool = False


@router.post("/{video_id}/proxy")
async def start_proxy(
    video_id: str,
    payload: ProxyPayload | None = None,
    ctx: ObjectContext = Depends(get_object),
    client_id: str = Depends(current_client),
) -> dict:
    _require(ctx, video_id)

    force = payload.force if payload else False
    frame_count = await frame_count_for(ctx, video_id)
    plan = proxy.plan_for(video_id, frame_count)

    if plan.mode == "window":
        return {
            "mode": "window",
            "job_id": None,
            "total_frames": frame_count,
            "already_complete": False,
        }

    if durable_jobs.enabled():
        if not force and proxy.is_complete(ctx, video_id) is not None:
            job_id = None
        else:
            job_id = durable_jobs.create(
                kind="proxy_full",
                object_id=ctx.object_id,
                payload={
                    "video_id": video_id,
                    "frame_count": plan.frame_count,
                    "force": force,
                    "client_id": client_id,
                    "message": "extraindo frames",
                },
                priority=40,
                idempotency_key=f"proxy-full:{ctx.object_id}:{video_id}",
            )
        return {
            "mode": "full",
            "job_id": job_id,
            "total_frames": frame_count,
            "already_complete": job_id is None,
        }

    job = await proxy.start_full(
        ctx, video_id, plan.frame_count, force=force, client_id=client_id
    )
    return {
        "mode": "full",
        "job_id": job.job_id if job else None,
        "total_frames": frame_count,
        "already_complete": job is None,
    }


class WindowPayload(BaseModel):
    center: int
    radius: int = WINDOW_RADIUS
    force: bool = False


@router.post("/{video_id}/window")
async def start_window(
    video_id: str,
    payload: WindowPayload,
    ctx: ObjectContext = Depends(get_object),
    client_id: str = Depends(current_client),
) -> dict:
    _require(ctx, video_id)

    frame_count = await frame_count_for(ctx, video_id)
    start = max(payload.center - payload.radius, 0)
    end = payload.center + payload.radius
    if frame_count:
        end = min(end, frame_count - 1)
    end = max(end, start)
    if not payload.force:
        for existing_start, existing_end in proxy.available_ranges(ctx, video_id):
            if existing_start <= start and end <= existing_end:
                return {
                    "job_id": None,
                    "start": existing_start,
                    "end": existing_end,
                    "already_available": True,
                }
    if durable_jobs.enabled():
        job_id = durable_jobs.create(
            kind="proxy_window",
            object_id=ctx.object_id,
            payload={
                "video_id": video_id,
                "start": start,
                "end": end,
                "client_id": client_id,
                "message": "extraindo janela",
            },
            priority=30,
            idempotency_key=f"proxy-window:{ctx.object_id}:{video_id}:{start}:{end}",
        )
        return {
            "job_id": job_id,
            "start": start,
            "end": end,
            "already_available": False,
        }
    job, start, end = await proxy.start_window(
        ctx,
        video_id,
        payload.center,
        payload.radius,
        frame_count,
        client_id=client_id,
        force=payload.force,
    )
    return {
        "job_id": job.job_id if job else None,
        "start": start,
        "end": end,
        "already_available": job is None,
    }


@router.get("/{video_id}/proxy/status")
async def proxy_status(video_id: str, ctx: ObjectContext = Depends(get_object)) -> dict:
    _require(ctx, video_id)
    return proxy.status(ctx, video_id, await frame_count_for(ctx, video_id))


@router.get("/{video_id}/frames/{frame}.jpg")
async def get_frame(
    video_id: str,
    frame: int,
    tier: str = proxy.SMALL,
    ctx: ObjectContext = Depends(get_object),
) -> Response:
    """`tier=small` alimenta filmstrip e reprodução; `tier=full` é a imagem grande
    do palco, na resolução original."""
    _require(ctx, video_id)

    path = proxy.locate_frame(ctx, video_id, frame, tier)
    if path is not None:
        return FileResponse(
            path,
            media_type="image/jpeg",
            # A URL é estável, mas uma geração reparada pode substituir o JPEG.
            # Revalidar evita que o browser retenha para sempre um frame defeituoso.
            headers={"Cache-Control": "private, max-age=0, must-revalidate"},
        )

    frame_count = await frame_count_for(ctx, video_id)
    if frame_count is not None and not (0 <= frame < frame_count):
        raise HTTPException(404, "frame fora do vídeo")

    # Ainda não extraído: o cliente sabe pedir a janela a partir daqui.
    start = max(frame - WINDOW_RADIUS, 0)
    end = frame + WINDOW_RADIUS
    if frame_count:
        end = min(end, frame_count - 1)
    return JSONResponse(
        {"detail": "frame ainda não extraído", "needed_window": [start, end]},
        status_code=409,
    )
